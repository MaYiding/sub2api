# Learning Log

## [LRN-20260824-001] correction

**Logged**: 2026-08-24T13:52:00+08:00
**Priority**: low
**Status**: resolved
**Area**: backend

### Summary
Do not infer that a feature fix is absent merely because the implementation lacks the feature's literal public tool name.

### Details
The Codex API-key manifest compatibility fix for web search is implemented by changing `use_responses_lite` for the GPT-5.6 model entries. A literal search for `web_search` in the manifest service therefore missed the active fix and briefly produced an incorrect regression hypothesis. `git blame` and direct inspection of `adjustAPIKeyCodexModelsManifest` confirmed the fix is present.

### Suggested Action
For indirect capability switches, inspect the referenced commit's exact symbols and diff those symbols against the current tree before declaring a regression.

### Metadata
- Source: error
- Related Files: backend/internal/service/openai_codex_models_service.go
- Tags: code-search, git-history, codex, web-search

### Resolution
- **Resolved**: 2026-08-24T13:52:00+08:00
- **Notes**: Corrected the user-facing progress update and separated the Responses Lite client-tool issue from the hosted-tool upstream failure.

---
