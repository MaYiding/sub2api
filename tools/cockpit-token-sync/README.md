# Cockpit Token Sync

This tool synchronizes matching OpenAI OAuth accounts between the local Cockpit encrypted store and the deployed Sub2API instance.

## Safety model

- The server and Cockpit are compared by exact token values in memory; tokens are never logged.
- On first adoption, matching tokens are accepted directly. When they differ, the access-token JWT issue time selects the newer side; equal or unreadable issue times remain a conflict.
- If only the server changed, the current server credentials are pulled into Cockpit.
- If only Cockpit changed, the credentials are applied to the matching server account.
- Local OAuth uploads include `expires_at` derived from the access-token JWT, plus any supported optional OAuth metadata present locally. Server-only credential metadata is preserved during an upload.
- If the tokens already match but server metadata is missing or stale, the tool still repairs the server credential record.
- Generic account updates (usage refreshes, quota probes, notes, and scheduler state) are not treated as credential changes. Server credential changes are tracked with `_token_version`; divergent tokens use JWT issue time as a deterministic tie-breaker.
- The server account and group lists are paginated. A local OAuth account missing on the server is skipped by default; it is created only with the explicit `--import-missing` option, then assigned to the active OpenAI group named `晴天纪` (override with `SUB2API_SYNC_GROUP_NAME`). Existing synced OAuth accounts missing that group have it appended without replacing any other group. Non-OAuth local records are excluded from this OAuth sync.
- The syncer treats `schedulable=false` as an administrator-owned setting: it neither probes nor recovers that account's runtime state, and it avoids the OAuth write path that can clear errors. For schedulable accounts with an `error` or account-level rate-limit state, the sync pass probes upstream quota at most once per minute. A successful, fully available quota response calls the server runtime-recovery endpoint without changing credentials or consuming a reset credit. Model-specific restrictions remain intact.
- If both sides changed and token age cannot establish a newer copy, the tool stops for that account instead of guessing.
- Before a server-to-Cockpit pull, the previous encrypted envelope is saved under `/Volumes/MacData/09_tmp/cockpit-token-sync-backups/`.

The sync process does not remove the underlying refresh-token race if Cockpit and Sub2API independently refresh the same OAuth account at the same instant. Keep the server as the runtime owner and use Cockpit primarily for re-authorization/import; the issue-time guard prevents stale state from being blindly copied over newer state.

Each SSH request has a 40-second hard process deadline, 15-second curl deadlines, SSH keepalive detection, and the whole pass has a 90-second deadline. Override them with `SUB2API_SYNC_REQUEST_TIMEOUT_MS`, `SUB2API_SYNC_CURL_TIMEOUT_SECONDS`, and `SUB2API_SYNC_PASS_TIMEOUT_MS` when diagnosing a slow link.

## Manual run

```sh
node tools/cockpit-token-sync/sync.js --dry-run
node tools/cockpit-token-sync/sync.js
node tools/cockpit-token-sync/sync.js --import-missing
```

The script authenticates to the private container API over the existing SSH key. The Sub2API administrator password is read only on the server from the container environment and is not stored on the Mac.

When the server uses blue-green deployment, each SSH request resolves the
active container and loopback port from `/opt/sub2api/deploy/.blue-green-active`
before logging in. The LaunchAgent does not need to change when ports alternate.

## LaunchAgent

Run `tools/cockpit-token-sync/install-macos.sh` from the checkout. It installs a small wrapper on the internal disk because macOS may reject LaunchAgent scripts and log paths located directly on an external volume. Launchd starts one bounded sync pass every 30 seconds and never overlaps it with another pass; the process exits after every pass so memory and child processes cannot accumulate indefinitely.
