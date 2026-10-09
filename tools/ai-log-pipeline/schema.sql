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

ALTER TABLE ai_logs.events ADD COLUMN IF NOT EXISTS source_name String DEFAULT '';
ALTER TABLE ai_logs.events ADD COLUMN IF NOT EXISTS source_region String DEFAULT '';
ALTER TABLE ai_logs.events ADD COLUMN IF NOT EXISTS source_verified UInt8 DEFAULT 0;
ALTER TABLE ai_logs.events ADD COLUMN IF NOT EXISTS raw_topic LowCardinality(String) DEFAULT '';
ALTER TABLE ai_logs.events ADD COLUMN IF NOT EXISTS raw_partition UInt16 DEFAULT 0;
ALTER TABLE ai_logs.events ADD COLUMN IF NOT EXISTS raw_offset UInt64 DEFAULT 0;
ALTER TABLE ai_logs.events ADD COLUMN IF NOT EXISTS source_binding_revision UInt64 DEFAULT 0;

CREATE TABLE IF NOT EXISTS ai_logs.source_activity
(
 tenant_id LowCardinality(String), source_id LowCardinality(String), kind LowCardinality(String), day Date,
 seen_at AggregateFunction(max, DateTime64(3, 'UTC')),
 events AggregateFunction(sum, UInt64), body_bytes AggregateFunction(sum, UInt64)
)
ENGINE=AggregatingMergeTree
PARTITION BY toYYYYMM(day) ORDER BY (tenant_id,source_id,kind,day)
TTL day + INTERVAL 7 DAY DELETE SETTINGS storage_policy='ai_logs';

CREATE MATERIALIZED VIEW IF NOT EXISTS ai_logs.source_activity_clean_mv TO ai_logs.source_activity AS
SELECT tenant_id,source_id,'clean' AS kind,today() AS day,maxState(now64(3,'UTC')) AS seen_at,
 sumState(toUInt64(1)) AS events,sumState(toUInt64(e.body_bytes)) AS body_bytes
FROM ai_logs.events AS e GROUP BY tenant_id,source_id;

CREATE MATERIALIZED VIEW IF NOT EXISTS ai_logs.source_activity_archive_mv TO ai_logs.source_activity AS
SELECT tenant_id,source_id,'archive' AS kind,today() AS day,maxState(now64(3,'UTC')) AS seen_at,
 sumState(toUInt64(event_count)) AS events,sumState(toUInt64(0)) AS body_bytes
FROM ai_logs.archive_catalog GROUP BY tenant_id,source_id;
