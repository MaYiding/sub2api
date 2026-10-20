"""Compact the clean event stream into one conversation file per session.

Events are reassembled into captures, captures that did not finish cleanly
are counted and discarded, and complete turns are merged into durable
conversation trees (sessions.py). A session file is written to HDD once the
session has been idle for `idle_flush_seconds` and rewritten if the session
continues within `reopen_window_seconds`.

Sources: `kafka` consumes `ai.clean.<tenant>.v1` from stored offsets;
`backfill` replays archived Parquet packs from S3 in upload order and records
the same Kafka offsets, so the live consumer continues exactly where the
backfill stopped.
"""
import argparse
import base64
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import signal
import sqlite3
import time

from common import LOG, kafka_config, load_config
from sessions import Store, instant
from turns import classify

STOP = False


def stop(_signal, _frame):
    global STOP
    STOP = True


class Captures:
    """Open captures in memory; offsets below the oldest open capture are safe to commit."""

    def __init__(self, timeout):
        self.open, self.position, self.timeout = {}, {}, timeout
        self.max_ts = 0.0

    def skip(self, topic, partition, offset):
        pair = (topic, partition)
        self.position[pair] = max(self.position.get(pair, 0), offset+1)

    def add(self, topic, partition, offset, event):
        pair = (topic, partition)
        self.skip(topic, partition, offset)
        ts = instant(event['timestamp'])
        self.max_ts = max(self.max_ts, ts)
        entry = self.open.get(event['capture_id'])
        if entry is None:
            entry = self.open[event['capture_id']] = {'events': {}, 'first': (pair, offset), 'seen': ts}
        entry['seen'] = max(entry['seen'], ts)
        event['size'] = len(event['data'])
        if event['kind'].startswith('upstream.'):
            event['data'] = b''  # Upstream copies are not part of the conversation; count only.
        entry['events'].setdefault(event['event_id'], event)
        if event['kind'] == 'capture.end':
            return self.finish(event['capture_id'])
        return None

    def finish(self, capture_id):
        events = sorted(self.open.pop(capture_id)['events'].values(), key=lambda e: e['sequence'])
        capture = {'capture_id': capture_id, 'source_id': events[0]['source_id'], 'start': None, 'end': None, 'streams': {},
                   't0': events[0]['timestamp'], 't1': events[-1]['timestamp'], 'bytes': sum(e['size'] for e in events),
                   'sequence_gap': [e['sequence'] for e in events] != list(range(len(events)))}
        for event in events:
            kind = event['kind']
            if kind == 'capture.start':
                capture['start'] = event.get('metadata') or {}
            elif kind == 'capture.end':
                capture['end'] = event.get('metadata') or {}
            elif event['data']:
                if kind.startswith('upstream.'):
                    kind += ':' + str((event.get('metadata') or {}).get('attempt_id', ''))
                capture['streams'].setdefault(kind, bytearray()).extend(event['data'])
        return capture

    def expire(self, clock):
        stale = [cid for cid, entry in self.open.items() if entry['seen'] < clock-self.timeout]
        return [self.finish(cid) for cid in stale]

    def watermarks(self):
        marks = dict(self.position)
        for entry in self.open.values():
            pair, offset = entry['first']
            marks[pair] = min(marks.get(pair, offset), offset)
        return marks


def decode(raw):
    event = json.loads(raw)
    event['data'] = base64.b64decode(event.pop('data_base64', '') or '')
    return event


class KafkaSource:
    live = True

    def __init__(self, config, stored):
        from confluent_kafka import Consumer, TopicPartition, OFFSET_BEGINNING
        self.topic = config['clean_topic']
        self.client = Consumer(dict(kafka_config(config), **{'group.id': config.get('group', 'ai-sessionizer-main'), 'enable.auto.commit': False,
                                                             'enable.auto.offset.store': False, 'isolation.level': 'read_committed',
                                                             # Never jump silently: an out-of-range stored offset means lost data.
                                                             'auto.offset.reset': 'error',
                                                             'fetch.max.bytes': 64 << 20, 'max.partition.fetch.bytes': 16 << 20}))
        partitions = self.client.list_topics(self.topic, timeout=30).topics[self.topic].partitions
        assignment = [TopicPartition(self.topic, p, stored.get((self.topic, p), OFFSET_BEGINNING)) for p in sorted(partitions)]
        self.client.assign(assignment)
        self.TopicPartition = TopicPartition
        LOG.info('kafka_assigned partitions=%d stored=%d', len(assignment), sum(1 for a in assignment if a.offset >= 0))

    def poll(self):
        result = []
        for message in self.client.consume(500, 1.0):
            if message.error():
                raise RuntimeError('Kafka consume failed; code='+str(message.error().code()))
            result.append((message.topic(), message.partition(), message.offset(), decode(message.value())))
        return result

    def commit(self, marks):
        offsets = [self.TopicPartition(t, p, o) for (t, p), o in marks.items() if t == self.topic]
        if offsets:
            # Committed group offsets only drive lag monitoring; SQLite is authoritative.
            self.client.commit(offsets=offsets, asynchronous=True)

    def close(self):
        self.client.close()


