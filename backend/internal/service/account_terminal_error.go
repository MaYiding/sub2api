package service

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/util/logredact"
	"github.com/tidwall/gjson"
)

// terminalAccountErrorKind describes an upstream account failure that cannot
// recover through request retries or a short rate-limit cooldown. These values
// are persisted in account.error_message and emitted in structured logs, so
// keep them stable and machine-readable.
type terminalAccountErrorKind string

const (
	terminalAccountSubscriptionInactive terminalAccountErrorKind = "subscription_inactive"
	terminalAccountDisabled             terminalAccountErrorKind = "account_disabled"
	terminalAccountCredentialRevoked    terminalAccountErrorKind = "credential_revoked"
	terminalAccountVerificationRequired terminalAccountErrorKind = "verification_required"
	terminalAccountBillingExhausted     terminalAccountErrorKind = "billing_exhausted"
)

type terminalAccountError struct {
	Kind   terminalAccountErrorKind
	Signal string
}

var terminalAccountCodeKinds = map[string]terminalAccountErrorKind{
	// Subscription / entitlement failures.
	"subscription_expired":          terminalAccountSubscriptionInactive,
	"subscription_inactive":         terminalAccountSubscriptionInactive,
	"subscription_invalid":          terminalAccountSubscriptionInactive,
	"subscription_required":         terminalAccountSubscriptionInactive,
	"no_active_subscription":        terminalAccountSubscriptionInactive,
	"entitlement_denied":            terminalAccountSubscriptionInactive,
	"grok_oauth_entitlement_denied": terminalAccountSubscriptionInactive,

	// Account, organization, or workspace lifecycle failures.
	"account_deactivated":      terminalAccountDisabled,
	"account_disabled":         terminalAccountDisabled,
	"account_suspended":        terminalAccountDisabled,
	"account_terminated":       terminalAccountDisabled,
	"organization_deactivated": terminalAccountDisabled,
	"organization_disabled":    terminalAccountDisabled,
	"workspace_deactivated":    terminalAccountDisabled,
	"deactivated_workspace":    terminalAccountDisabled,
	"workspace_disabled":       terminalAccountDisabled,

	// Revoked credentials can also surface as 400/403 instead of 401.
	"api_key_blocked":    terminalAccountCredentialRevoked,
	"api_key_disabled":   terminalAccountCredentialRevoked,
	"api_key_revoked":    terminalAccountCredentialRevoked,
	"credential_revoked": terminalAccountCredentialRevoked,
	"key_revoked":        terminalAccountCredentialRevoked,

	// Manual verification gates.
	"identity_verification_required": terminalAccountVerificationRequired,
	"verification_required":          terminalAccountVerificationRequired,

	// Billing failures that require an operator action. Generic quota/rate-limit
	// codes are intentionally absent because they normally recover on a timer.
	"billing_hard_limit_reached": terminalAccountBillingExhausted,
	"credit_balance_exhausted":   terminalAccountBillingExhausted,
	"insufficient_credits":       terminalAccountBillingExhausted,
	"insufficient_quota":         terminalAccountBillingExhausted,
	"payment_required":           terminalAccountBillingExhausted,
}

