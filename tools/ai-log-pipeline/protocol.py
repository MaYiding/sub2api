"""Versioned, lossless body handling and self-contained archival packs."""
import base64
from datetime import datetime, timezone
import hashlib
import io
import json
import uuid

SENSITIVE = {'authorization', 'cookie', 'set-cookie', 'api_key', 'access_token', 'refresh_token', 'password', 'secret', 'client_secret'}
GEAR = [int.from_bytes(hashlib.sha256(bytes([i])).digest()[:8], 'big') for i in range(256)]


def canonical(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode()


def scrub_metadata(value, depth=0):
    if depth > 16:
        raise ValueError('metadata nesting exceeds schema limit')
    if isinstance(value, dict):
        return {key: '[REDACTED]' if key.lower() in SENSITIVE else scrub_metadata(item, depth+1) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub_metadata(item, depth+1) for item in value]
    return value


def normalize(raw, tenant):
    event = json.loads(raw)
    if not isinstance(event, dict):
        raise ValueError('event must be an object')
    if event.get('schema_version') != 1:
        raise ValueError('unsupported schema')
    for key in ('event_id', 'capture_id'):
        if not isinstance(event.get(key), str):
            raise ValueError('invalid UUID')
        uuid.UUID(event[key])
    for key in ('source_id', 'trace_id', 'kind'):
        if not isinstance(event.get(key), str) or not 0 < len(event[key]) <= 256:
            raise ValueError('invalid event identity')
    if not isinstance(event.get('sequence'), int) or isinstance(event['sequence'], bool) or not 0 <= event['sequence'] < 2**63:
        raise ValueError('invalid sequence')
    if not isinstance(event.get('timestamp'), str) or len(event['timestamp']) > 64:
        raise ValueError('invalid timestamp')
    timestamp = datetime.fromisoformat(event['timestamp'].replace('Z', '+00:00'))
    if timestamp.tzinfo is None or not 2000 <= timestamp.year <= 2100:
        raise ValueError('invalid timestamp')
    # Retain the source's sub-microsecond timestamp string. Comparisons in the
    # archive catalog use parsed instants, rather than lexical time-zone order.
    if 'message_id' in event and (not isinstance(event['message_id'], str) or len(event['message_id']) > 256):
        raise ValueError('invalid message identity')
    data = base64.b64decode(event.get('data_base64', ''), validate=True)
    if len(data) > 65536:
        raise ValueError('unfragmented payload exceeds 64 KiB')
    part, parts = event.get('part', 0), event.get('parts', 0)
    if type(part) is not int or type(parts) is not int or not 0 <= part < 2**32 or not 0 <= parts < 2**32 or (parts and part >= parts):
        raise ValueError('invalid message fragmentation')
    metadata = event.get('metadata', {})
    if not isinstance(metadata, dict) or len(canonical(metadata)) > 16384:
        raise ValueError('invalid metadata')
    result = {key: event[key] for key in ('schema_version', 'event_id', 'source_id', 'capture_id', 'trace_id', 'sequence', 'timestamp', 'kind')}
    for key in ('message_id', 'part', 'parts'):
        if key in event:
            result[key] = event[key]
    result.update(tenant_id=tenant, clean_version=1, metadata=scrub_metadata(metadata), data_base64=base64.b64encode(data).decode(), body_sha256=hashlib.sha256(data).hexdigest(), body_bytes=len(data))
    return result


def content_chunks(data):
    """Bounded gear-hash chunks improve dedup when repeated prompts shift."""
    start, fingerprint = 0, 0
    for pos, byte in enumerate(data, 1):
        fingerprint = ((fingerprint << 1) + GEAR[byte]) & 0xffffffffffffffff
        length = pos-start
        if length >= 32768 or (length >= 4096 and fingerprint & 8191 == 0):
            yield data[start:pos]
            start, fingerprint = pos, 0
    if start < len(data):
        yield data[start:]


def make_pack(events):
    import pyarrow as pa
    import pyarrow.parquet as pq
    rows, blocks, seen = [], {}, set()
    raw_bytes = 0
    for original in events:
        event = dict(original)
        identity = (event['tenant_id'], event['source_id'], event['event_id'])
        if identity in seen:
            continue
        seen.add(identity)
        data = base64.b64decode(event.pop('data_base64', ''), validate=True)
        raw_bytes += len(data)
        refs = []
        for chunk in content_chunks(data):
            digest = hashlib.sha256(chunk).digest()
            blocks.setdefault(digest, chunk)
            refs.append(digest)
        rows.append({'record_type': 0, 'event_json': canonical(event).decode(), 'refs': refs, 'digest': None, 'body': None})
    rows.extend({'record_type': 1, 'event_json': None, 'refs': None, 'digest': digest, 'body': body} for digest, body in blocks.items())
    schema = pa.schema([('record_type', pa.uint8()), ('event_json', pa.string()), ('refs', pa.list_(pa.binary(32))), ('digest', pa.binary(32)), ('body', pa.binary())], metadata={b'ai_log_pack_version': b'1', b'self_contained': b'true'})
    output = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), output, compression='zstd', compression_level=9, row_group_size=4096, data_page_size=4*1024*1024)
    blob = output.getvalue()
    return blob, {'events': len(seen), 'raw_body_bytes': raw_bytes, 'unique_body_bytes': sum(map(len, blocks.values())), 'archive_bytes': len(blob), 'sha256': hashlib.sha256(blob).hexdigest()}


def read_pack(blob):
    import pyarrow.parquet as pq
    table = pq.read_table(io.BytesIO(blob))
    if (table.schema.metadata or {}).get(b'ai_log_pack_version') != b'1':
        raise ValueError('unknown archive format')
    rows = table.to_pylist()
    blocks = {}
    for row in rows:
        if row['record_type'] == 1:
            if hashlib.sha256(row['body']).digest() != row['digest']:
                raise ValueError('archive block checksum mismatch')
            blocks[row['digest']] = row['body']
    for row in rows:
        if row['record_type'] == 0:
            event = json.loads(row['event_json'])
            data = b''.join(blocks[digest] for digest in row['refs'])
            if hashlib.sha256(data).hexdigest() != event['body_sha256'] or len(data) != event['body_bytes']:
                raise ValueError('archive event checksum mismatch')
            event['data_base64'] = base64.b64encode(data).decode()
            yield event


def catalog_entries(events, key):
    entries = {}
    for event in events:
        identity = (event['tenant_id'], event['source_id'], event['trace_id'], event['capture_id'])
        row = entries.setdefault(identity, dict(zip(('tenant_id', 'source_id', 'trace_id', 'capture_id'), identity), archive_key=key, first_time=event['timestamp'], last_time=event['timestamp'], event_count=0))
        instant = lambda value: datetime.fromisoformat(value.replace('Z', '+00:00'))
        row['first_time'] = min(row['first_time'], event['timestamp'], key=instant)
        row['last_time'] = max(row['last_time'], event['timestamp'], key=instant)
        row['event_count'] += 1
    return list(entries.values())
