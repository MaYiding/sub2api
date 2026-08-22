//go:build unit

package service

import (
	"context"
	"net/http"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/stretchr/testify/require"
)

func TestClassifyTerminalAccountError(t *testing.T) {
	tests := []struct {
		name       string
		account    *Account
		statusCode int
		body       string
		wantKind   terminalAccountErrorKind
		want       bool
	}{
		{
			name:       "openai_429_insufficient_quota_is_terminal",
			account:    &Account{Platform: PlatformOpenAI},
			statusCode: http.StatusTooManyRequests,
			body:       `{"error":{"code":"insufficient_quota","message":"You exceeded your current quota, please check your plan and billing details."}}`,
			wantKind:   terminalAccountBillingExhausted,
			want:       true,
		},
		{
			name:       "openai_429_billing_phrase_is_terminal",
			account:    &Account{Platform: PlatformOpenAI},
			statusCode: http.StatusTooManyRequests,
			body:       `{"error":{"message":"You exceeded your current quota, please check your plan and billing details."}}`,
			wantKind:   terminalAccountBillingExhausted,
			want:       true,
		},
		{
			name:       "openai_usage_window_429_is_not_terminal",
			account:    &Account{Platform: PlatformOpenAI},
			statusCode: http.StatusTooManyRequests,
			body:       `{"error":{"code":"rate_limit_exceeded","type":"usage_limit_reached","message":"The usage limit has been reached","resets_in_seconds":3600}}`,
			want:       false,
		},
		{
			name:       "openai_oauth_401_account_disabled_keeps_refresh_policy",
			account:    &Account{Platform: PlatformOpenAI, Type: AccountTypeOAuth},
			statusCode: http.StatusUnauthorized,
			body:       `{"error":{"message":"account is disabled"}}`,
			want:       false,
		},
		{
			name:       "gemini_resource_exhausted_429_is_not_terminal",
			account:    &Account{Platform: PlatformGemini},
			statusCode: http.StatusTooManyRequests,
			body:       `{"error":{"code":429,"status":"RESOURCE_EXHAUSTED","message":"You exceeded your current quota, please check your plan and billing details."}}`,
			want:       false,
		},
		{
			name:       "anthropic_rate_limit_429_is_not_terminal",
			account:    &Account{Platform: PlatformAnthropic},
			statusCode: http.StatusTooManyRequests,
			body:       `{"type":"error","error":{"type":"rate_limit_error","message":"rate limit reached"}}`,
			want:       false,
		},
		{
			name:       "antigravity_credit_429_keeps_existing_cooldown_policy",
			account:    &Account{Platform: PlatformAntigravity},
			statusCode: http.StatusTooManyRequests,
			body:       `{"error":{"status":"RESOURCE_EXHAUSTED","message":"insufficient credit balance"}}`,
			want:       false,
		},
		{
			name:       "grok_subscription_required_is_terminal",
			account:    &Account{Platform: PlatformGrok},
			statusCode: http.StatusForbidden,
			body:       `{"error":"subscription required"}`,
			wantKind:   terminalAccountSubscriptionInactive,
			want:       true,
		},
		{
			name:       "disabled_organization_is_terminal",
			account:    &Account{Platform: PlatformOpenAI},
			statusCode: http.StatusBadRequest,
			body:       `{"error":{"message":"The organization has been disabled."}}`,
			wantKind:   terminalAccountDisabled,
			want:       true,
		},
		{
			name:       "deactivated_workspace_code_is_terminal",
			account:    &Account{Platform: PlatformOpenAI},
			statusCode: http.StatusPaymentRequired,
			body:       `{"detail":{"code":"deactivated_workspace"}}`,
			wantKind:   terminalAccountDisabled,
			want:       true,
		},
		{
			name:       "leaked_gemini_key_is_terminal",
			account:    &Account{Platform: PlatformGemini},
			statusCode: http.StatusForbidden,
			body:       `{"error":{"status":"PERMISSION_DENIED","message":"Your API key was reported as leaked. Please use another API key."}}`,
			wantKind:   terminalAccountCredentialRevoked,
			want:       true,
		},
		{
			name:       "identity_verification_is_terminal",
			account:    &Account{Platform: PlatformOpenAI},
			statusCode: http.StatusBadRequest,
			body:       `{"error":{"message":"Identity verification is required to continue."}}`,
			wantKind:   terminalAccountVerificationRequired,
			want:       true,
		},
		{
			name:       "model_plan_gate_is_not_account_terminal",
			account:    &Account{Platform: PlatformOpenAI},
			statusCode: http.StatusBadRequest,
			body:       `{"error":{"message":"This model is not supported on your current plan."}}`,
			want:       false,
		},
		{
			name:       "server_error_body_never_auto_disables",
			account:    &Account{Platform: PlatformOpenAI},
			statusCode: http.StatusInternalServerError,
			body:       `{"error":{"message":"subscription required"}}`,
			want:       false,
		},
		{
			name:       "terminal_phrase_in_echoed_request_is_not_terminal",
			account:    &Account{Platform: PlatformOpenAI},
			statusCode: http.StatusBadRequest,
			body:       `{"error":{"message":"invalid request"},"request":{"prompt":"subscription expired"}}`,
			want:       false,
		},
		{
			name:       "plain_text_terminal_error_is_detected",
			account:    &Account{Platform: PlatformGrok},
			statusCode: http.StatusForbidden,
			body:       `active subscription is required`,
			wantKind:   terminalAccountSubscriptionInactive,
			want:       true,
		},
		{
			name:       "empty_body_is_not_terminal",
			account:    &Account{Platform: PlatformOpenAI},
			statusCode: http.StatusTooManyRequests,
			body:       "",
			want:       false,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, ok := classifyTerminalAccountError(tt.account, tt.statusCode, []byte(tt.body))
			require.Equal(t, tt.want, ok)
			if tt.want {
				require.Equal(t, tt.wantKind, got.Kind)
				require.NotEmpty(t, got.Signal)
			}
		})
	}
}

