"""Ship bounded gateway WAL segments to Kafka with at-least-once delivery.

The server binds the Kafka principal/topic to a tenant. No tenant supplied by
an HTTP caller is trusted. Payloads and credentials are never written to stdout.
"""
import argparse
import fcntl
import json
import logging
import os
from pathlib import Path
import signal
import stat
import time

MAX_LINE = 128 * 1024
STOP = False
LOG = logging.getLogger('ai-log-shipper')


def producer_config(config):
    result = {
        'bootstrap.servers': config['bootstrap_servers'],
        'security.protocol': 'SASL_SSL',
        'sasl.mechanism': 'SCRAM-SHA-512',
        'sasl.username': config['username'],
        'sasl.password': config['password'],
        'ssl.endpoint.identification.algorithm': 'https',
        'enable.ssl.certificate.verification': True,
        'enable.idempotence': True,
        'acks': 'all',
        'compression.type': 'zstd',
        'linger.ms': 20,
        'batch.size': 131072,
        'message.max.bytes': 1048576,
        'delivery.timeout.ms': 120000,
        'queue.buffering.max.kbytes': 32768,
        'client.id': config['source_id'],
    }
    if config.get('ca_file'):
        result['ssl.ca.location'] = config['ca_file']
    return result


def ship_segment(path, producer, topic, source_id):
    """Return delivered count; keep the entire file on any ambiguous outcome."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('spool entry is not a regular file')
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        errors, count = [], 0
        def delivered(error, _message):
            if error is not None:
                errors.append(error.code())
        while True:
            line = stream.readline(MAX_LINE + 1)
            if not line:
                break
            if len(line) > MAX_LINE or not line.endswith(b'\n'):
                # Leave the original file intact for recovery, including its
                # complete prefix. Never silently truncate an interrupted WAL.
                raise ValueError('incomplete or oversized WAL line')
            event = json.loads(line)
            if not isinstance(event, dict) or event.get('schema_version') != 1 or event.get('source_id') != source_id:
                raise ValueError('WAL schema/source mismatch')
            if any(not isinstance(event.get(key), str) or not event[key] for key in ('event_id', 'capture_id')):
                raise ValueError('missing event identity')
            key = (source_id + ':' + event['capture_id']).encode()
            while True:
                try:
                    producer.produce(topic, key=key, value=line, on_delivery=delivered)
                    break
                except BufferError:
                    producer.poll(1)
            producer.poll(0)
            count += 1
        remaining = producer.flush(125)
        if remaining or errors:
            raise RuntimeError('Kafka did not acknowledge the entire segment')
        # A crash between broker acknowledgement and unlink only duplicates
        # stable event IDs. The backend deduplicates them.
        path.unlink()
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return count


def main():
    from confluent_kafka import Producer
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    config_path = Path(args.config)
    if stat.S_IMODE(config_path.stat().st_mode) & 0o077:
        raise ValueError('shipper config must be mode 0600')
    config = json.loads(config_path.read_text())
    spool = Path(config['spool_dir']).resolve(strict=True)
    # Hold this descriptor for the process lifetime. Two shippers could
    # otherwise reorder segments of the same capture across Kafka partitions.
    lock_fd = os.open(spool / '.shipper.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    from shipper_control import Heartbeat, prepare_probe
    instance = prepare_probe(config, spool)
    heartbeat = Heartbeat(config, spool, instance) if instance else None
    producer = Producer(producer_config(config))
    if heartbeat:
        heartbeat.thread.start()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    def stop(_signal, _frame):
        global STOP
        STOP = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    delivered, last_report = 0, 0.0
    invalid_segments = {}
    while not STOP:
        failed, pending, oldest = 0, 0, time.time()
        # A flat, task-owned spool only; never follow symlinks or recurse.
        entries = sorted((p for p in spool.iterdir() if p.suffix in ('.ready', '.open')), key=lambda p: p.name)
        for path in entries:
            if STOP:
                break
            try:
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode):
                    continue
                pending += info.st_size
                oldest = min(oldest, info.st_mtime)
                fingerprint = (info.st_ino, info.st_mtime_ns, info.st_size)
                if invalid_segments.get(path.name) == fingerprint:
                    failed += 1
                    continue
                count = ship_segment(path, producer, config['topic'], config['source_id'])
                delivered += count
                if heartbeat and count:
                    heartbeat.update(delivered_events=delivered, last_delivery_at=int(time.time()))
            except FileNotFoundError:
                continue
            except Exception as error:
                failed += 1
                # Exception strings can include broker URLs. Report only type.
                LOG.error('segment_delivery_failed file=%s class=%s', path.name, type(error).__name__)
                if isinstance(error, ValueError):
                    # Keep the bytes and the failure visible, but do not resend
                    # a corrupt file's valid prefix on every polling cycle.
                    # An operator repair (inode/size/mtime change) permits retry.
                    invalid_segments[path.name] = fingerprint
                time.sleep(1)
                # Invalid local files must remain recoverable without starving
                # healthy files. Broker/network failures should back off.
                if failed >= 3 and not isinstance(error, (ValueError, json.JSONDecodeError)):
                    break
        present = {path.name for path in entries}
        invalid_segments = {name: value for name, value in invalid_segments.items() if name in present}
        if heartbeat:
            heartbeat.update(failures=failed)
        now = time.time()
        if now-last_report >= 30:
            stats = {'delivered_events': delivered, 'pending_bytes': pending, 'oldest_age_seconds': int(now-oldest), 'failures': failed, 'updated_at': int(now)}
            temp = spool / 'shipper-status.json.tmp'
            temp.write_text(json.dumps(stats))
            os.chmod(temp, 0o600)
            temp.replace(spool / 'shipper-status.json')
            LOG.info('status %s', json.dumps(stats))
            last_report = now
        time.sleep(1)
    producer.flush(15)
    if heartbeat:
        heartbeat.close()


if __name__ == '__main__':
    main()
