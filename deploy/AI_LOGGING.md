# AI log capture

This optional extension records traffic observed by the authenticated relay.
It is disabled by default. It does not expose a new public sub2api API and does
not change model routing, billing, token synchronization or retry policy.

## Source configuration

Pass these variables to **each** blue/green app container and mount a dedicated
local directory at `/app/ai-log-spool`, writable by the app UID (1000):

```dotenv
AI_LOG_ENABLED=true
AI_LOG_SOURCE_ID=sub2api-production
AI_LOG_SPOOL_DIR=/app/ai-log-spool
AI_LOG_SPOOL_MAX_BYTES=21474836480
```

The spool is separate from the application database. Files/directories are
0600/0700 and contain private inference traffic. Run one shipper for the shared
spool with the same UID, and give both blue/green app containers the same source
ID. Backlog remains usable across deployments. Never remove the spool during a
blue/green rollout.

Create a mode-0600 shipper JSON configuration outside Git:

```json
{
  "bootstrap_servers": "kafka.infra.qingtianji.com:443",
  "username": "ingest-default",
  "password": "REPLACE_FROM_PRIVATE_CONNECTION_BUNDLE",
  "topic": "ai.raw.default.v1",
  "source_id": "sub2api-production",
  "spool_dir": "/app/ai-log-spool"
}
```

Install `confluent-kafka==2.12.0` into a Python 3.12 venv from a prebuilt wheel,
then run `tools/ai-log-pipeline/shipper.py --config /etc/sub2api-ai-log/shipper.json`
under a restarting systemd service. Use normal CA verification; the public
listener uses SASL/SCRAM over TLS. The ingest principal can write only its raw
topic. Credentials are not included in this repository.

## Capture contract

- HTTP gateway request bytes, delivered HTTP/SSE response bytes, and each
  upstream HTTP attempt (including retries) are recorded. SSE flush, request
  cancellation and upstream body limits retain their original behavior.
- Responses WebSocket, Grok Realtime and Live sideband application frames are
  captured at the decoded frame boundary, preserving binary data and framing.
- Large messages are split into 64 KiB parts. `capture_id`, `message_id`, `part`,
  `parts`, `sequence` and stable `event_id` support reconstruction and duplicate
  detection. Tool calls and tool results remain in the actual message bodies.
- `X-Agent-Run-ID` / `X-Trace-ID` are preferred for run correlation. Native
  `thread-id`, session and subagent headers are retained when present. A relay
  cannot reconstruct local tool execution, subagent hierarchy or reasoning
  that the client never transmitted; an Agent SDK should supply explicit run
  and parent-span identifiers for those events.
- Arbitrary headers, Authorization, cookies and URL query strings are not
  copied. Content inside user/tool messages is retained and must be treated as
  private data; this is not a universal secret detector.
- `capture.end` distinguishes incomplete reads, cancellation, write errors and
  enqueue loss. Absence of this marker means an unfinished/interrupted capture.

## Delivery and failure behavior

Inference does not wait for Kafka or WAN connectivity. A bounded 32 MiB memory
queue feeds a local WAL, fsynced and rotated every second or 4 MiB. A process or
host crash can lose the unsynced tail (normally up to one second); capture is
**not a zero-loss transactional prerequisite for inference**.

The shipper deletes a segment only after all Kafka delivery callbacks succeed.
Broker acknowledgement followed by a source crash can replay stable IDs, so
downstream consumers must deduplicate. Partial WALs and invalid records must
remain visible for recovery; they must not be silently treated as complete.

When the memory queue or spool capacity is exhausted, inference continues and
`ai_log_capture_degraded` reports a loss counter. Monitor that signal together
with `shipper-status.json` backlog bytes/age and backend consumer lag. Do not
describe such a capture as complete. A 20 GiB spool is a starting value, not a
one-day guarantee at 50–100 GB/day; resize for the measured compressed traffic
and outage window. Recording both relay boundaries and retries increases raw
traffic; archival compression can remove repeated content, but Kafka capacity
must be measured independently.

## Rollout

1. Build the selected fork branch in CI or a separate build host. Do not build
   on the public production relay.
2. Start the shipper and validate TLS/authentication with synthetic events.
3. Mount/configure the source spool, then use the existing blue/green deployment
   script. Verify normal, streaming, WebSocket and canceled requests.
4. Verify those exact event IDs reach the clean topic, database and HDD archive;
   replay archived content before enabling all production traffic.
5. Roll back by setting `AI_LOG_ENABLED=false`; retain the spool and shipper
   until backlog is drained. Model/API behavior remains upstream-owned.
