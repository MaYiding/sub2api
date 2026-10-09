# AI log pipeline

The gateway writes private WAL segments; `shipper.py` uploads them over verified
SASL/SCRAM TLS. Gateway changes are opt-in. See
[`deploy/AI_LOGGING.md`](../../deploy/AI_LOGGING.md) for source deployment.

The storage path is:

```
source WAL -> Kafka raw -> transactional cleaner -> Kafka clean
                                                   |       |
                                              archiver   indexer -> ClickHouse
                                                   |
                                               S3 on HDD
                                                   |
                                            archive receipts -> indexer
```

The cleaner binds tenancy and registered source identity from its raw topic, validates the event
schema and redacts explicit credential fields in envelope metadata. It never
summarizes, truncates or rewrites message bodies. Malformed input is fragmented
into a dead-letter topic for operator recovery. Kafka offsets and cleaned
output are committed in the same transaction; readers use `read_committed`.

The archiver batches up to roughly 128 MiB of input or 60 seconds, separately
per Kafka partition. It stores self-contained Parquet/ZSTD packs with a
content-defined block dictionary. Exact repeated content is stored once per
pack; there is no unbounded cross-pack dependency chain. Each pack includes
all event metadata, block checksums and reconstruction references. A complete
manifest records capture/trace membership and can rebuild the catalog.

An archive receipt is published only after the pack and manifest are written.
The archiver commits Kafka offsets only after receipt delivery succeeds. A
crash between these operations may replay IDs or leave overlapping packs;
readers must deduplicate `(tenant_id, source_id, event_id)` across all pages.
Receipt failure must never be treated as successful archival. Object store
durability and an independent backup remain necessary: a successful S3 reply
does not protect against losing the entire storage host. For SeaweedFS, enable
path-specific `fsync` on the archive bucket and use synchronous durable filer
metadata. The deployed leveldb2 metadata volume uses an ext4 `sync` mount;
body fsync alone does not make the separate filer directory durable. Verify the
[SeaweedFS path settings](https://github.com/seaweedfs/seaweedfs/wiki/Path-Specific-Configuration)
and metadata backend together before committing archival offsets.

ClickHouse stores searchable metadata, not large bodies. `schema.sql` moves
recent metadata from SSD to HDD after seven days and deletes detailed event
indexes after 180 days. Archive catalogs and manifests have no expiry; complete
events stay in HDD packs. Use `FINAL` or explicit stable-ID deduplication when
querying ReplacingMergeTree tables, especially for counts and accounting.

## Deployment

Use Python 3.12 on Linux amd64. Install only verified prebuilt wheels:

```sh
python -m pip install --only-binary=:all: --require-hashes \
  -r requirements-linux-amd64-py312.lock
python worker.py cleaner --config /etc/ai-logs/pipeline/cleaner.json
python worker.py archiver --config /etc/ai-logs/pipeline/archiver.json
python worker.py indexer --config /etc/ai-logs/pipeline/indexer.json
AI_LOG_CONFIG=/etc/ai-logs/pipeline/api.json uvicorn api:APP --host 127.0.0.1
```

Run each worker under a dedicated service account and restarting service. Keep
configuration mode 0640 or stricter and private keys accessible only to that
account. Sample shape (replace placeholders outside Git):

```json
{
  "instance": "ai-cleaner-01",
  "brokers": "broker1:9092,broker2:9092,broker3:9092",
  "tls": {"ca": "/private/ca.crt", "cert": "/private/cleaner.crt", "key": "/private/cleaner.key"},
  "status_path": "/var/lib/ai-log-pipeline/cleaner-status.json",
  "raw_topics": {"ai.raw.default.v1": "default"},
  "bindings_path": "/etc/ai-logs/pipeline/source-bindings.json",
  "clean_topics": ["ai.clean.default.v1"]
}
```

The archiver additionally requires `s3` with `endpoint`, `bucket`, `access_key`,
`secret_key`, and private `ca` path. The indexer needs `clickhouse` with localhost
`url`, `user`, and `password`. API configuration combines read-only S3 and
ClickHouse credentials with `query_tokens`, a mapping of SHA256 bearer-token
digests to server-assigned tenant IDs. Never commit real credentials.

Raw and clean topics must have the same partition count. The cleaner currently
uses a single consumer process with fixed transaction ID `instance`; do not
launch simultaneous workers with the same ID. Its consumer group is
`ai-cleaner-main`; archival and indexing use independent `ai-archiver-main` and
`ai-indexer-main` groups. Kafka ACLs must constrain each role and source topic.

## Recovery and query

`GET /v1/traces/{trace_id}` lists archived captures; `GET /v1/captures/{uuid}`
returns lossless events with `next_after` pagination. Collect every page, dedupe
IDs and validate sequence continuity and `capture.end` before calling a run
complete. An absent end marker or recorded drops means an incomplete capture.
`GET /v1/archive/{archive_key}` downloads a checksummed pack for operator export.
All data routes require a bearer token; `/healthz` returns only process health.

If the ClickHouse catalog is lost, apply `schema.sql` and run:

```sh
python rebuild_catalog.py --tenant default --config /private/recovery.json
```

The recovery config combines read-only S3 with ClickHouse insert credentials.
An optional `clickhouse.database` selects an isolated recovery database for a
restore drill. Recovery uses S3 **ListV1**, verifies pack receipts, and rebuilds
both permanent catalog tables. Body integrity is checked during actual replay.
Do not delete packs solely because a consumer offset has advanced.

## Capacity and monitoring

Measure cleaned/compressed size from representative production logs. Synthetic
repeated prompts compress exceptionally well and are not a sizing estimate.
Record both gateway boundaries and retries when measuring WAN/Kafka traffic.
Kafka retention is the earlier of time and per-partition byte limits. The
deployment uses 72 hours with byte caps for raw/clean, seven days for DLQ and
receipts, replication factor three and minimum ISR two.

Monitor source loss counters and spool backlog, consumer lag/progress, DLQ
events, worker status timestamps, under-replicated partitions, certificates,
archive checksums and disk free space. Permanent retention requires expanding
storage as it fills; it cannot be guaranteed by a fixed-size RAID volume. Keep
an independent copy of bulk archives in addition to OS/metadata backups.

## Source registration control plane

See [the implemented multi-source runbook](../../deploy/AI_LOG_MULTI_SITE_DESIGN.md).
`control.py` serves the administrator UI and scoped heartbeat API;
`provisioner.py` processes SQLite jobs separately. `provision_remote.py` is
installed as a root-owned forced SSH helper on the broker and cleaner hosts.
Web must not receive the helper keys or Kafka administrator certificate.

Use `Registry.initialize()` for the additive registry schema before restarting
the control services. Apply `schema.sql` before deploying the new indexer/API;
source activity materialized views count transport attempts, not billing usage.
The registry config requires database/encryption-key paths, public origin and
brokers; Web additionally needs admin credential hashes, session secret and
trusted network/proxy addresses. The provisioner requires fixed SSH helper
endpoints/known-hosts, a separate query token, status and online-backup paths.
Keep all configurations outside Git.

Control dependencies add `requirements-control-linux-amd64-py312.lock`.
Run tests on CI or the designated remote test host with the combined
`requirements-test-linux-amd64-py312.lock` and `python -m unittest discover -v`.

Source filters on trace/capture APIs do not grant per-source read authorization:
central query credentials still read all sources in their tenant. Source Kafka
and heartbeat credentials cannot read archives or call administrator APIs.
