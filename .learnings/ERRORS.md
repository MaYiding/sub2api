# Error Log

## [ERR-20260911-002] chained-merge-used-parent-checkout

**Logged**: 2026-09-11T11:10:42+08:00
**Priority**: high
**Status**: resolved
**Area**: infra

### Summary
After creating an isolated worktree in a chained shell command, the following `git merge` ran in the parent checkout and advanced local `dev` unexpectedly.

### Error
```
dev advanced from 4f5c4fa33 to 929ddcb6c via merge upstream/main
```

### Context
- `git worktree add ... && git merge ...` was executed with the parent checkout as the command working directory.
- The new worktree was created correctly, but the chained merge inherited the parent directory instead of entering the new worktree.
- The remote `origin/dev` was not changed; the accidental merge commit was retained before correction.

### Suggested Fix
Use `git -C <worktree> merge ...` or a separate command with an explicit worktree working directory after every worktree creation.

### Metadata
- Reproducible: yes
- Related Files: .git/worktrees, .learnings/ERRORS.md

### Resolution
- **Resolved**: 2026-09-11T11:14:00+08:00
- **Commit/PR**: current synchronization run
- **Notes**: Saved the accidental merge under a backup branch and restored local `dev` to `origin/dev` before continuing in the isolated worktree.

---

## [ERR-20260911-001] git-command-scoped-remote-url-override

**Logged**: 2026-09-11T11:05:23+08:00
**Priority**: medium
**Status**: resolved
**Area**: infra

### Summary
Setting `remote.<name>.url` with `git -c` did not override the configured HTTPS URL for fetches in this checkout.

### Error
```
fatal: unable to access 'https://github.com/Wei-Shaw/sub2api.git/': LibreSSL SSL_connect: SSL_ERROR_SYSCALL
fatal: unable to access 'https://github.com/MaYiding/sub2api.git/': LibreSSL SSL_connect: SSL_ERROR_SYSCALL
```

### Context
- The checkout has HTTPS `origin` and `upstream` remotes and recurring HTTPS/TLS failures.
- A parallel fetch attempt used command-scoped `remote.origin.url` and `remote.upstream.url` values, but Git still contacted the configured HTTPS endpoints.
- No ref or remote configuration changed.

### Suggested Fix
Use command-scoped `url.<ssh-url>.insteadOf` rewrites (or an explicit temporary repository URL) and verify the resulting refs after fetch.

### Metadata
- Reproducible: yes
- Related Files: .git/config, .learnings/ERRORS.md

### Resolution
- **Resolved**: 2026-09-11T11:07:00+08:00
- **Commit/PR**: current synchronization run
- **Notes**: Refreshed both remotes successfully with the temporary VPN HTTP proxy; no remote or global Git configuration was changed.

---

## [ERR-20260909-001] upstream-provider-count-assertion

**Logged**: 2026-09-09T11:22:34+08:00
**Priority**: low
**Status**: resolved
**Area**: frontend

### Summary
An upstream Grok monitor test hard-coded eight provider buttons and failed after the fork added MiniMax as a ninth provider.

### Error
```
expected providerButtons to have a length of 8 but got 9
```

### Context
- The upstream test correctly validated Grok availability and defaults but assumed the upstream-only provider catalog size.
- The merged fork catalog legitimately includes MiniMax, so the rendered grid and `PROVIDERS` both contain nine entries.

### Suggested Fix
Assert the rendered button count against the shared `PROVIDERS.length` source of truth while keeping provider-specific assertions explicit.

### Metadata
- Reproducible: yes
- Related Files: frontend/src/views/admin/__tests__/ChannelMonitorView.grok.spec.ts, frontend/src/constants/channelMonitor.ts

### Resolution
- **Resolved**: 2026-09-09T11:23:10+08:00
- **Commit/PR**: follow-up compatibility PR
- **Notes**: Replaced the literal count with `PROVIDERS.length`; the focused Grok test passed.

---

## [ERR-20260902-001] git-rm-ignored-residual-file

