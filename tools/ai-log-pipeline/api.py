"""Authenticated catalog lookup and lossless cold-archive replay."""
import hashlib
import hmac
import os
import re
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import Response

from common import ch_request, load_config, s3_client
from protocol import read_pack

CONFIG = load_config(os.environ['AI_LOG_CONFIG'])
S3 = s3_client(CONFIG)
APP = FastAPI(title='AI Log Archive', docs_url=None, redoc_url=None, openapi_url=None)

API_KEY_MAX_BYTES = 128
API_KEY_FINGERPRINT = re.compile(r'[0-9a-f]{64}')


def tenant(authorization: str = Header(default='')):
    if not authorization.startswith('Bearer ') or len(authorization) > 4096:
        raise HTTPException(401, 'Authentication required')
    digest = hashlib.sha256(authorization[7:].encode()).hexdigest()
    for expected, bound_tenant in CONFIG['query_tokens'].items():
        if hmac.compare_digest(digest, expected):
            return bound_tenant
    raise HTTPException(401, 'Authentication required')


def query(sql, parameters):
    return ch_request(CONFIG, sql+' FORMAT JSON', parameters=parameters).json()['data']


def _cutoff(seconds):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _validate_source(source_id):
    if len(source_id) > 128:
        raise HTTPException(400, 'Invalid source ID')
    return source_id


def _api_key_filters(api_key_id, api_key_name, api_key_prefix, api_key_last4, api_key_fingerprint, api_key):
    """Return a bound ClickHouse predicate for API-key metadata.

    The full key is accepted through X-Sub2API-Key so it does not appear in a
    URL. It is compared against the exact value projected from capture
    metadata into the ClickHouse events table.
    """
    if api_key and len(api_key.encode()) > API_KEY_MAX_BYTES:
        raise HTTPException(400, 'Invalid API key')
    if api_key_fingerprint and API_KEY_FINGERPRINT.fullmatch(api_key_fingerprint) is None:
        raise HTTPException(400, 'Invalid API key fingerprint')
    filters, parameters = [], {}
    if api_key_id is not None:
        filters.append("e.api_key_id = {api_key_id:Int64}")
        parameters['api_key_id'] = api_key_id
    if api_key_name:
        if len(api_key_name) > 100:
            raise HTTPException(400, 'Invalid API key name')
        filters.append("e.api_key_name = {api_key_name:String}")
        parameters['api_key_name'] = api_key_name
    if api_key_prefix:
        if len(api_key_prefix) > 16:
            raise HTTPException(400, 'Invalid API key prefix')
        filters.append("e.api_key_prefix = {api_key_prefix:String}")
        parameters['api_key_prefix'] = api_key_prefix
    if api_key_last4:
        if len(api_key_last4) > 16:
            raise HTTPException(400, 'Invalid API key suffix')
        filters.append("e.api_key_last4 = {api_key_last4:String}")
        parameters['api_key_last4'] = api_key_last4
    if api_key_fingerprint:
        filters.append("e.api_key_fingerprint = {api_key_fingerprint:String}")
        parameters['api_key_fingerprint'] = api_key_fingerprint
    if api_key:
        filters.append("e.api_key = {api_key:String}")
        parameters['api_key'] = api_key
    return filters, parameters


def _request_columns():
    return """source_id,capture_id,trace_id,event_time,
        api_key,api_key_id,api_key_name,api_key_prefix,api_key_last4,api_key_fingerprint,
        simpleJSONExtractString(metadata_json, 'route') AS route,
        simpleJSONExtractString(metadata_json, 'method') AS method,
        simpleJSONExtractString(metadata_json, 'request_id') AS request_id"""


def _public_api_key_metadata(metadata):
    if not isinstance(metadata, dict):
        return None
    result = {}
    for field in ('api_key', 'api_key_id', 'api_key_name', 'api_key_prefix', 'api_key_last4', 'api_key_fingerprint'):
        if field == 'api_key' and metadata.get('api_key_capture') is not True:
            continue
        if field in metadata:
            result[field] = metadata[field]
    return result or None


