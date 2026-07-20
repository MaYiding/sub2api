//go:build unit

package service

import (
	"context"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/stretchr/testify/require"
)

// Regression: ISSUE-001 — recovering an auto-disabled account left scheduling disabled.
// Found by /qa on 2026-07-20.
// Report: .gstack/qa-reports/qa-report-localhost-2026-07-20.md
type recoverySchedulableRepoStub struct {
	rateLimitClearRepoStub
	callOrder           []string
	setSchedulableCalls int
	setSchedulableValue bool
}

func (r *recoverySchedulableRepoStub) SetSchedulable(_ context.Context, _ int64, schedulable bool) error {
	r.callOrder = append(r.callOrder, "set_schedulable")
	r.setSchedulableCalls++
	r.setSchedulableValue = schedulable
	return nil
}

func (r *recoverySchedulableRepoStub) ClearError(_ context.Context, _ int64) error {
	r.callOrder = append(r.callOrder, "clear_error")
	r.clearErrorCalls++
	return nil
}

func TestRateLimitService_RecoverAccountState_RestoresTerminallyDisabledScheduling(t *testing.T) {
	repo := &recoverySchedulableRepoStub{
		rateLimitClearRepoStub: rateLimitClearRepoStub{
			getByIDAccount: &Account{
				ID:          73,
				Status:      StatusError,
				Schedulable: false,
			},
		},
	}
	svc := NewRateLimitService(repo, nil, &config.Config{}, nil, nil)

	result, err := svc.RecoverAccountState(context.Background(), 73, AccountRecoveryOptions{})
	require.NoError(t, err)
	require.NotNil(t, result)
	require.True(t, result.ClearedError)
	require.Equal(t, 1, repo.setSchedulableCalls)
	require.True(t, repo.setSchedulableValue)
	require.Equal(t, []string{"set_schedulable", "clear_error"}, repo.callOrder)
}
