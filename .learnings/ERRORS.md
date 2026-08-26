# Error Log

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
