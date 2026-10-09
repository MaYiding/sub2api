"""Kafka cleaner, independent HDD archiver, and metadata indexer."""
import argparse
import base64
from collections import defaultdict
from datetime import datetime, timezone
import json
import logging
import signal
import time

from common import LOG, ch_insert, consumer, load_config, offsets, producer, s3_client, status
from protocol import canonical, catalog_entries, make_pack, normalize

STOP = False


def stop(_signal, _frame):
    global STOP
    STOP = True


def messages(client, count=1000, timeout=1):
    result = client.consume(count, timeout)
    for message in result:
        if message.error():
            raise RuntimeError('Kafka consume failed; code='+str(message.error().code()))
    return result


def emit(client, topic, value, key, partition=-1):
    while True:
        try:
            client.produce(topic, value=canonical(value), key=key, partition=partition)
            client.poll(0)
            return
        except BufferError:
            client.poll(1)


def cleaner(config):
    mapping = config['raw_topics']
    client = consumer(config, 'ai-cleaner-main', list(mapping))
    output = producer(config, transactional=True)
    output.init_transactions(60)
    counters = {'processed': 0, 'dead_lettered': 0}
    try:
        while not STOP:
            batch = messages(client)
            if not batch:
                counters['updated_at'] = int(time.time()); status(config, counters)
                continue
            output.begin_transaction()
            try:
                for message in batch:
                    tenant = mapping[message.topic()]
                    try:
                        event = normalize(message.value(), tenant)
                        event['raw_partition'] = message.partition()
                        event['raw_offset'] = message.offset()
                        key = (tenant+':'+event['source_id']+':'+event['capture_id']).encode()
                        emit(output, 'ai.clean.'+tenant+'.v1', event, key, message.partition())
                        counters['processed'] += 1
                    except (ValueError, TypeError, KeyError, OverflowError) as error:
                        # Preserve malformed input as bounded DLQ fragments.
                        raw = message.value() or b''
                        parts = max(1, (len(raw)+65535)//65536)
                        for part in range(parts):
                            record = {'dlq_version': 1, 'raw_topic': message.topic(), 'raw_partition': message.partition(), 'raw_offset': message.offset(), 'error_class': type(error).__name__, 'part': part, 'parts': parts, 'data_base64': base64.b64encode(raw[part*65536:(part+1)*65536]).decode()}
                            emit(output, 'ai.dlq.'+tenant+'.v1', record, str(message.partition()).encode())
                        counters['dead_lettered'] += 1
                output.send_offsets_to_transaction(offsets(batch), client.consumer_group_metadata(), 60)
                output.commit_transaction(60)
            except Exception:
                output.abort_transaction(30)
                raise
            counters['updated_at'] = int(time.time())
            status(config, counters)
    finally:
        client.close()


def archive_batch(config, batch, s3, receipts):
    by_partition = defaultdict(list)
    for message in batch:
        event = json.loads(message.value())
        event['clean_partition'], event['clean_offset'] = message.partition(), message.offset()
        by_partition[(message.topic(), message.partition())].append(event)
    result = {'events': 0, 'raw_body_bytes': 0, 'unique_body_bytes': 0, 'archive_bytes': 0, 'packs': 0}
    for (topic, partition), original in by_partition.items():
        # Keep transport retries visible in event metadata but deduplicate a
        # replay of the same immutable source event within this pack.
        events = list({(e['tenant_id'], e['source_id'], e['event_id']): e for e in original}.values())
        first = min(e['clean_offset'] for e in original)
        last = max(e['clean_offset'] for e in original)
        tenant = events[0]['tenant_id']
        if any(e['tenant_id'] != tenant for e in events):
            raise ValueError('mixed-tenant archive partition')
        # Offset-derived paths do not depend on wall-clock retry timing.
        key = f'v1/tenant={tenant}/topic={topic}/partition={partition}/{first:020d}-{last:020d}.parquet'
        blob, summary = make_pack(events)
        membership = catalog_entries(events, key)
        manifest = {'manifest_version': 1, 'tenant_id': tenant, 'archive_key': key, 'topic': topic, 'partition': partition, 'first_offset': first, 'last_offset': last, 'created_at': datetime.now(timezone.utc).isoformat(), 'summary': summary, 'catalog': membership}
        bucket = config['s3']['bucket']
        s3.put_object(Bucket=bucket, Key=key, Body=blob, ContentType='application/vnd.apache.parquet', Metadata={'sha256': summary['sha256'], 'pack-version': '1'})
        head = s3.head_object(Bucket=bucket, Key=key)
        if head['ContentLength'] != len(blob) or head.get('Metadata', {}).get('sha256') != summary['sha256']:
            raise RuntimeError('archive durable-write verification failed')
        s3.put_object(Bucket=bucket, Key=key+'.manifest.json', Body=canonical(manifest), ContentType='application/json')
        # Split membership receipts; a high-cardinality pack can otherwise
        # exceed Kafka's message limit. The S3 manifest remains complete.
        for start in range(0, len(membership), 500):
            receipt = dict(manifest, catalog=membership[start:start+500], receipt_part=start//500)
            emit(receipts, 'ai.archive.receipts.v1', receipt, key.encode())
        for name in ('events', 'raw_body_bytes', 'unique_body_bytes', 'archive_bytes'):
            result[name] += summary[name]
        result['packs'] += 1
    # Delivery errors are tracked by the caller's producer callback.
    if receipts.flush(120):
        raise RuntimeError('archive receipt delivery timed out')
    return result


def archiver(config):
    from confluent_kafka import Producer
    from common import kafka_config
    client = consumer(config, 'ai-archiver-main', config['clean_topics'])
    failures = []
    def delivery(error, _message):
        if error is not None:
            failures.append(error.code())
    class CheckedProducer:
        def __init__(self):
            self.inner = Producer(dict(kafka_config(config), **{'enable.idempotence': True, 'acks': 'all', 'compression.type': 'zstd', 'delivery.timeout.ms': 120000}))
        def produce(self, *args, **kwargs):
            self.inner.produce(*args, on_delivery=delivery, **kwargs)
        def poll(self, timeout):
            return self.inner.poll(timeout)
        def flush(self, timeout):
            pending = self.inner.flush(timeout)
            if failures:
                raise RuntimeError('archive receipt was rejected')
            return pending
    receipts, s3 = CheckedProducer(), s3_client(config)
    counters = {'events': 0, 'raw_body_bytes': 0, 'unique_body_bytes': 0, 'archive_bytes': 0, 'packs': 0}
    batch, size, started = [], 0, time.monotonic()
    try:
        while not STOP:
            new = messages(client, count=1000)
            batch.extend(new)
            size += sum(len(m.value()) for m in new)
            if batch and (size >= config.get('pack_input_bytes', 128<<20) or time.monotonic()-started >= config.get('flush_seconds', 60)):
                result = archive_batch(config, batch, s3, receipts)
                client.commit(offsets=offsets(batch), asynchronous=False)
                for name, count in result.items():
                    counters[name] += count
                batch, size, started = [], 0, time.monotonic()
            counters.update(updated_at=int(time.time()), buffered_events=len(batch), buffered_bytes=size)
            status(config, counters)
        if batch:
            archive_batch(config, batch, s3, receipts)
            client.commit(offsets=offsets(batch), asynchronous=False)
    finally:
        client.close()


def event_row(event, message):
    return {'tenant_id': event['tenant_id'], 'source_id': event['source_id'], 'trace_id': event['trace_id'], 'capture_id': event['capture_id'], 'event_id': event['event_id'], 'event_time': event['timestamp'], 'sequence': event['sequence'], 'kind': event['kind'], 'message_id': event.get('message_id', ''), 'part': event.get('part', 0), 'parts': event.get('parts', 0), 'body_sha256': event['body_sha256'], 'body_bytes': event['body_bytes'], 'metadata_json': json.dumps(event['metadata'], ensure_ascii=False), 'kafka_partition': message.partition(), 'kafka_offset': message.offset(), 'version': message.offset()+1}


def indexer(config):
    client = consumer(config, 'ai-indexer-main', config['clean_topics']+['ai.archive.receipts.v1'])
    counters = {'events': 0, 'receipts': 0}
    try:
        while not STOP:
            batch = messages(client)
            events, catalog, segments = [], [], []
            for message in batch:
                value = json.loads(message.value())
                if message.topic() == 'ai.archive.receipts.v1':
                    for entry in value['catalog']:
                        catalog.append(dict(entry, version=message.offset()+1))
                    segments.append({'tenant_id': value['tenant_id'], 'archive_key': value['archive_key'], 'topic': value['topic'], 'partition_id': value['partition'], 'first_offset': value['first_offset'], 'last_offset': value['last_offset'], 'sha256': value['summary']['sha256'], 'archive_bytes': value['summary']['archive_bytes'], 'event_count': value['summary']['events'], 'created_at': value['created_at'], 'version': message.offset()+1})
                    counters['receipts'] += 1
                else:
                    events.append(event_row(value, message))
                    counters['events'] += 1
            ch_insert(config, 'events', events)
            ch_insert(config, 'archive_catalog', catalog)
            ch_insert(config, 'archive_segments', segments)
            if batch:
                client.commit(offsets=offsets(batch), asynchronous=False)
            counters['updated_at'] = int(time.time())
            status(config, counters)
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('role', choices=['cleaner', 'archiver', 'indexer'])
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    config = load_config(args.config)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        globals()[args.role](config)
    except Exception as error:
        LOG.error('worker_failed role=%s class=%s', args.role, type(error).__name__)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