class BackfillSource:
    """Replay archived packs (keys sorted by upload time) that precede live Kafka data."""
    live = False

    def __init__(self, config, stored):
        import boto3
        from botocore.config import Config
        s3 = config['backfill']['s3']
        self.bucket, self.topic = s3['bucket'], config['clean_topic']
        self.client = boto3.client('s3', endpoint_url=s3['endpoint'], aws_access_key_id=s3['access_key'], aws_secret_access_key=s3['secret_key'],
                                   region_name='us-east-1', verify=s3['ca'], config=Config(signature_version='s3v4', s3={'addressing_style': 'path'},
                                                                                       retries={'max_attempts': 5, 'mode': 'standard'}, read_timeout=120))
        prefix = 'v1/tenant=%s/topic=%s/' % (config.get('tenant', 'default'), self.topic)
        keys, marker = [], ''
        while True:
            # SeaweedFS needs ListObjects V1 for complete listings.
            page = self.client.list_objects(Bucket=self.bucket, Prefix=prefix, Marker=marker, MaxKeys=1000)
            for item in page.get('Contents', []):
                if item['Key'].endswith('.parquet'):
                    keys.append((item['LastModified'], item['Key']))
            if not page.get('IsTruncated'):
                break
            marker = page.get('NextMarker') or page['Contents'][-1]['Key']
        lower, upper = config['backfill'].get('not_before', ''), config['backfill'].get('not_after', '9999')
        self.keys = [k for t, k in sorted(keys) if lower <= t.isoformat() <= upper]
        self.stored, self.index = stored, 0
        LOG.info('backfill_packs total=%d', len(self.keys))

    def _fetch(self, key):
        import io
        import pyarrow.parquet as pq
        blob = self.client.get_object(Bucket=self.bucket, Key=key)['Body'].read()
        table = pq.read_table(io.BytesIO(blob))
        kinds, digests, bodies = (table.column(n).to_pylist() for n in ('record_type', 'digest', 'body'))
        blocks = {digests[i]: bodies[i] for i, kind in enumerate(kinds) if kind == 1}
        result = []
        for event_json, refs in zip(table.column('event_json').to_pylist(), table.column('refs').to_pylist()):
            if event_json is None:
                continue
            event = json.loads(event_json)
            partition, offset = event.pop('clean_partition'), event.pop('clean_offset')
            event['data'] = b''.join(blocks[d] for d in refs)
            result.append((self.topic, partition, offset, event))
        result.sort(key=lambda r: (r[1], r[2]))
        return result

    def _consumed(self, key):
        # Keys are .../partition=P/<first>-<last>.parquet; skip packs already consumed.
        match = re.search(r'/partition=(\d+)/\d+-(\d+)\.parquet$', key)
        return bool(match) and int(match.group(2)) < self.stored.get((self.topic, int(match.group(1))), 0)

    def poll(self):
        # Download/decode a few packs ahead: S3 reads from the HDD archive are
        # otherwise ~40% of backfill time.
        from concurrent.futures import ThreadPoolExecutor
        if not hasattr(self, 'pool'):
            self.pool, self.ahead = ThreadPoolExecutor(max_workers=4), []
        while len(self.ahead) < 8 and self.index < len(self.keys):
            key = self.keys[self.index]
            self.index += 1
            if not self._consumed(key):
                self.ahead.append(self.pool.submit(self._fetch, key))
        if not self.ahead:
            return None
        return [r for r in self.ahead.pop(0).result() if r[2] >= self.stored.get((r[0], r[1]), 0)]

    def commit(self, marks):
        self.stored.update(marks)

    def close(self):
        if hasattr(self, 'pool'):
            self.pool.shutdown(wait=False, cancel_futures=True)


def write_catalog(store, root, days):
    import pyarrow as pa
    import pyarrow.parquet as pq
    names = ['session_id', 'source_id', 'user_id', 'state', 'first_ts', 'last_ts', 'version', 'path', 'file_bytes', 'resumed_from', 'merged_from', 'inferred']
    for day in sorted(days):
        rows = [dict(zip(names, r)) for r in store.catalog_rows(day)]
        if not rows:
            continue
        target = Path(root) / '_catalog' / ('dt=%s.parquet' % day)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name('.' + target.name + '.tmp')
        pq.write_table(pa.Table.from_pylist(rows), temp, compression='zstd')
        os.replace(temp, target)


def recent_days(store, clock, days=None):
    days = days or store.reopen_window // 86400 + 2
    return {datetime.fromtimestamp(clock - d*86400, store.tz).strftime('%Y-%m-%d') for d in range(int(days))}


def write_status(config, values):
    path = Path(config['status_path'])
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(values))
    os.chmod(temp, 0o640)
    temp.replace(path)