var terminalAccountPhrases = []struct {
	Kind   terminalAccountErrorKind
	Phrase string
}{
	{terminalAccountSubscriptionInactive, "subscription is invalid or expired"},
	{terminalAccountSubscriptionInactive, "subscription has expired"},
	{terminalAccountSubscriptionInactive, "subscription is expired"},
	{terminalAccountSubscriptionInactive, "subscription expired"},
	{terminalAccountSubscriptionInactive, "subscription is inactive"},
	{terminalAccountSubscriptionInactive, "subscription required"},
	{terminalAccountSubscriptionInactive, "active subscription is required"},
	{terminalAccountSubscriptionInactive, "requires an active subscription"},
	{terminalAccountSubscriptionInactive, "no active subscription"},
	{terminalAccountSubscriptionInactive, "no active grok subscription"},
	{terminalAccountSubscriptionInactive, "subscription has been canceled"},
	{terminalAccountSubscriptionInactive, "subscription has been cancelled"},
	{terminalAccountSubscriptionInactive, "订阅已过期"},
	{terminalAccountSubscriptionInactive, "订阅已失效"},
	{terminalAccountSubscriptionInactive, "无有效订阅"},
	{terminalAccountSubscriptionInactive, "需要有效订阅"},

	{terminalAccountDisabled, "account has been deactivated"},
	{terminalAccountDisabled, "account is deactivated"},
	{terminalAccountDisabled, "account has been disabled"},
	{terminalAccountDisabled, "account is disabled"},
	{terminalAccountDisabled, "account has been suspended"},
	{terminalAccountDisabled, "account is suspended"},
	{terminalAccountDisabled, "account has been terminated"},
	{terminalAccountDisabled, "organization has been disabled"},
	{terminalAccountDisabled, "organization is disabled"},
	{terminalAccountDisabled, "workspace has been deactivated"},
	{terminalAccountDisabled, "workspace is deactivated"},
	{terminalAccountDisabled, "workspace has been disabled"},
	{terminalAccountDisabled, "workspace is disabled"},
	{terminalAccountDisabled, "terms of service violation"},
	{terminalAccountDisabled, "账号已停用"},
	{terminalAccountDisabled, "账户已停用"},
	{terminalAccountDisabled, "账号已禁用"},
	{terminalAccountDisabled, "账户已禁用"},
	{terminalAccountDisabled, "账号已封禁"},
	{terminalAccountDisabled, "账户已封禁"},
	{terminalAccountDisabled, "工作区已停用"},

	{terminalAccountCredentialRevoked, "api key was reported as leaked"},
	{terminalAccountCredentialRevoked, "api key has been blocked"},
	{terminalAccountCredentialRevoked, "api key is blocked"},
	{terminalAccountCredentialRevoked, "api key has been revoked"},
	{terminalAccountCredentialRevoked, "api key is revoked"},

	{terminalAccountVerificationRequired, "identity verification is required"},
	{terminalAccountVerificationRequired, "account verification is required"},
	{terminalAccountVerificationRequired, "需要身份验证"},
	{terminalAccountVerificationRequired, "请完成身份验证"},

	{terminalAccountBillingExhausted, "you exceeded your current quota, please check your plan and billing details"},
	{terminalAccountBillingExhausted, "billing hard limit"},
	{terminalAccountBillingExhausted, "run out of credits"},
	{terminalAccountBillingExhausted, "insufficient credit balance"},
	{terminalAccountBillingExhausted, "credit balance exhausted"},
	{terminalAccountBillingExhausted, "maximum monthly spend"},
	{terminalAccountBillingExhausted, "账户余额不足"},
	{terminalAccountBillingExhausted, "账号余额不足"},
	{terminalAccountBillingExhausted, "余额已用尽"},
}

// classifyTerminalAccountError recognizes only high-confidence, account-wide
// failures. A bare HTTP status is never enough (except the existing 401/402/403
// policy in RateLimitService): 429 can mean a short RPM limit, a daily window,
// or a billing/quota exhaustion, so the response must carry a terminal signal.
func classifyTerminalAccountError(account *Account, statusCode int, responseBody []byte) (terminalAccountError, bool) {
	if account == nil || statusCode < http.StatusBadRequest || statusCode >= http.StatusInternalServerError || len(responseBody) == 0 {
		return terminalAccountError{}, false
	}

	for _, signal := range terminalAccountStructuredSignals(responseBody) {
		normalized := normalizeTerminalAccountSignal(signal)
		kind, ok := terminalAccountCodeKinds[normalized]
		if !ok || !terminalAccountKindAllowed(account, statusCode, kind) {
			continue
		}
		return terminalAccountError{Kind: kind, Signal: normalized}, true
	}

	phraseText := terminalAccountPhraseText(responseBody)
	if len(phraseText) > tempUnschedBodyMaxBytes {
		phraseText = phraseText[:tempUnschedBodyMaxBytes]
	}
	bodyLower := strings.ToLower(phraseText)
	for _, candidate := range terminalAccountPhrases {
		if !terminalAccountKindAllowed(account, statusCode, candidate.Kind) {
			continue
		}
		if strings.Contains(bodyLower, candidate.Phrase) {
			return terminalAccountError{Kind: candidate.Kind, Signal: candidate.Phrase}, true
		}
	}

	return terminalAccountError{}, false
}