**Logged**: 2026-09-02T11:20:00+08:00
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary
`git rm` could not delete a `.DS_Store` file that an upstream merge had already removed from the index but left on disk as an ignored residual file.

### Error
```
fatal: pathspec '.DS_Store' did not match any files
```

### Context
- The pre-merge branch tracked the root `.DS_Store`, while upstream had already deleted it.
- The merge adopted the index deletion, but the ignored working-tree file remained physically present.
- A clean `git status` therefore did not imply that the ignored file was absent.

### Suggested Fix
Check both `git ls-files --error-unmatch <path>` and filesystem presence before choosing `git rm` versus ordinary ignored-file cleanup.

### Metadata
- Reproducible: yes
- Related Files: .DS_Store, .gitignore

### Resolution
- **Resolved**: 2026-09-02T11:20:00+08:00
- **Commit/PR**: current upstream-sync PR
- **Notes**: Reclassified the residual as ignored local garbage and removed it with an ordinary filesystem deletion.

---

## [ERR-20260901-001] pnpm-non-tty-modules-rebuild

**Logged**: 2026-09-01T11:05:00+08:00
**Priority**: low
**Status**: resolved
**Area**: frontend

### Summary
The root frontend build could not start because pnpm attempted to rebuild `node_modules` in a non-interactive shell.

### Error
```
[ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY] Aborted removal of modules directory due to no TTY
```

### Context
- Ran `make build` after merging the latest upstream `main`.
- Backend compilation completed successfully; the frontend target's implicit `pnpm install` was blocked before Vite/Vue compilation.

### Suggested Fix
Run `CI=true pnpm install --frozen-lockfile` explicitly (or invoke the repository-pinned pnpm version) before non-interactive frontend validation.

### Metadata
- Reproducible: yes
- Related Files: frontend/package.json, frontend/pnpm-lock.yaml
- See Also: ERR-20260825-001

### Resolution
- **Resolved**: 2026-09-01T11:12:00+08:00
- **Commit/PR**: #79 validation
- **Notes**: Ran `CI=true npx pnpm@9.15.9 --dir frontend install --frozen-lockfile`, then the frontend production build completed successfully.

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
- **Commit/PR**: current upstream-sync PR
- **Notes**: Rechecked the live worktree and resumed from a clean branch without carrying conflict markers forward.

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

### Resolution
- **Resolved**: 2026-08-26T07:33:00Z
- **Commit/PR**: pending sync PR
- **Notes**: Continued discovery with `find`-resolved paths and avoided optional shell globs.

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
- Recurrence-Count: 5
- Related Files: none

### Resolution
- **Resolved**: 2026-08-23T03:08:00Z
- **Commit/PR**: pending sync PR
- **Notes**: GitHub SSH authentication succeeded on ports 22 and 443; the 2026-08-24 and 2026-08-25 sync pushes used a command-scoped `url.insteadOf` rewrite without changing the persistent remote.

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
- **Commit/PR**: pending sync PR
- **Notes**: Removed the inapplicable check from `main`; it will be run on the final `dev` branch.

---

## [ERR-20260913-001] gh-pr-create-wrapper-quoting

**Logged**: 2026-09-13T11:07:41+08:00
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary
The first GitHub PR creation call was rejected by the local tool wrapper before shell execution because a multi-line body was embedded directly in a JavaScript string.

### Error
```text
Script error:
SyntaxError: Invalid or unexpected token
```

### Context
- `gh pr create` was invoked through the orchestration wrapper with literal newlines in the `--body` argument.
- No shell command ran and no remote state changed; the sync branch had already been pushed successfully.

### Suggested Fix
Use a single-line shell argument or an apply-patched body file when invoking multi-line GitHub CLI content through the wrapper.

### Metadata
- Reproducible: yes
- Related Files: none

### Resolution
- **Resolved**: 2026-09-13T11:08:00+08:00
- **Commit/PR**: pending sync PR
- **Notes**: Retried with a single-line body argument.

---

## [ERR-20260913-002] gh-pr-merge-short-head-sha

