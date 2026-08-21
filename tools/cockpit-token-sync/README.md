# Cockpit Token Sync

This tool synchronizes matching OpenAI OAuth accounts between the local Cockpit encrypted store and the deployed Sub2API instance.

## Safety model

- The server and Cockpit are compared by exact token values in memory; tokens are never logged.
- A first run only adopts an account when both sides already match. A mismatch is reported as a conflict and is not overwritten.
- If only the server changed, the current server credentials are pulled into Cockpit.
- If only Cockpit changed, the credentials are applied to the matching server account.
- Local OAuth uploads include `expires_at` derived from the access-token JWT, plus any supported optional OAuth metadata present locally. Server-only credential metadata is preserved during an upload.
- If the tokens already match but server metadata is missing or stale, the tool still repairs the server credential record.
- If both sides changed since the last successful sync, the tool stops for that account instead of guessing.
- Before a server-to-Cockpit pull, the previous encrypted envelope is saved under `/Volumes/MacData/09_tmp/cockpit-token-sync-backups/`.

The sync process does not remove the underlying refresh-token race if Cockpit and Sub2API independently refresh the same OAuth account at the same instant. Keep the server as the runtime owner and use Cockpit primarily for re-authorization/import; the conflict guard prevents stale state from being blindly copied over newer state.

## Manual run

```sh
node tools/cockpit-token-sync/sync.js --dry-run
node tools/cockpit-token-sync/sync.js
```

The script authenticates to the private container API over the existing SSH key. The Sub2API administrator password is read only on the server from the container environment and is not stored on the Mac.

## LaunchAgent

Run `tools/cockpit-token-sync/install-macos.sh` from the checkout. It installs a small wrapper on the internal disk because macOS may reject LaunchAgent scripts and log paths located directly on an external volume. The wrapper runs one sync pass every 30 seconds.
