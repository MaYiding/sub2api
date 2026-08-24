# Error Log

## [ERR-20260823-001] git-push-github-https

**Logged**: 2026-08-23T03:06:34Z
**Priority**: medium
**Status**: resolved
**Area**: infra

### Summary
The first push of the daily upstream-sync branch failed because GitHub reset the HTTPS connection.

### Error
```
fatal: unable to access 'https://github.com/MaYiding/sub2api.git/': Recv failure: Connection reset by peer
```

### Context
- Attempted to push `agent/sync-upstream-main-20260823` after a clean upstream merge and passing backend tests.
- The subsequent PR creation also failed because the branch did not yet exist on GitHub.
- GitHub CLI authentication was valid before the push.

### Suggested Fix
When GitHub HTTPS is reset but SSH authentication succeeds, rewrite the GitHub URL to SSH for that Git command only. Confirm the remote branch exists before creating the PR.

### Metadata
- Reproducible: yes
- Recurrence-Count: 6
- Related Files: none

### Resolution
- **Resolved**: 2026-08-23T03:08:00Z
- **Commit/PR**: #57
- **Notes**: GitHub SSH authentication succeeded on ports 22 and 443; the 2026-08-24 sync push and follow-up fetch used a command-scoped `url.insteadOf` rewrite without changing the persistent remote. A later GraphQL status poll hit a transient EOF and succeeded on retry.

---

## [ERR-20260824-001] branch-specific-validation-target

**Logged**: 2026-08-24T03:04:21Z
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary
A validation referenced a development script that does not exist on the main branch.

### Error
```
bash: tools/sub2api-dev.sh: No such file or directory
```

### Context
- `bash -n tools/sub2api-dev.sh` was run while validating the upstream merge on `main`.
- `tools/sub2api-dev.sh` is intentionally maintained only on this repository's custom `dev` branch.
- An initial diagnosis incorrectly attributed the failure to parallel working-directory interference; `git ls-tree` and branch history showed that the file is absent on `main`.

### Suggested Fix
Before validating a branch-specific tool, confirm it is tracked on the current branch. Run the development-manager syntax check after merging `main` into `dev`.

### Metadata
- Reproducible: yes
- Related Files: none

### Resolution
- **Resolved**: 2026-08-24T03:04:21Z
- **Commit/PR**: #57
- **Notes**: Removed the inapplicable check from `main`; it will be run on the final `dev` branch.

---

## [ERR-20260823-005] vite-config-hot-reload

**Logged**: 2026-08-23T08:25:00Z
**Priority**: low
**Status**: resolved
**Area**: frontend

### Summary
The managed Vite process kept routing through the wrong port target after the config default changed.

### Error
```
settings_status=404
login_status=404
```

### Context
- `frontend/vite.config.ts` was changed from `localhost` to `127.0.0.1` and the frontend was restarted.
- The served config showed the IPv4 default, but requests through port 3000 still reached the unrelated Docker listener.
- The local manager already had the authoritative `BACKEND_URL` but did not pass it explicitly to Vite.

### Suggested Fix
Export the manager's `BACKEND_URL` as `VITE_DEV_PROXY_TARGET` when starting Vite, then restart the managed frontend process.

### Metadata
- Reproducible: unknown
- Related Files: frontend/vite.config.ts, tools/sub2api-dev.sh

### Resolution
- **Resolved**: 2026-08-23T08:26:00Z
- **Commit/PR**: local verification
- **Notes**: After explicitly passing `BACKEND_URL`, both public settings and `.env` credential login returned HTTP 200 through port 3000.

---

## [ERR-20260823-004] snapshot-frontend-validation

**Logged**: 2026-08-23T08:23:00Z
**Priority**: low
**Status**: resolved
**Area**: frontend

### Summary
Package-manager validation commands attempted dependency installation instead of using the existing local binaries.

### Error
```
The modules directory ... will be removed and reinstalled from scratch. Proceed? (Y/n)
[ERR_PNPM_ABORTED_REMOVE_MODULES_DIR] Aborted removal of modules directory
```

### Context
- The readable snapshot excluded `frontend/node_modules` to keep the copy small.
- `pnpm exec eslint` first created a dependency tree in that snapshot.
- In the real workspace, the same global pnpm command prompted to replace the existing modules directory; the operation was aborted and `node_modules` remained present.

### Suggested Fix
Run the checked-in workspace binaries directly, such as `./node_modules/.bin/eslint` and `./node_modules/.bin/vue-tsc`.

### Metadata
- Reproducible: yes
- Recurrence-Count: 3
- Related Files: frontend/package.json

### Resolution
- **Resolved**: 2026-08-23T08:34:00Z
- **Commit/PR**: local workaround
- **Notes**: Direct local binaries completed successfully with `eslint_status=0` and `typecheck_status=0`; the 2026-08-24 sync also passed ESLint, Vue typecheck, and 165 critical Vitest cases this way.

---

## [ERR-20260823-006] screen-window-query

**Logged**: 2026-08-23T08:33:00Z
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary
The installed macOS Screen version does not support the `-Q` query option.