**Logged**: 2026-09-13T11:20:15+08:00
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary
GitHub rejected the first PR merge request because the expected head commit was supplied as a short SHA.

### Error
```text
GraphQL: Variable $input of type MergePullRequestInput! was provided invalid value for expectedHeadOid (Could not coerce value "a55f1bcd8" to GitObjectID)
```

### Context
- `gh pr merge 102 --match-head-commit a55f1bcd8` was rejected before merge evaluation.
- No remote merge or branch deletion occurred.

### Suggested Fix
Resolve and pass the full 40-character head SHA to `--match-head-commit`.

### Metadata
- Reproducible: yes
- Related Files: none

### Resolution
- **Resolved**: 2026-09-13T11:21:00+08:00
- **Commit/PR**: pending sync PR
- **Notes**: Retried with the full head SHA.

---

## [ERR-20260915-001] empty-apply-patch-probe

**Logged**: 2026-09-15T11:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary
An empty `apply_patch` probe was rejected before any repository change.

### Error
```text
patch rejected: empty patch
```

### Context
- The automation invoked `apply_patch` with only the begin/end markers before creating the sync worktree.
- The patch tool correctly rejected the no-op input; no file or Git state changed.

### Suggested Fix
Call `apply_patch` only for concrete file edits; Git merge and worktree operations do not require an empty patch preflight.

### Metadata
- Reproducible: yes
- Related Files: none

### Resolution
- **Resolved**: 2026-09-15T11:00:00+08:00
- **Commit/PR**: pending sync PR
- **Notes**: Continued with the intended Git worktree and merge commands directly.

---

## [ERR-20260915-002] dev-only-patch-anchor-on-main

**Logged**: 2026-09-15T11:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary
A diagnostics append used a `dev`-only context line while editing a branch based on `main`.

### Error
```text
apply_patch verification failed: Failed to find expected lines
```

### Context
- The expected anchor came from the original `dev` checkout's newer `.learnings/ERRORS.md`.
- The isolated upstream-sync worktree is based on `origin/main`, whose fork diagnostics history is intentionally older.
- The failed patch made no file changes.

### Suggested Fix
Read the target worktree's file before patching branch-specific diagnostics instead of reusing context from another branch.

### Metadata
- Reproducible: yes
- Related Files: .learnings/ERRORS.md

### Resolution
- **Resolved**: 2026-09-15T11:00:00+08:00
- **Commit/PR**: pending sync PR
- **Notes**: Read the main worktree file tail and reapplied the append using its actual final entry.

---

## [ERR-20260915-003] chained-worktree-command-kept-parent-cwd

**Logged**: 2026-09-15T11:05:00+08:00
**Priority**: high
**Status**: resolved
**Area**: infra

### Summary
A merge chained after `git worktree add` still ran in the original checkout and temporarily advanced local `dev`.

### Error
```text
## dev...origin/dev [ahead 45]
```

### Context
- `git worktree add <path> ... && git merge ...` does not change the shell working directory after creating the worktree.
- The merge was clean, remained local, and was detected before any push.
- The accidental commit was preserved on `backup/accidental-dev-sync-20260915` before restoring `dev` to `origin/dev`.

### Suggested Fix
Run every isolated-worktree command with that worktree as the command runner's explicit `workdir`, or use `git -C <worktree> ...`.

### Metadata
- Reproducible: yes
- Related Files: none

### Resolution
- **Resolved**: 2026-09-15T11:05:00+08:00
- **Commit/PR**: pending sync PR
- **Notes**: Preserved the accidental commit, restored local `dev` exactly, and continued only in the isolated worktree.

---

## [ERR-20260915-004] expanded-short-sha-by-guessing

**Logged**: 2026-09-15T11:05:00+08:00
**Priority**: medium
**Status**: resolved
**Area**: infra

### Summary
The first recovery branch command used an invalid guessed expansion of a short commit SHA.

### Error
```text
fatal: not a valid branch point
```

### Context
- A 9-character displayed SHA was incorrectly extended instead of resolving the exact object ID.
- Git rejected the branch command before any branch, checkout, or working-tree change.