func TestRateLimitService_HandleUpstreamError_Terminal429AutoDisables(t *testing.T) {
	repo := &rateLimitAccountRepoStub{}
	blocker := &runtimeBlockRecorder{}
	svc := NewRateLimitService(repo, nil, &config.Config{}, nil, nil)
	svc.SetAccountRuntimeBlocker(blocker)
	account := &Account{
		ID:       701,
		Platform: PlatformOpenAI,
		Type:     AccountTypeAPIKey,
	}

	shouldDisable := svc.HandleUpstreamError(
		context.Background(),
		account,
		http.StatusTooManyRequests,
		http.Header{},
		[]byte(`{"error":{"code":"insufficient_quota","message":"You exceeded your current quota, please check your plan and billing details."}}`),
	)

	require.True(t, shouldDisable)
	require.Equal(t, 1, repo.setErrorCalls)
	require.Equal(t, account.ID, repo.lastErrorID)
	require.Contains(t, repo.lastErrorMsg, "[billing_exhausted]")
	require.Contains(t, repo.lastErrorMsg, "HTTP 429")
	require.Len(t, blocker.accounts, 1)
	require.Equal(t, "terminal_billing_exhausted", blocker.reasons[0])
}

func TestRateLimitService_HandleUpstreamError_TerminalErrorOverridesTempRule(t *testing.T) {
	repo := &rateLimitAccountRepoStub{}
	svc := NewRateLimitService(repo, nil, &config.Config{}, nil, nil)
	account := &Account{
		ID:       702,
		Platform: PlatformGrok,
		Type:     AccountTypeOAuth,
		Credentials: map[string]any{
			"temp_unschedulable_enabled": true,
			"temp_unschedulable_rules": []any{
				map[string]any{
					"error_code":       float64(http.StatusForbidden),
					"keywords":         []any{"subscription required"},
					"duration_minutes": float64(30),
				},
			},
		},
	}

	shouldDisable := svc.HandleUpstreamError(
		context.Background(),
		account,
		http.StatusForbidden,
		http.Header{},
		[]byte(`{"error":"subscription required"}`),
	)

	require.True(t, shouldDisable)
	require.Equal(t, 1, repo.setErrorCalls)
	require.Equal(t, 0, repo.tempCalls)
	require.Contains(t, repo.lastErrorMsg, "[subscription_inactive]")
}

func TestRateLimitService_CheckErrorPolicy_TerminalErrorOverridesTempRule(t *testing.T) {
	repo := &rateLimitAccountRepoStub{}
	svc := NewRateLimitService(repo, nil, &config.Config{}, nil, nil)
	account := &Account{
		ID:       703,
		Platform: PlatformGrok,
		Type:     AccountTypeOAuth,
		Credentials: map[string]any{
			"temp_unschedulable_enabled": true,
			"temp_unschedulable_rules": []any{
				map[string]any{
					"error_code":       float64(http.StatusForbidden),
					"keywords":         []any{"subscription required"},
					"duration_minutes": float64(30),
				},
			},
		},
	}

	result := svc.CheckErrorPolicy(
		context.Background(),
		account,
		http.StatusForbidden,
		[]byte(`{"error":"subscription required"}`),
	)

	require.Equal(t, ErrorPolicyMatched, result)
	require.Equal(t, 0, repo.tempCalls)
}