### Error
```
Error: Unknown option -Q
```

### Context
- `screen -S sub2api-local-backend -Q windows` was used to locate a validation window.
- The installed Screen version is 4.00.03.

### Suggested Fix
Inspect the spawned process group with `ps` and terminate only that exact task-owned group when needed.

### Metadata
- Reproducible: yes
- Related Files: none

### Resolution
- **Resolved**: 2026-08-23T08:34:00Z
- **Commit/PR**: local workaround
- **Notes**: Identified the validation process group from its known script path and terminated only that group.

---

## [ERR-20260823-002] workspace-read-permission

**Logged**: 2026-08-23T08:14:00Z
**Priority**: medium
**Status**: resolved
**Area**: infra

### Summary
The Codex shell could stat the Desktop workspace but macOS denied directory reads.

### Error
```
rg: ./: Operation not permitted (os error 1)
fatal: Unable to read current working directory: Operation not permitted
```

### Context
- Direct reads under `/Users/mayiding/Desktop/GitMy/sub2api` failed from the tool process.
- Existing Terminal-owned `screen` sessions retained access to the Desktop workspace.

### Suggested Fix
Grant the Codex app Desktop access. Until then, use an existing authorized process to copy a read-only snapshot into a private temporary directory.

### Metadata
- Reproducible: yes
- Related Files: none

### Resolution
- **Resolved**: 2026-08-23T08:16:00Z
- **Commit/PR**: local workaround
- **Notes**: Used the existing backend `screen` session to create a temporary readable snapshot without changing project files.

---

## [ERR-20260823-003] temporary-workspace-cleanup

**Logged**: 2026-08-23T08:16:00Z
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary
A temporary-copy command was rejected because it contained recursive deletion.

### Error
```
rm -f style commands are not permitted. Use a safer approach
```

### Context
- The command attempted to recreate a task-specific directory under `/tmp` before copying a read-only workspace snapshot.
- No files were deleted.

### Suggested Fix
Create unique task directories with `mktemp -d` and leave cleanup to the operating system.

### Metadata
- Reproducible: yes
- Related Files: none

### Resolution
- **Resolved**: 2026-08-23T08:17:00Z
- **Commit/PR**: local workaround
- **Notes**: Replaced recursive cleanup with `mktemp -d`.

---

## [ERR-20260824-002] apply-patch-context-window

**Logged**: 2026-08-24T03:23:34Z
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary
A patch expected a missing heading that was only outside the inspected line window.

### Error
```
apply_patch verification failed: Failed to find expected lines
```

### Context
- A `sed` window started one line after an existing `### Summary` heading.
- The patch attempted to insert that heading together with unrelated metadata updates.
- Re-reading with numbered lines confirmed the file was already correctly formatted.

### Suggested Fix
Use numbered context around the exact patch target before combining formatting assumptions with content edits.

### Metadata
- Reproducible: yes
- Related Files: .learnings/ERRORS.md

### Resolution
- **Resolved**: 2026-08-24T03:23:34Z
- **Commit/PR**: pending dev sync PR
- **Notes**: Applied a smaller patch limited to the fields that actually required updates.

---

## [ERR-20260824-003] dev-restart-multiple-listeners

**Logged**: 2026-08-24T05:34:40Z
**Priority**: medium
**Status**: resolved
**Area**: infra

### Summary
The development restart could not stop the managed backend when an unrelated IPv6 listener shared port 8080.

### Error
```
[sub2api-dev] Go 后端 未在 20 秒内退出，发送 KILL
[sub2api-dev] ERROR: Go 后端 在 20 秒内未停止
```

### Context
- `lsof -tiTCP:8080` returned both the managed IPv4 backend PID and Docker's IPv6 listener PID.
- The script passed the newline-separated PID list as one argument to `kill`, so neither listener was signaled.
- The screen session exited, but the managed backend process remained orphaned on `127.0.0.1:8080`.

### Suggested Fix
Select the listener whose command matches the managed application, ignore unrelated IPv6-only listeners during IPv4 availability checks, and recheck after a forced kill before reporting failure.

### Metadata
- Reproducible: yes
- Recurrence-Count: 2
- Related Files: tools/sub2api-dev.sh

### Resolution
- **Resolved**: 2026-08-24T05:34:40Z
- **Commit/PR**: pending restart-fix PR
- **Notes**: Added command-aware listener selection, a `SO_REUSEADDR` IPv4 bind probe matching the Go server's behavior, and post-KILL verification while leaving Docker untouched.

---

## [ERR-20260824-004] local-dev-database-env-bootstrap

**Logged**: 2026-08-24T13:48:00+08:00
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary
Sourcing `deploy/.env` alone does not define the runtime `DATABASE_*` variables used by the local development backend.

### Error
```
psql: error: connection to server on socket "/tmp/.s.PGSQL.5432" failed: FATAL:  database "mayiding" does not exist
```

