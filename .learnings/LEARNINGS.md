# Learning Log

## [LRN-20260831-001] knowledge_gap

**Logged**: 2026-08-31T11:09:22+08:00
**Priority**: low
**Status**: resolved
**Area**: backend

### Summary
Use `/api/v1/settings/public` for direct and frontend-proxied public-settings health checks.

### Details
The automation memory still referenced `/api/v1/public/settings`, which now returns HTTP 404. The current backend route is `/api/v1/settings/public`; both direct port 8080 and the Vite proxy on port 3000 return HTTP 200 for that path.

### Suggested Action
Keep service connection checks aligned with the registered route or confirm the path from the running access log before treating a 404 as an application failure.

### Metadata
- Source: error
- Related Files: none
- Tags: health-check, routing, frontend-proxy, automation

### Resolution
- **Resolved**: 2026-08-31T11:09:22+08:00
- **Notes**: Re-ran the direct and proxied checks with the current route; both returned HTTP 200.

---

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