// terminalAccountPhraseText deliberately limits semantic matching to known
// error-message fields. Some providers echo request metadata in JSON errors;
// scanning the whole object could disable an account merely because user input
// contained a terminal-looking phrase. Non-JSON error bodies are treated as
// plain upstream messages and remain eligible for matching.
func terminalAccountPhraseText(responseBody []byte) string {
	if !json.Valid(responseBody) {
		return string(responseBody)
	}

	paths := []string{
		"error.message",
		"error.detail",
		"detail.message",
		"detail",
		"message",
		"error_description",
	}
	parts := make([]string, 0, len(paths)+4)
	for _, path := range paths {
		result := gjson.GetBytes(responseBody, path)
		if result.Type == gjson.String && strings.TrimSpace(result.String()) != "" {
			parts = append(parts, result.String())
		}
	}
	if result := gjson.GetBytes(responseBody, "error"); result.Type == gjson.String && strings.TrimSpace(result.String()) != "" {
		parts = append(parts, result.String())
	}
	for _, result := range gjson.GetBytes(responseBody, "error.details.#.message").Array() {
		if result.Type == gjson.String && strings.TrimSpace(result.String()) != "" {
			parts = append(parts, result.String())
		}
	}
	if root := gjson.ParseBytes(responseBody); root.Type == gjson.String && strings.TrimSpace(root.String()) != "" {
		parts = append(parts, root.String())
	}
	return strings.Join(parts, "\n")
}

// terminalAccountKindAllowed preserves provider-specific retry semantics.
// Google/Anthropic/Antigravity document and use 429 as a recoverable quota or
// rate window; their billing-looking 429 text must not become a permanent ban.
func terminalAccountKindAllowed(account *Account, statusCode int, kind terminalAccountErrorKind) bool {
	// Refreshable OpenAI OAuth credentials use the existing 401 recovery path:
	// invalidate the cached token and pause scheduling while refresh runs. Error
	// messages such as "account is disabled" are not authoritative enough to
	// bypass that policy; explicit token_invalidated/token_revoked codes remain
	// permanently handled by RateLimitService's 401 branch.
	if statusCode == http.StatusUnauthorized && account.IsOpenAIOAuth() {
		return false
	}
	if kind != terminalAccountBillingExhausted || statusCode != http.StatusTooManyRequests {
		return true
	}
	return account.Platform == PlatformOpenAI || account.Platform == PlatformGrok
}

func terminalAccountStructuredSignals(responseBody []byte) []string {
	paths := []string{
		"error.code",
		"error.type",
		"error.status",
		"detail.code",
		"detail.type",
		"code",
		"type",
		"status",
	}
	signals := make([]string, 0, len(paths)+4)
	for _, path := range paths {
		if value := strings.TrimSpace(gjson.GetBytes(responseBody, path).String()); value != "" {
			signals = append(signals, value)
		}
	}

	// Several OAuth/token endpoints return {"error":"subscription required"}.
	if result := gjson.GetBytes(responseBody, "error"); result.Type == gjson.String {
		if value := strings.TrimSpace(result.String()); value != "" {
			signals = append(signals, value)
		}
	}
	for _, result := range gjson.GetBytes(responseBody, "error.details.#.reason").Array() {
		if value := strings.TrimSpace(result.String()); value != "" {
			signals = append(signals, value)
		}
	}
	return signals
}