### Suggested Fix
Always obtain the full object ID with a separate `git rev-parse <ref>` call before using exact-SHA recovery operations.

### Metadata
- Reproducible: yes
- Related Files: none

### Resolution
- **Resolved**: 2026-09-15T11:05:00+08:00
- **Commit/PR**: pending sync PR
- **Notes**: Resolved the real 40-character HEAD, created the backup branch, and restored `dev` successfully.

---

## [ERR-20260915-005] duplicate-scan-masked-awk-failure

**Logged**: 2026-09-15T11:07:00+08:00
**Priority**: high
**Status**: resolved
**Area**: infra

### Summary
An invalid awk regular expression failed inside command substitution while the wrapper still printed a false duplicate-scan success line.

### Error
```text
awk: nonterminated character class
duplicate_artifacts=none
```

### Context
- The awk regex used an unescaped slash inside a slash-delimited character class.
- The failing command was inside an assignment, and the subsequent empty-string check allowed the wrapper to exit successfully.
- No files were changed or deleted.

### Suggested Fix
Use `git ls-files` piped to `rg` with an explicit no-match allowance, and do not let a producer failure be interpreted as an empty successful result.

### Metadata
- Reproducible: yes
- Related Files: none

### Resolution
- **Resolved**: 2026-09-15T11:07:00+08:00
- **Commit/PR**: pending sync PR
- **Notes**: Replaced the awk expression and reran the bounded Git-index duplicate scan successfully.

---

## [ERR-20260916-001] govulncheck-grpc-1-82-1

**Logged**: 2026-09-16T11:20:00+08:00
**Priority**: high
**Status**: resolved
**Area**: security

### Summary
GitHub Security Scan rejected upstream sync PR #108 because the merged dependency graph directly reached two vulnerabilities in `google.golang.org/grpc v1.82.1`.

### Error
```text
Vulnerability #1: GO-2026-6443 (fixed in google.golang.org/grpc@v1.82.2)
Vulnerability #2: GO-2026-6348 (fixed in google.golang.org/grpc@v1.83.1)
Your code is affected by 2 vulnerabilities from 1 module.
```

### Context
- PR: #108, `chore: sync upstream main (2026-09-16)`
- Failed runs: push Security Scan `35050257171`; pull-request Security Scan `35050262034`
- CI tests, frontend, shell, and lint jobs passed for the same commit.

### Suggested Fix
Upgrade the direct backend requirement and checksums to `google.golang.org/grpc v1.83.1`, then rerun the remote Security Scan.

### Metadata
- Reproducible: yes
- Related Files: `backend/go.mod`, `backend/go.sum`
- See Also: none

### Resolution
- **Resolved**: 2026-09-16T11:20:00+08:00
- **Commit/PR**: pending CI rerun for PR #108
- **Notes**: Bumped gRPC to v1.83.1 using checksums returned by sum.golang.org. No local compile, build, or test was run.

---

## [ERR-20260916-002] govulncheck-grpc-1-83-1

**Logged**: 2026-09-16T11:45:00+08:00
**Priority**: high
**Status**: in_progress
**Area**: security

### Summary
The refreshed GitHub Security Scan database still reported `GO-2026-6443` against the initial gRPC remediation version `v1.83.1`.

### Error
```text
Vulnerability #1: GO-2026-6443
Module: google.golang.org/grpc
Found in: google.golang.org/grpc@v1.83.1
Fixed in: google.golang.org/grpc@v1.83.2
```

### Context
- PR: #108, latest remediation commit `2973ee725`
- Failed runs: push Security Scan `35052592429`; pull-request Security Scan `35052590392`
- The canonical module graph, CI test, frontend, shell, and lint changes from the previous remediation were otherwise accepted or still running.

### Suggested Fix
Upgrade the direct backend requirement and checksums to `google.golang.org/grpc v1.83.2`, then regenerate the Go 1.27 module graph and rerun remote CI/security checks.

### Metadata
- Reproducible: yes
- Related Files: `backend/go.mod`, `backend/go.sum`
- See Also: ERR-20260916-001

---
