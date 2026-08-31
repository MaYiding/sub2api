# Error Log

## [ERR-20260831-001] git-rev-parse-verify-multiple-revisions

**Logged**: 2026-08-31T11:04:12+08:00
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary
A branch-topology diagnostic passed multiple revisions to `git rev-parse --verify`, which accepts exactly one revision.

### Error
```
fatal: Needed a single revision
```

### Context
- The read-only command tried to verify `origin/main`, `origin/dev`, and `upstream/main` in one invocation.
- Later commands in the same diagnostic continued, but the first ancestry result was invalid because it inherited the failed command's exit status.
- No repository or remote state changed.

### Suggested Fix
Resolve each revision with a separate `git rev-parse` invocation before running ancestry checks.

### Metadata
- Reproducible: yes
- Related Files: none

### Resolution
- **Resolved**: 2026-08-31T11:04:12+08:00
- **Commit/PR**: pending diagnostics PR
- **Notes**: Re-ran each revision lookup separately and confirmed that both fork branches contain `upstream/main`, while `dev` also contains fork `main`.

---

## [ERR-20260830-003] apply-patch-ambiguous-context

**Logged**: 2026-08-30T11:36:00+08:00
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary
A patch used a generic Metadata block as context and added recurrence fields to the wrong error entry.

### Error
```
The recurrence fields appeared under ERR-20260830-001 instead of ERR-20260824-009.
```

### Context
- Many entries in `.learnings/ERRORS.md` contain identical `Reproducible` and `Related Files` lines.
- The post-patch diff exposed the misplaced fields before any commit or push.

### Suggested Fix
Anchor patches for repetitive Markdown logs with the unique entry heading as well as the local field context.

### Metadata
- Reproducible: yes
- Related Files: .learnings/ERRORS.md

### Resolution
- **Resolved**: 2026-08-30T11:36:00+08:00
- **Commit/PR**: pending diagnostics PR
- **Notes**: Removed the fields from the date-path entry and applied them under the uniquely anchored apply-patch entry.

---

## [ERR-20260830-002] conflict-marker-scan-false-positive

**Logged**: 2026-08-30T11:07:19+08:00
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary
A broad conflict-marker scan treated a legitimate source string made of equals signs as an unresolved Git conflict marker.