func normalizeTerminalAccountSignal(signal string) string {
	signal = strings.ToLower(strings.TrimSpace(signal))
	signal = strings.NewReplacer("-", "_", " ", "_", ".", "_").Replace(signal)
	for strings.Contains(signal, "__") {
		signal = strings.ReplaceAll(signal, "__", "_")
	}
	return strings.Trim(signal, "_")
}

func safeUpstreamAccountErrorMessage(responseBody []byte) string {
	msg := strings.TrimSpace(extractUpstreamErrorMessage(responseBody))
	if msg == "" {
		if result := gjson.GetBytes(responseBody, "error"); result.Type == gjson.String {
			msg = strings.TrimSpace(result.String())
		}
	}
	if msg == "" {
		return ""
	}
	msg = sanitizeUpstreamErrorMessage(msg)
	msg = logredact.RedactText(msg, "api_key", "secret_key")
	return truncateForLog([]byte(msg), 512)
}

func formatTerminalAccountErrorMessage(statusCode int, terminal terminalAccountError, responseBody []byte) string {
	prefix := fmt.Sprintf("Auto-disabled terminal upstream error [%s] (HTTP %d)", terminal.Kind, statusCode)
	if msg := safeUpstreamAccountErrorMessage(responseBody); msg != "" {
		return prefix + ": " + msg
	}
	return prefix
}

// handleImmediateAccountDisable applies account-wide policies that must run
// before model-specific 429 handling. Several mature gateway paths have their
// own quota logic and do not call HandleUpstreamError for 429; keeping this
// small gate reusable prevents those paths from silently bypassing explicit
// custom error codes or terminal subscription/account failures.
func (s *RateLimitService) handleImmediateAccountDisable(
	ctx context.Context,
	account *Account,
	statusCode int,
	responseBody []byte,
) bool {
	if s == nil || s.accountRepo == nil || account == nil {
		return false
	}

	customErrorCodesEnabled := account.IsCustomErrorCodesEnabled()
	if account.IsPoolMode() && !customErrorCodesEnabled {
		return false
	}
	if !account.ShouldHandleErrorCode(statusCode) {
		return false
	}

	// A non-empty custom list is an explicit administrator decision. An enabled
	// but empty list intentionally falls through to the default policy.
	if customErrorCodesEnabled && len(account.GetCustomErrorCodes()) > 0 {
		msg := safeUpstreamAccountErrorMessage(responseBody)
		if msg == "" {
			msg = "Custom error code triggered"
		}
		s.handleCustomErrorCode(ctx, account, statusCode, msg)
		return true
	}

	terminal, ok := classifyTerminalAccountError(account, statusCode, responseBody)
	if !ok {
		return false
	}

	terminalAccount := account
	if account.IsShadow() {
		if resolved, err := resolveCredentialAccount(ctx, s.accountRepo, account); err == nil && resolved != nil {
			terminalAccount = resolved
		}
	}
	s.handleTerminalAccountError(ctx, terminalAccount, statusCode, terminal, responseBody)
	return true
}

func (s *RateLimitService) handleTerminalAccountError(
	ctx context.Context,
	account *Account,
	statusCode int,
	terminal terminalAccountError,
	responseBody []byte,
) {
	if s == nil || account == nil {
		return
	}
	errorMsg := formatTerminalAccountErrorMessage(statusCode, terminal, responseBody)
	s.notifyAccountSchedulingBlocked(account, time.Time{}, "terminal_"+string(terminal.Kind))
	if err := s.accountRepo.SetError(ctx, account.ID, errorMsg); err != nil {
		slog.Warn(
			"account_terminal_error_set_failed",
			"account_id", account.ID,
			"status_code", statusCode,
			"classification", terminal.Kind,
			"signal", terminal.Signal,
			"error", err,
		)
		return
	}
	slog.Warn(
		"account_auto_disabled_terminal_error",
		"account_id", account.ID,
		"platform", account.Platform,
		"status_code", statusCode,
		"classification", terminal.Kind,
		"signal", terminal.Signal,
	)
}