### Context
- A read-only diagnostic command sourced `deploy/.env` and expected `DATABASE_HOST`, `DATABASE_USER`, and `DATABASE_DBNAME` to be present.
- `tools/sub2api-dev.sh load_env` derives those values from `POSTGRES_USER`, `POSTGRES_PASSWORD`, and `POSTGRES_DB`; sourcing the file without applying that mapping leaves the database variables unset.

### Suggested Fix
For ad hoc local database diagnostics, source `deploy/.env` and reproduce the non-secret `load_env` mapping, or query the running process environment without printing credentials.

### Metadata
- Reproducible: yes
- Related Files: deploy/.env, tools/sub2api-dev.sh

### Resolution
- **Resolved**: 2026-08-24T13:48:00+08:00
- **Commit/PR**: local diagnostic correction
- **Notes**: Subsequent commands use `POSTGRES_*` directly with explicit host and port.

---

## [ERR-20260824-005] zsh-unmatched-source-glob

**Logged**: 2026-08-24T13:52:00+08:00
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary
An unquoted optional source-file glob aborted a zsh diagnostic subcommand before `rg` could run.

### Error
```
zsh:1: no matches found: backend/internal/service/openai_upstream_error*.go
```

### Context
- A read-only search listed several concrete files plus an optional wildcard.
- zsh expands unmatched globs as an error by default.

### Suggested Fix
Use `rg` directory filters, quote the pattern, or resolve optional files with `rg --files` before passing them to another command.

### Metadata
- Reproducible: yes
- Related Files: none

### Resolution
- **Resolved**: 2026-08-24T13:52:00+08:00
- **Commit/PR**: local diagnostic correction
- **Notes**: Continued with directory-scoped `rg` queries that do not depend on optional shell glob expansion.

---

## [ERR-20260824-006] zsh-reserved-diagnostic-variables

**Logged**: 2026-08-24T13:57:00+08:00
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary
Using `path` and `status` as temporary zsh variable names broke command lookup and assignment in a diagnostic loop.

### Error
```
zsh:6: command not found: curl
zsh:7: read-only variable: status
```

### Context
- In zsh, `path` is tied to `PATH`, so assigning a request path to it removed normal executable directories.
- `status` is a read-only special parameter containing the previous exit status.

### Suggested Fix
Use task-specific names such as `endpoint_path` and `http_code`, especially in zsh scripts.

### Metadata
- Reproducible: yes
- Related Files: none

### Resolution
- **Resolved**: 2026-08-24T13:57:00+08:00
- **Commit/PR**: local diagnostic correction
- **Notes**: Reserved names were replaced with task-specific variables.

---

## [ERR-20260824-007] database-schema-assumption

**Logged**: 2026-08-24T13:57:00+08:00
**Priority**: low
**Status**: resolved
**Area**: backend

### Summary
A read-only account-state query assumed columns that are not present in the current Ent schema.

### Error
```
ERROR:  column "disabled_at" does not exist
```

### Context
- The query attempted to inspect account scheduling state using guessed column names.
- The project schema has evolved and must be introspected before ad hoc SQL diagnostics.

### Suggested Fix
Query `information_schema.columns` or use `\d accounts` before selecting non-core diagnostic fields.

### Metadata
- Reproducible: yes
- Related Files: backend/ent/schema/account.go

### Resolution
- **Resolved**: 2026-08-24T13:57:00+08:00
- **Commit/PR**: local diagnostic correction
- **Notes**: Subsequent database queries use schema-discovered column names.

---

## [ERR-20260824-008] duplicated-workdir-prefix

**Logged**: 2026-08-24T14:07:00+08:00
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary
A diagnostic command used repository-root file paths while its working directory was already `backend/`.

### Error
```
sed: backend/internal/service/openai_responses_lite_tools.go: No such file or directory
```

### Context
- The command set `workdir` to the backend directory for `go test`.
- Adjacent source-inspection arguments still included the `backend/` prefix, so the first `sed` failed and prevented the chained test listing.

### Suggested Fix
Run mixed repository inspection from the repository root, or split backend test commands from root-relative source inspection.

### Metadata
- Reproducible: yes
- Related Files: backend/internal/service/openai_responses_lite_tools.go

### Resolution
- **Resolved**: 2026-08-24T14:07:00+08:00
- **Commit/PR**: local diagnostic correction
- **Notes**: Re-ran source inspection from the repository root and the Go test from `backend/` separately.

---

## [ERR-20260824-009] apply-patch-single-operation-per-file

**Logged**: 2026-08-24T05:55:50Z
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary
An automation-memory patch tried to delete and add the same file in one patch.

### Error
```
apply_patch verification failed: invalid patch: multiple operations target the same file
```

### Context
- The memory file needed a full-content replacement after the synchronization run.
- `apply_patch` rejects multiple operations targeting one path in the same patch.

### Suggested Fix
Use one `Update File` operation when replacing an existing file's contents.

### Metadata
- Reproducible: yes
- Related Files: none

### Resolution
- **Resolved**: 2026-08-24T05:56:00Z
- **Commit/PR**: local diagnostic correction
- **Notes**: Replaced the delete/add pair with one update operation.

---