### Error
```
backend/internal/pkg/antigravity/request_transformer.go:274:===========================================`
```

### Context
- The scan used `^(<<<<<<<|=======|>>>>>>>)`, which also matches longer separator strings.
- `git diff --check` passed, the merge was clean, and the source line was unrelated to the upstream changes.

### Suggested Fix
Match the full Git marker shape: `^<<<<<<< .+`, exactly seven equals signs, or `^>>>>>>> .+`.

### Metadata
- Reproducible: yes
- Related Files: backend/internal/pkg/antigravity/request_transformer.go

### Resolution
- **Resolved**: 2026-08-30T11:07:36+08:00
- **Commit/PR**: pending dev sync PR
- **Notes**: Re-ran the strict marker scan successfully and confirmed upstream is an ancestor of the sync branch.

---

## [ERR-20260830-001] hardcoded-date-command-path

**Logged**: 2026-08-30T11:02:19+08:00
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary
A read-only automation inspection assumed `date` lived at `/usr/bin/date`, but this macOS host exposes it at `/bin/date`.

### Error
```
zsh:2: no such file or directory: /usr/bin/date
```

### Context
- The command was gathering the current run timestamp alongside Git diagnostics.
- All repository reads in the same command completed; no repository or remote state changed.

### Suggested Fix
Resolve standard utilities through `command -v` or invoke `date` through `PATH` instead of hardcoding `/usr/bin/date`.

### Metadata
- Reproducible: yes
- Related Files: none

### Resolution
- **Resolved**: 2026-08-30T11:02:19+08:00
- **Commit/PR**: pending dev maintenance PR
- **Notes**: Confirmed `date` resolves to `/bin/date` and continued with the portable command name.

---

## [ERR-20260829-001] git-push-github-https-reset

**Logged**: 2026-08-29T11:22:41+08:00
**Priority**: medium
**Status**: resolved
**Area**: infra

### Summary
GitHub reset the HTTPS connection while pushing the validated dev-sync branch, so the immediately following PR creation could not find the head ref.

### Error
```
fatal: unable to access 'https://github.com/MaYiding/sub2api.git/': Recv failure: Connection reset by peer
pull request create failed: GraphQL: Head sha can't be blank, Base sha can't be blank, No commits between dev and agent/sync-main-into-dev-20260829, Head ref must be a branch
```

### Context
- The branch was clean and had passed the complete local backend and frontend suites.
- The push failed before any remote ref was created; the PR command therefore had no valid head branch.
- The repository had previously recovered from the same GitHub HTTPS reset by pushing over SSH port 22.

### Suggested Fix
Retry with an explicit SSH repository URL and exact refspec, then create the PR only after confirming the remote branch exists. A one-shot `remote.<name>.url` config override did not bypass the configured HTTPS URL on this host.

### Metadata
- Reproducible: intermittent
- Related Files: none
- See Also: ERR-20260823-001
- Recurrence-Count: 2
- First-Seen: 2026-08-29
- Last-Seen: 2026-08-30

### Resolution
- **Resolved**: 2026-08-29T11:23:26+08:00
- **Commit/PR**: current dev-sync PR
- **Notes**: The same commit pushed successfully over SSH port 22; the remote branch was confirmed before retrying PR creation. On 2026-08-30, both origin and upstream HTTPS fetches reset again; explicit SSH URLs with exact refspecs refreshed both tracking branches without changing configured remote URLs.

---

## [ERR-20260828-002] stale-duplicate-delete-batch

**Logged**: 2026-08-28T16:24:27+08:00
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary
A duplicate-file cleanup patch used a previously enumerated list after another workspace process had already removed one target.

### Error
```
apply_patch verification failed: Failed to read .learnings/LEARNINGS 2.md: No such file or directory
```

### Context
- Nine numbered duplicate files were enumerated and verified against their canonical originals.
- Before the delete patch ran, at least one target disappeared and the active branch also changed, indicating concurrent workspace activity.
- The patch failed atomically before the subsequent Git fetch commands ran.

### Suggested Fix
Re-enumerate numbered duplicates immediately before deletion and avoid batching stale targets when concurrent workspace activity is detected.

### Metadata
- Reproducible: unknown
- Related Files: .learnings/LEARNINGS 2.md

### Resolution
- **Resolved**: 2026-08-28T16:24:27+08:00
- **Commit/PR**: current dev-sync PR
- **Notes**: Rechecked the live worktree and resumed from a clean branch without carrying conflict markers forward.

---

## [ERR-20260828-001] parallel-diagnostic-command-omission

**Logged**: 2026-08-28T11:02:18+08:00
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary
A parallel diagnostic tuple stored only the arguments for a Git command, so zsh tried to execute `status` directly.

### Error
```
zsh:1: command not found: status
```

### Context
- A JavaScript orchestration tuple used `git` as the result label and `status --short ...` as the shell command.
- The shell call therefore omitted the `git` executable.
- The failed command was read-only; no repository or remote state changed.

### Suggested Fix
Keep display labels separate from complete, independently executable command strings in parallel diagnostic arrays.

### Metadata
- Reproducible: yes
- Related Files: none

### Resolution
- **Resolved**: 2026-08-28T11:02:18+08:00
- **Commit/PR**: pending dev maintenance PR
- **Notes**: Re-ran the inspection with an explicit `git status` command and confirmed a clean `dev` worktree.

---

## [ERR-20260826-004] testcontainers-reaper-name-collision

**Logged**: 2026-08-26T08:02:26Z
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
The local integration suite hit a transient Testcontainers reaper-name collision while Go test packages were running concurrently.

### Error
```
Error response from daemon: Conflict. The container name "/reaper_9cad487e74debd6dd449d663840bb909810cd2f88d993c371ebc2c924d9ff6db" is already in use
```

### Context
- Ran `go test -tags=integration ./...` after the tagged unit suite passed.
- `TestRateLimiterSetsTTLAndDoesNotRefresh` failed before its assertions while creating the Redis test container.
- The named reaper container had already disappeared by the time Docker was inspected, confirming it was transient test infrastructure rather than a persistent application container.

### Suggested Fix
Rerun the integration packages serially with `go test -p 1 -tags=integration ./...` after confirming the transient reaper container is gone.

### Metadata
- Reproducible: unknown
- Related Files: backend/internal/middleware/rate_limiter_integration_test.go, backend/Makefile

### Resolution
- **Resolved**: 2026-08-26T08:04:35Z
- **Commit/PR**: pending dev sync PR
- **Notes**: Confirmed the transient reaper was already gone; the full integration suite passed with package concurrency limited to one.

---

## [ERR-20260827-001] functions-exec-object-literal-quote

**Logged**: 2026-08-27T03:04:07Z
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary
A malformed quote in a `functions.exec` JavaScript object prevented a GitHub label query from running.

### Error
```
SyntaxError: Unexpected identifier 'max_output_tokens'
```

### Context
- The `workdir` string ended with an extra quote before the `yield_time_ms` property.
- The nested command was not invoked, so no repository or GitHub state changed.

### Suggested Fix
Keep tool-call object properties on separate lines and verify string delimiters before submitting JavaScript orchestration code.

### Metadata
- Reproducible: yes
- Related Files: none

### Resolution
- **Resolved**: 2026-08-27T03:04:07Z
- **Commit/PR**: #70
- **Notes**: Corrected the object literal, reran the label query, and added the available automation labels.

---

## [ERR-20260826-003] go-unit-transient-import-open

**Logged**: 2026-08-26T07:56:29Z
**Priority**: low
**Status**: resolved
**Area**: backend

### Summary
A local tagged Go unit-test build transiently failed to open imports, including the standard-library `fmt` package, while frontend dependency reconstruction was running in parallel.

### Error
```
could not import fmt (open : no such file or directory)
could not import github.com/Wei-Shaw/sub2api/ent/user (open : no such file or directory)
```

### Context
- Ran `GOTOOLCHAIN=auto make test-unit test-integration` after the latest upstream refresh.
- Most packages passed; only `internal/service_test` failed during compilation before its assertions ran.
- The untagged full Go suite had passed earlier, and GitHub CI for the same branch was still running without this compiler error.

### Suggested Fix
Rerun the tagged Go suites serially after the concurrent dependency reconstruction finishes; if the failure recurs, inspect and clean only the Go build cache before retrying.

### Metadata
- Reproducible: unknown
- Related Files: backend/internal/service/auth_service_email_bind_test.go, backend/Makefile

### Resolution
- **Resolved**: 2026-08-26T08:04:35Z
- **Commit/PR**: pending dev sync PR
- **Notes**: A serial rerun completed the full tagged unit suite, including `internal/service`, without cleaning caches or changing code.

---

## [ERR-20260826-002] pnpm-non-tty-modules-rebuild-recurrence

**Logged**: 2026-08-26T07:55:37Z
**Priority**: medium
**Status**: resolved
**Area**: frontend

### Summary
Frontend validation again stopped before tests because pnpm required confirmation to rebuild `node_modules` after duplicate dependency directories were removed.

### Error
```
[ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY] Aborted removal of modules directory due to no TTY
[ERR_PNPM_LOCKFILE_CONFIG_MISMATCH] Cannot proceed with the frozen installation.
```

### Context
- Ran `make test-frontend` after deleting 805 system-created dependency duplicates with numbered suffixes.
- The pnpm wrapper detected that the remaining modules directory needed rebuilding and refused the non-interactive purge.
- Retrying with global pnpm 11.5.0 and `CI=true` rebuilt the directory but ignored package-level overrides, so its frozen-lockfile check disagreed with the pnpm 9 lockfile.
- No frontend lint, typecheck, or Vitest assertion had run or failed yet.

### Suggested Fix
Use the CI-compatible pnpm 9.15.9 explicitly with `CI=true` for the frozen-lockfile install/rebuild and subsequent non-interactive frontend validation.

### Metadata
- Reproducible: yes
- Related Files: frontend/package.json, frontend/pnpm-lock.yaml, Makefile
- See Also: ERR-20260825-001

### Resolution
- **Resolved**: 2026-08-26T08:04:35Z
- **Commit/PR**: pending dev sync PR
- **Notes**: Rebuilt dependencies with `CI=true npx --yes pnpm@9.15.9 --dir frontend install --frozen-lockfile`; ESLint, Vue typecheck, and all 245 Vitest files / 1746 tests passed.

---

## [ERR-20260826-001] zsh-unmatched-root-glob

**Logged**: 2026-08-26T07:33:00Z
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary
A read-only configuration search stopped early because zsh rejected unmatched root-level Docker Compose globs.

### Error
```
zsh:6: no matches found: docker-compose*.yml
```

### Context
- Searched project manifests and common port declarations after merging upstream `main`.
- Docker Compose files live under `deploy/`, so the root-level glob had no matches.
- Commands before the unmatched glob completed; the merge and working tree were unaffected.

### Suggested Fix
Use `find`/`rg --files` to enumerate optional files, or enable a null-glob locally instead of passing unmatched globs to zsh.

### Metadata
- Reproducible: yes
- Related Files: deploy/docker-compose.dev.yml, deploy/docker-compose.local.yml
- Recurrence-Count: 2
- First-Seen: 2026-08-26
- Last-Seen: 2026-08-30

### Resolution
- **Resolved**: 2026-08-26T07:33:00Z
- **Commit/PR**: pending sync PR
- **Notes**: Continued discovery with `find`-resolved paths and avoided optional shell globs. The same root-level Compose glob recurred on 2026-08-30; subsequent scans enumerate candidate files with `rg --files` or `find` first.

---

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
- Recurrence-Count: 12
- Last-Seen: 2026-08-31
- Related Files: none

### Resolution
- **Resolved**: 2026-08-23T03:08:00Z
- **Commit/PR**: #57
- **Notes**: GitHub SSH authentication succeeded on ports 22 and 443; the 2026-08-24 through 2026-08-28 sync pushes/fetches and branch cleanup used a command-scoped `url.insteadOf` rewrite without changing the persistent remote. On 2026-08-30 and 2026-08-31, explicit SSH repository URLs and exact refspecs refreshed origin and upstream after HTTPS resets. GraphQL polling and one SSH push have also hit transient connection resets; retrying continues safely without changing repository state.

---

## [ERR-20260825-001] pnpm-non-tty-modules-rebuild

**Logged**: 2026-08-25T03:03:45Z
**Priority**: low
**Status**: resolved
**Area**: frontend

### Summary
Frontend validation could not start because pnpm required a non-interactive `node_modules` rebuild.

### Error
```
[ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY] Aborted removal of modules directory due to no TTY
```

### Context
- Ran the frontend lint, typecheck, Vitest, and build commands after merging the latest upstream `main`.
- The existing `node_modules` metadata was produced by a different pnpm version, so the current pnpm wrapper attempted an implicit install and refused to purge dependencies without a TTY.

### Suggested Fix
Run `CI=true pnpm install --frozen-lockfile` explicitly before non-interactive frontend validation when the pnpm version or modules metadata has changed.

### Metadata
- Reproducible: yes
- Related Files: frontend/package.json, frontend/pnpm-lock.yaml

### Resolution
- **Resolved**: 2026-08-25T03:07:45Z
- **Commit/PR**: #62 validation
- **Notes**: Ran frontend validation through `npx pnpm@9.15.9`; ESLint, Vue typecheck, and all 244 Vitest files / 1744 tests passed without rebuilding dependencies.

---

## [ERR-20260825-002] missing-golangci-lint-binary

**Logged**: 2026-08-25T03:13:00Z
**Priority**: low
**Status**: resolved
**Area**: backend

### Summary
The backend `make test` target completed all Go tests but could not start its lint phase because `golangci-lint` was not installed locally.

### Error
```
make: golangci-lint: No such file or directory
make: *** [test] Error 1
```

### Context
- The repository CI pins golangci-lint v2.13.
- `go test ./...` passed before Make reached the missing binary.

### Suggested Fix
For automation hosts without a global install, run the CI-pinned linter with `go run github.com/golangci/golangci-lint/v2/cmd/golangci-lint@v2.13.0 run ./...`.

### Metadata
- Reproducible: yes
- Related Files: backend/Makefile, .github/workflows/backend-ci.yml

### Resolution
- **Resolved**: 2026-08-25T03:14:30Z
- **Commit/PR**: #62 validation
- **Notes**: The CI-pinned v2.13.0 linter completed with 0 issues; GitHub's golangci-lint checks also passed.

---

## [ERR-20260825-003] concurrent-sync-merge-state

**Logged**: 2026-08-25T03:37:30Z
**Priority**: medium
**Status**: resolved
**Area**: infra

### Summary
A parallel synchronization process changed branches and left a stale main-to-dev merge in progress while the upstream refresh was still advancing.

### Error
```
fatal: cannot switch branch while merging
```

### Context
- The pending merge targeted an older `origin/main` while upstream had already advanced again.
- Read-only inspection confirmed the merge had no manual conflict-resolution work to preserve.
- The stale merge was aborted only after verifying the branch HEAD, MERGE_HEAD, and worktree state.

### Suggested Fix
Before each branch transition in a shared automation workspace, inspect `git status`, `MERGE_HEAD`, and active Git processes; abort only a verified stale, uncommitted merge and restart from refreshed remote refs.

### Metadata
- Reproducible: unknown
- Related Files: none

### Resolution
- **Resolved**: 2026-08-25T03:37:30Z
- **Commit/PR**: pending dev sync PR
- **Notes**: Aborted the stale merge, refreshed and merged main via PRs #63-#64, then restarted the main-to-dev merge from the final origin/main.

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
- Recurrence-Count: 2
- Last-Seen: 2026-08-27
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
- Recurrence-Count: 3
- First-Seen: 2026-08-24
- Last-Seen: 2026-08-30

### Resolution
- **Resolved**: 2026-08-24T05:56:00Z
- **Commit/PR**: local diagnostic correction
- **Notes**: Replaced the delete/add pair with one update operation. The same pattern recurred during the 2026-08-29 and 2026-08-30 automation-memory updates; both failed atomically and were corrected with a single update operation. The prevention rule is also retained in automation memory.

---
