CREATE DATABASE IF NOT EXISTS ai_logs;

CREATE TABLE IF NOT EXISTS ai_logs.events
(
    tenant_id LowCardinality(String), source_id LowCardinality(String),
    trace_id String, capture_id String, event_id String,
    event_time DateTime64(9, 'UTC'), sequence UInt64,
    kind LowCardinality(String), message_id String, part UInt32, parts UInt32,
    body_sha256 FixedString(64), body_bytes UInt32, metadata_json String CODEC(ZSTD(3)),
    kafka_partition UInt16, kafka_offset UInt64, version UInt64,
    INDEX capture_bloom capture_id TYPE bloom_filter(0.01) GRANULARITY 4
)
ENGINE = ReplacingMergeTree(version)
PARTITION BY toYYYYMM(event_time)
ORDER BY (tenant_id, trace_id, source_id, capture_id, sequence, event_id)
TTL toDateTime(event_time) + INTERVAL 7 DAY TO VOLUME 'cold',
    toDateTime(event_time) + INTERVAL 180 DAY DELETE
SETTINGS storage_policy = 'ai_logs';

-- Permanent small catalog; bodies remain in the independent HDD archive packs.
CREATE TABLE IF NOT EXISTS ai_logs.archive_catalog
(
    tenant_id LowCardinality(String), source_id LowCardinality(String),
    trace_id String, capture_id String, archive_key String,
    first_time DateTime64(9, 'UTC'), last_time DateTime64(9, 'UTC'),
    event_count UInt64, version UInt64,
    INDEX capture_bloom capture_id TYPE bloom_filter(0.01) GRANULARITY 4
)
ENGINE = ReplacingMergeTree(version)
ORDER BY (tenant_id, trace_id, source_id, capture_id, archive_key)
TTL toDateTime(first_time) + INTERVAL 7 DAY TO VOLUME 'cold'
SETTINGS storage_policy = 'ai_logs';

CREATE TABLE IF NOT EXISTS ai_logs.archive_segments
(
    tenant_id LowCardinality(String), archive_key String, topic LowCardinality(String),
    partition_id UInt16, first_offset UInt64, last_offset UInt64,
    sha256 FixedString(64), archive_bytes UInt64, event_count UInt64,
    created_at DateTime64(6, 'UTC'), version UInt64
)
ENGINE = ReplacingMergeTree(version)
ORDER BY (tenant_id, archive_key)
TTL toDateTime(created_at) + INTERVAL 7 DAY TO VOLUME 'cold'
SETTINGS storage_policy = 'ai_logs';