def run(config, mode):
    import fcntl
    lock = open(config['state_db'] + '.lock', 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('another sessionizer (live or backfill) owns this state database') from None
    store = Store(config['state_db'], config['output_root'], config.get('idle_flush_seconds', 1800), config.get('reopen_window_seconds', 7*86400),
                  config.get('max_unflushed_seconds', 6*3600))
    if mode == 'backfill':
        # Event time runs ~30x faster than wall time; write each session less often.
        store.idle_flush = max(store.idle_flush, config.get('backfill_idle_flush_seconds', 6*3600))
        store.max_unflushed = max(store.max_unflushed, config.get('backfill_max_unflushed_seconds', 48*3600))
    stored = store.offsets()
    if mode == 'kafka' and stored and not store.get_meta('backfill_complete') and not config.get('skip_backfill'):
        raise SystemExit('backfill has not completed; refusing to consume live Kafka from its partial offsets')
    if mode == 'backfill' and store.get_meta('backfill_complete'):
        raise SystemExit('backfill already completed; live offsets are authoritative')
    source = BackfillSource(config, stored) if mode == 'backfill' else KafkaSource(config, stored)
    captures = Captures(config.get('capture_timeout_seconds', 7200))
    counters = {'captures': 0, 'kept': 0, 'dropped': 0, 'errors': 0, 'written_sessions': 0, 'written_bytes': 0, 'sealed': 0}
    last_commit = last_flush = last_status = last_catalog = time.monotonic()
    last_gc, pending, touched, clock = 0.0, 0, set(), 0.0

    def handle(capture):
        nonlocal pending
        counters['captures'] += 1
        pending += 1
        if store.known(capture['capture_id']):
            return
        size = capture['bytes']
        try:
            turn, reason, hint = classify(capture)
            if turn:
                store.ingest(turn, size)
                counters['kept'] += 1
                return
        except (sqlite3.Error, OSError):
            raise
        except Exception as error:  # Unexpected client JSON shapes must not stop the stream.
            LOG.warning('capture_unprocessable class=%s', type(error).__name__)
            turn, reason, hint = None, 'error:' + type(error).__name__, None
            counters['errors'] += 1
        store.record_drop(capture, reason, hint, size)
        counters['dropped'] += 1

    try:
        while not STOP:
            batch = source.poll()
            if batch is None:
                LOG.info('backfill_complete')
                break
            for topic, partition, offset, event in batch:
                if not event.get('source_verified') and not config.get('allow_unverified_sources'):
                    # Only sources bound to a registered Kafka identity (not legacy/acceptance traffic).
                    captures.skip(topic, partition, offset)
                    counters['unverified_events'] = counters.get('unverified_events', 0) + 1
                    continue
                capture = captures.add(topic, partition, offset, event)
                if capture:
                    handle(capture)
            clock = captures.max_ts if not source.live else max(captures.max_ts, time.time() - 120)
            for capture in captures.expire(clock):
                handle(capture)
            now = time.monotonic()
            if pending >= 2000 or (pending and now-last_commit >= 5) or now-last_commit >= 30:
                marks = captures.watermarks()
                store.commit(marks)
                source.commit(marks)
                pending, last_commit = 0, now
            if now-last_flush >= 30 and clock:
                store.commit(captures.watermarks())
                touched |= {store.relative_path(s).split('/')[1][3:] for s in store.due(clock)}
                written, size = store.flush(clock)
                counters['written_sessions'] += written
                counters['written_bytes'] += size
                counters['sealed'] += store.seal(clock)
                if touched and now-last_catalog >= 300:
                    write_catalog(store, config['output_root'], touched)
                    touched, last_catalog = set(), now
                last_flush = now
            if clock and clock-last_gc >= 6*3600:
                store.gc(clock)
                touched |= recent_days(store, clock)
                last_gc = clock
            if now-last_status >= 15:
                counters.update(store.stats(), updated_at=int(time.time()), clock=int(clock), open_captures=len(captures.open),
                                backfill_complete=int(bool(store.get_meta('backfill_complete'))),
                                # Durably consumed offsets: archive retention never deletes packs above these.
                                offsets={'%s/%d' % pair: offset for pair, offset in store.offsets().items()})
                write_status(config, counters)
                last_status = now
        store.commit(captures.watermarks())
        if touched:
            write_catalog(store, config['output_root'], touched)
        if mode == 'backfill' and not STOP:
            # Open captures stay below the stored offsets for the live consumer.
            while store.flush(clock)[0]:
                pass
            write_catalog(store, config['output_root'], recent_days(store, clock, 400))
            store.set_meta('backfill_complete', datetime.now(timezone.utc).isoformat())
            counters.update(store.stats(), updated_at=int(time.time()), clock=int(clock), open_captures=len(captures.open), backfill_complete=1,
                            offsets={'%s/%d' % pair: offset for pair, offset in store.offsets().items()})
            write_status(config, counters)
    finally:
        source.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['kafka', 'backfill'])
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    import faulthandler
    faulthandler.register(signal.SIGUSR1, all_threads=False)  # `kill -USR1` prints the current stack to the journal.
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        run(load_config(args.config), args.mode)
    except Exception as error:
        LOG.error('sessionizer_failed class=%s', type(error).__name__)
        raise SystemExit(1) from None
    if STOP and args.mode == 'backfill':
        # An interrupted backfill must not count as success (OnSuccess starts live consumption).
        LOG.info('backfill_interrupted')
        raise SystemExit(3)


if __name__ == '__main__':
    main()