def checked_key(key, owner):
    pattern = r'v1/tenant='+re.escape(owner)+r'/topic=ai\.clean\.'+re.escape(owner)+r'\.v1/partition=\d+/\d{20}-\d{20}\.parquet'
    if re.fullmatch(pattern, key) is None:
        raise HTTPException(400, 'Invalid archive key')
    return key


def read_object(key, owner):
    checked_key(key, owner)
    obj = S3.get_object(Bucket=CONFIG['s3']['bucket'], Key=key)
    try:
        if obj['ContentLength'] > 256<<20:
            raise HTTPException(413, 'Archive exceeds replay limit; use operator export')
        blob = obj['Body'].read()
    finally:
        obj['Body'].close()
    if hashlib.sha256(blob).hexdigest() != obj.get('Metadata', {}).get('sha256'):
        raise HTTPException(502, 'Archive checksum verification failed')
    return blob


@APP.get('/healthz')
def health():
    return {'ok': True, 'service': 'ai-log-query', 'archive_format': 1}


@APP.get('/v1/traces/{trace_id}')
def trace(trace_id: str, owner=Depends(tenant), source_id: str = '', limit: int = Query(default=100, ge=1, le=1000)):
    if len(trace_id) > 256:
        raise HTTPException(400, 'Invalid trace ID')
    rows = query('SELECT source_id,capture_id,min(first_time) AS first_time,max(last_time) AS last_time,uniqExact(archive_key) AS archive_packs FROM ai_logs.archive_catalog FINAL WHERE tenant_id={tenant:String} AND trace_id={trace:String} AND ({source:String}=\'\' OR source_id={source:String}) GROUP BY source_id,capture_id ORDER BY first_time LIMIT {limit:UInt32}', {'tenant': owner, 'trace': trace_id, 'source': source_id, 'limit': limit})
    return {'trace_id': trace_id, 'captures': rows, 'limit': limit, 'archive_delay_seconds': CONFIG.get('archive_delay_seconds', 60)}


@APP.get('/v1/captures/{capture_id}')
def capture(capture_id: uuid.UUID, owner=Depends(tenant), source_id: str = '', after: str = '', packs: int = Query(default=4, ge=1, le=8)):
    if after:
        checked_key(after, owner)
    keys = query('SELECT DISTINCT archive_key FROM ai_logs.archive_catalog FINAL WHERE tenant_id={tenant:String} AND capture_id={capture:String} AND ({source:String}=\'\' OR source_id={source:String}) AND archive_key>{after:String} ORDER BY archive_key LIMIT {limit:UInt32}', {'tenant': owner, 'capture': str(capture_id), 'source': source_id, 'after': after, 'limit': packs+1})
    page, more = keys[:packs], len(keys)>packs
    events, seen, encoded_bytes = [], set(), 0
    last_key = after
    for index, row in enumerate(page):
        pack_events = []
        for event in read_pack(read_object(row['archive_key'], owner)):
            if event['tenant_id'] != owner or event['capture_id'] != str(capture_id) or (source_id and event['source_id'] != source_id):
                continue
            identity = (event['source_id'], event['event_id'])
            if identity not in seen:
                seen.add(identity)
                pack_events.append(event)
        size = sum(len(e.get('data_base64', '')) for e in pack_events)
        if events and encoded_bytes+size > 96<<20:
            more = True
            break
        events.extend(pack_events)
        encoded_bytes += size
        last_key = row['archive_key']
    events.sort(key=lambda event: (event['source_id'], event['sequence']))
    completion = [event['metadata'] for event in events if event['kind'] == 'capture.end']
    starts = [event.get('metadata') for event in events if event['kind'] == 'capture.start']
    return {'capture_id': str(capture_id), 'events': events, 'next_after': last_key if more else None,
            'capture_end_markers': completion, 'api_key': _public_api_key_metadata(starts[0] if starts else None),
            'note': 'Combine all pages and validate sequences; a missing end marker indicates an unfinished or incomplete capture.'}


