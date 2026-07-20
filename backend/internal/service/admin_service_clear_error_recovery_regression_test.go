//go:build unit

package service

import (
	"context"
	"testing"

	"github.com/stretchr/testify/require"
)

// Regression: ISSUE-001 — clear-error did not reverse SetError's scheduling disable.
// Found by /qa on 2026-07-20.
// Report: .gstack/qa-reports/qa-report-localhost-2026-07-20.md
type clearErrorSchedulingRepoStub struct {
	accountRepoStubForClearAccountError
	setSchedulableCalls int
	setSchedulableValue bool
}

func (r *clearErrorSchedulingRepoStub) SetSchedulable(_ context.Context, _ int64, schedulable bool) error {
	r.setSchedulableCalls++
	r.setSchedulableValue = schedulable
	r.account.Schedulable = schedulable
	return nil
}

func TestAdminService_ClearAccountError_RestoresScheduling(t *testing.T) {
	repo := &clearErrorSchedulingRepoStub{
		accountRepoStubForClearAccountError: accountRepoStubForClearAccountError{
			account: &Account{
				ID:          75,
				Status:      StatusError,
				Schedulable: false,
			},
		},
	}
	svc := &adminServiceImpl{accountRepo: repo}

	updated, err := svc.ClearAccountError(context.Background(), 75)
	require.NoError(t, err)
	require.NotNil(t, updated)
	require.Equal(t, 1, repo.setSchedulableCalls)
	require.True(t, repo.setSchedulableValue)
	require.True(t, updated.Schedulable)
	require.Equal(t, StatusActive, updated.Status)
}