func TestSpecializedGateway429PathsHonorExplicitAutoDisable(t *testing.T) {
	t.Run("gemini", func(t *testing.T) {
		repo := &rateLimitAccountRepoStub{}
		rateLimitSvc := NewRateLimitService(repo, nil, &config.Config{}, nil, nil)
		gateway := &GeminiMessagesCompatService{rateLimitService: rateLimitSvc}
		account := explicitCustom429Account(710, PlatformGemini)

		gateway.handleGeminiUpstreamError(
			context.Background(), account, http.StatusTooManyRequests, http.Header{},
			[]byte(`{"error":{"status":"RESOURCE_EXHAUSTED","message":"rate limited"}}`),
		)

		require.Equal(t, 1, repo.setErrorCalls)
		require.Contains(t, repo.lastErrorMsg, "Custom error code 429")
	})

	t.Run("antigravity", func(t *testing.T) {
		repo := &rateLimitAccountRepoStub{}
		rateLimitSvc := NewRateLimitService(repo, nil, &config.Config{}, nil, nil)
		gateway := &AntigravityGatewayService{accountRepo: repo, rateLimitService: rateLimitSvc}
		account := explicitCustom429Account(711, PlatformAntigravity)

		body := []byte(`{"error":{"status":"RESOURCE_EXHAUSTED","details":[{"reason":"RATE_LIMIT_EXCEEDED","metadata":{"model":"gemini-3-flash"}}]}}`)
		params := antigravityRetryLoopParams{
			ctx:             context.Background(),
			prefix:          "[test]",
			account:         account,
			accountRepo:     repo,
			requestedModel:  "gemini-3-flash",
			groupID:         1,
			sessionHash:     "session",
			isStickySession: true,
			handleError:     gateway.handleUpstreamError,
		}
		handled, status, err := gateway.applyErrorPolicy(params, http.StatusTooManyRequests, http.Header{}, body)

		require.True(t, handled)
		require.Equal(t, http.StatusTooManyRequests, status)
		require.NoError(t, err)
		require.Equal(t, 1, repo.setErrorCalls)
		require.Contains(t, repo.lastErrorMsg, "Custom error code 429")
	})

	t.Run("grok", func(t *testing.T) {
		repo := &rateLimitAccountRepoStub{}
		rateLimitSvc := NewRateLimitService(repo, nil, &config.Config{}, nil, nil)
		gateway := &OpenAIGatewayService{accountRepo: repo, rateLimitService: rateLimitSvc}
		account := explicitCustom429Account(712, PlatformGrok)

		gateway.handleGrokAccountUpstreamError(
			context.Background(), account, http.StatusTooManyRequests, http.Header{},
			[]byte(`{"error":{"type":"rate_limit_error","message":"rate limited"}}`),
		)

		require.Equal(t, 1, repo.setErrorCalls)
		require.Contains(t, repo.lastErrorMsg, "Custom error code 429")
	})
}

func TestGrokSubscriptionFailureAutoDisablesInsteadOfCoolingDown(t *testing.T) {
	repo := &rateLimitAccountRepoStub{}
	rateLimitSvc := NewRateLimitService(repo, nil, &config.Config{}, nil, nil)
	gateway := &OpenAIGatewayService{accountRepo: repo, rateLimitService: rateLimitSvc}
	account := &Account{ID: 720, Platform: PlatformGrok, Type: AccountTypeOAuth}

	gateway.handleGrokAccountUpstreamError(
		context.Background(), account, http.StatusForbidden, http.Header{},
		[]byte(`{"error":"subscription required"}`),
	)

	require.Equal(t, 1, repo.setErrorCalls)
	require.Equal(t, 0, repo.tempCalls)
	require.Contains(t, repo.lastErrorMsg, "[subscription_inactive]")
}

func TestRateLimitService_HandleUpstreamError_ShadowTerminalErrorDisablesCredentialOwner(t *testing.T) {
	repo := &rateLimitAccountRepoStub{mockAccountRepoForGemini: mockAccountRepoForGemini{accountsByID: map[int64]*Account{}}}
	svc := NewRateLimitService(repo, nil, &config.Config{}, nil, nil)

	const parentID int64 = 800
	parent := &Account{ID: parentID, Platform: PlatformOpenAI, Type: AccountTypeOAuth}
	repo.accountsByID[parentID] = parent
	shadow := &Account{
		ID:              801,
		Platform:        PlatformOpenAI,
		Type:            AccountTypeOAuth,
		ParentAccountID: terminalPtrInt64(parentID),
		QuotaDimension:  QuotaDimensionSpark,
	}

	shouldDisable := svc.HandleUpstreamError(
		context.Background(),
		shadow,
		http.StatusForbidden,
		http.Header{},
		[]byte(`{"error":{"code":"subscription_expired","message":"Subscription has expired"}}`),
	)

	require.True(t, shouldDisable)
	require.Equal(t, 1, repo.setErrorCalls)
	require.Equal(t, parentID, repo.lastErrorID)
}

func terminalPtrInt64(v int64) *int64 { return &v }

func explicitCustom429Account(id int64, platform string) *Account {
	return &Account{
		ID:       id,
		Platform: platform,
		Type:     AccountTypeAPIKey,
		Credentials: map[string]any{
			"custom_error_codes_enabled": true,
			"custom_error_codes":         []any{float64(http.StatusTooManyRequests)},
		},
	}
}