@APP.get('/v1/requests/latest')
def latest_requests(owner=Depends(tenant), source_id: str = '', api_key_id: int | None = Query(default=None, ge=1),
                    api_key_name: str = '', api_key_prefix: str = '', api_key_last4: str = '',
                    api_key_fingerprint: str = '', window_seconds: int = Query(default=86400, ge=0, le=180*86400),
                    limit: int = Query(default=100, ge=1, le=1000),
                    api_key: str = Header(default='', alias='X-Sub2API-Key')):
    """List recent client captures, optionally filtered by an API key.

    API keys are read from a header so a credential is not placed in a URL.
    Captures created before plaintext-key capture was enabled can only be
    selected with their recorded api_key_id or other recorded metadata.
    """
    source_id = _validate_source(source_id)
    filters, parameters = _api_key_filters(api_key_id, api_key_name, api_key_prefix, api_key_last4, api_key_fingerprint, api_key)
    filters.extend(["e.tenant_id = {tenant:String}", "e.kind = 'capture.start'", "e.event_time >= parseDateTime64BestEffort({cutoff:String}, 3, 'UTC')"])
    parameters.update({'tenant': owner, 'cutoff': _cutoff(window_seconds), 'limit': limit})
    if source_id:
        filters.append("e.source_id = {source:String}")
        parameters['source'] = source_id
    rows = query('SELECT '+_request_columns()+" FROM ai_logs.events AS e FINAL WHERE " + ' AND '.join(filters) + ' ORDER BY event_time DESC, capture_id DESC LIMIT {limit:UInt32}', parameters)
    return {'requests': rows, 'window_seconds': window_seconds, 'limit': limit,
            'note': 'Rows are capture.start events; use /v1/captures/{capture_id} for complete replay.'}


@APP.get('/v1/api-keys/stats')
def api_key_stats(owner=Depends(tenant), source_id: str = '', api_key_id: int | None = Query(default=None, ge=1),
                  api_key_name: str = '', api_key_prefix: str = '', api_key_last4: str = '',
                  api_key_fingerprint: str = '', window_seconds: int = Query(default=86400, ge=0, le=180*86400),
                  limit: int = Query(default=100, ge=1, le=1000),
                  api_key: str = Header(default='', alias='X-Sub2API-Key')):
    """Count recent captures grouped by API key identity."""
    source_id = _validate_source(source_id)
    filters, parameters = _api_key_filters(api_key_id, api_key_name, api_key_prefix, api_key_last4, api_key_fingerprint, api_key)
    filters.extend(["e.tenant_id = {tenant:String}", "e.kind = 'capture.start'", "e.event_time >= parseDateTime64BestEffort({cutoff:String}, 3, 'UTC')",
                    "e.api_key_id > 0"])
    parameters.update({'tenant': owner, 'cutoff': _cutoff(window_seconds), 'limit': limit})
    if source_id:
        filters.append("e.source_id = {source:String}")
        parameters['source'] = source_id
    rows = query("""SELECT e.source_id,
        e.api_key, e.api_key_id, e.api_key_name, e.api_key_prefix, e.api_key_last4, e.api_key_fingerprint,
        uniqExact(e.capture_id) AS request_count,
        min(e.event_time) AS first_seen, max(e.event_time) AS last_seen
        FROM ai_logs.events AS e FINAL WHERE """ + ' AND '.join(filters) +
        " GROUP BY e.source_id,api_key,api_key_id,api_key_name,api_key_prefix,api_key_last4,api_key_fingerprint ORDER BY request_count DESC,last_seen DESC LIMIT {limit:UInt32}", parameters)
    return {'stats': rows, 'window_seconds': window_seconds, 'limit': limit,
            'note': 'Counts distinct capture IDs from capture.start events.'}


@APP.get('/v1/archive/{key:path}')
def archive(key: str, owner=Depends(tenant)):
    return Response(read_object(key, owner), media_type='application/vnd.apache.parquet', headers={'Content-Disposition': 'attachment; filename="ai-log-archive.parquet"'})


@APP.get('/v1/sources/activity')
def source_activity(owner=Depends(tenant)):
    rows = query("SELECT source_id,kind,maxMerge(seen_at) AS last_seen,sumMerge(events) AS received_events,sumMerge(body_bytes) AS received_body_bytes FROM ai_logs.source_activity WHERE tenant_id={tenant:String} AND day>=today()-1 GROUP BY source_id,kind", {'tenant': owner})
    return {'sources': rows, 'note': 'Transport counters include retries; these are not billing counts.'}
