//go:build unit

package service

import (
	"context"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/stretchr/testify/require"
)

// Regression: ISSUE-002 — the first read of an unset optional Web Search
// configuration returned 404 while the cached second read returned 200.
// Found by /qa on 2026-07-20.
// Report: .gstack/qa-reports/qa-report-localhost-2026-07-20.md
type missingWebSearchConfigRepoStub struct {
	settingWeChatRepoStub
	getValueCalls int
}

func (r *missingWebSearchConfigRepoStub) GetValue(_ context.Context, _ string) (string, error) {
	r.getValueCalls++
	return "", ErrSettingNotFound
}

func TestSettingService_GetWebSearchEmulationConfig_MissingSettingUsesStableDefault(t *testing.T) {
	webSearchEmulationCache.Store(&cachedWebSearchEmulationConfig{expiresAt: 0})
	webSearchEmulationSF.Forget(sfKeyWebSearchConfig)
	defer func() {
		webSearchEmulationCache.Store(&cachedWebSearchEmulationConfig{expiresAt: 0})
		webSearchEmulationSF.Forget(sfKeyWebSearchConfig)
	}()

	repo := &missingWebSearchConfigRepoStub{}
	svc := NewSettingService(repo, &config.Config{})

	first, err := svc.GetWebSearchEmulationConfig(context.Background())
	require.NoError(t, err)
	require.NotNil(t, first)
	require.False(t, first.Enabled)
	require.Empty(t, first.Providers)

	second, err := svc.GetWebSearchEmulationConfig(context.Background())
	require.NoError(t, err)
	require.NotNil(t, second)
	require.False(t, second.Enabled)
	require.Empty(t, second.Providers)
	require.Equal(t, 1, repo.getValueCalls, "the stable empty default should be cached")
}
