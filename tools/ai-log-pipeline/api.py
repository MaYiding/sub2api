"""Authenticated catalog lookup and lossless cold-archive replay."""
import hashlib
import hmac
import os
import re
import uuid

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import Response

from common import ch_request, load_config, s3_client
from protocol import read_pack

CONFIG = load_config(os.environ['AI_LOG_CONFIG'])
S3 = s3_client(CONFIG)
APP = FastAPI(title='AI Log Archive', docs_url=None, redoc_url=None, openapi_url=None)


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
def trace(trace_id: str, owner=Depends(tenant), limit: int = Query(default=100, ge=1, le=1000)):
    if len(trace_id) > 256:
        raise HTTPException(400, 'Invalid trace ID')
    rows = query('SELECT source_id,capture_id,min(first_time) AS first_time,max(last_time) AS last_time,uniqExact(archive_key) AS archive_packs FROM ai_logs.archive_catalog FINAL WHERE tenant_id={tenant:String} AND trace_id={trace:String} GROUP BY source_id,capture_id ORDER BY first_time LIMIT {limit:UInt32}', {'tenant': owner, 'trace': trace_id, 'limit': limit})
    return {'trace_id': trace_id, 'captures': rows, 'limit': limit, 'archive_delay_seconds': CONFIG.get('archive_delay_seconds', 60)}


@APP.get('/v1/captures/{capture_id}')
def capture(capture_id: uuid.UUID, owner=Depends(tenant), after: str = '', packs: int = Query(default=4, ge=1, le=8)):
    if after:
        checked_key(after, owner)
    keys = query('SELECT DISTINCT archive_key FROM ai_logs.archive_catalog FINAL WHERE tenant_id={tenant:String} AND capture_id={capture:String} AND archive_key>{after:String} ORDER BY archive_key LIMIT {limit:UInt32}', {'tenant': owner, 'capture': str(capture_id), 'after': after, 'limit': packs+1})
    page, more = keys[:packs], len(keys)>packs
    events, seen, encoded_bytes = [], set(), 0
    last_key = after
    for index, row in enumerate(page):
        pack_events = []
        for event in read_pack(read_object(row['archive_key'], owner)):
            if event['tenant_id'] != owner or event['capture_id'] != str(capture_id):
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
    return {'capture_id': str(capture_id), 'events': events, 'next_after': last_key if more else None, 'capture_end_markers': completion, 'note': 'Combine all pages and validate sequences; a missing end marker indicates an unfinished or incomplete capture.'}


@APP.get('/v1/archive/{key:path}')
def archive(key: str, owner=Depends(tenant)):
    return Response(read_object(key, owner), media_type='application/vnd.apache.parquet', headers={'Content-Disposition': 'attachment; filename="ai-log-archive.parquet"'})
