"""Expire raw archive packs once session files are the long-term copy.

Deletes `<first>-<last>.parquet` packs (manifest first, then pack) older than
`--days` by upload time, but only when the sessionizer has durably consumed
every offset in the pack, as reported by its exported
`ai_log_worker_offset{component="sessionizer"}` metrics. Missing, stale or
pre-backfill sessionizer metrics mean nothing is deleted. The ClickHouse
archive catalog expires on the same horizon through its table TTL, so the
query API never lists a capture whose pack is gone.

  python archive_retention.py --config archiver.json [--days 30] [--dry-run]
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import time
import urllib.parse
import urllib.request

from common import LOG, load_config, s3_client

PACK = re.compile(r'^v1/tenant=[a-z0-9_-]+/topic=(ai\.clean\.[a-z0-9_-]+\.v1)/partition=(\d+)/(\d{20})-(\d{20})\.parquet$')


def prom(url, expr):
    with urllib.request.urlopen(url + '/api/v1/query?' + urllib.parse.urlencode({'query': expr}), timeout=20) as response:
        return json.loads(response.read())['data']['result']


def consumed_offsets(url, max_age):
    """Per (topic, partition) offsets the sessionizer has durably consumed, or None if not trustworthy."""
    fresh = prom(url, 'time() - ai_log_worker_updated_at{component="sessionizer"}')
    done = prom(url, 'ai_log_worker_backfill_complete{component="sessionizer"}')
    if len(fresh) != 1 or float(fresh[0]['value'][1]) > max_age or len(done) != 1 or float(done[0]['value'][1]) != 1:
        return None
    return {(r['metric']['topic'], int(r['metric']['partition'])): int(float(r['value'][1]))
            for r in prom(url, 'ai_log_worker_offset{component="sessionizer"}')}


def listing(client, bucket, prefix):
    marker = ''
    while True:
        # SeaweedFS needs ListObjects V1 for complete listings.
        page = client.list_objects(Bucket=bucket, Prefix=prefix, Marker=marker, MaxKeys=1000)
        entries = page.get('Contents', [])
        yield from entries
        if not page.get('IsTruncated'):
            return
        following = page.get('NextMarker') or (entries[-1]['Key'] if entries else '')
        if not following or following <= marker:
            raise RuntimeError('S3 listing cursor did not advance')
        marker = following


def run(config, args, client=None):
    client, bucket = client or s3_client(config), config['s3']['bucket']
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    status = {'updated_at': int(time.time()), 'days': args.days, 'dry_run': int(args.dry_run), 'eligible_packs': 0, 'eligible_bytes': 0,
              'blocked_packs': 0, 'deleted_packs': 0, 'deleted_bytes': 0, 'retained_packs': 0, 'retained_bytes': 0, 'guard_ok': 0}
    offsets = consumed_offsets(args.prometheus, args.max_status_age)
    status['guard_ok'] = int(offsets is not None)
    eligible, manifests = [], set()
    for entry in listing(client, bucket, 'v1/tenant=%s/' % args.tenant):
        key = entry['Key']
        if key.endswith('.parquet.manifest.json'):
            manifests.add(key)
            continue
        match = PACK.match(key)
        if not match:
            continue
        if entry['LastModified'] >= cutoff:
            status['retained_packs'] += 1
            status['retained_bytes'] += entry['Size']
            continue
        topic, partition, last = match.group(1), int(match.group(2)), int(match.group(4))
        if offsets is None or last >= offsets.get((topic, partition), -1):
            status['blocked_packs'] += 1
            continue
        eligible.append((entry['LastModified'], key, entry['Size']))
    eligible.sort()
    status['eligible_packs'], status['eligible_bytes'] = len(eligible), sum(e[2] for e in eligible)
    batch = eligible[:args.max_delete]
    LOG.info('retention cutoff=%s guard_ok=%d eligible=%d blocked=%d this_run=%d dry_run=%d',
             cutoff.isoformat(), status['guard_ok'], len(eligible), status['blocked_packs'], len(batch), int(args.dry_run))
    for start in range(0, len(batch), 500):
        chunk = batch[start:start+500]
        if args.dry_run:
            continue
        # Manifest first: a crash in between leaves an orphan pack that the next
        # run deletes, never a manifest that would rebuild a dangling catalog row.
        for keys in ([k + '.manifest.json' for _t, k, _s in chunk if k + '.manifest.json' in manifests], [k for _t, k, _s in chunk]):
            if not keys:
                continue
            result = client.delete_objects(Bucket=bucket, Delete={'Objects': [{'Key': k} for k in keys], 'Quiet': True})
            if result.get('Errors'):
                raise RuntimeError('S3 delete reported %d errors' % len(result['Errors']))
        status['deleted_packs'] += len(chunk)
        status['deleted_bytes'] += sum(s for _t, _k, s in chunk)
    if eligible:
        status['oldest_eligible_upload'] = int(eligible[0][0].timestamp())
    path = Path(args.status)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(status))
    os.chmod(temp, 0o640)
    temp.replace(path)
    LOG.info('retention done %s', json.dumps(status))


def main():
    import logging
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--tenant', default='default')
    parser.add_argument('--days', type=int, default=30)
    parser.add_argument('--max-delete', type=int, default=5000, help='packs per run; about 5 days of traffic')
    parser.add_argument('--prometheus', default='http://10.10.20.55:9090')
    parser.add_argument('--max-status-age', type=int, default=900)
    parser.add_argument('--status', default='/var/lib/ai-log-pipeline/retention-status.json')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if args.days < 7:
        raise SystemExit('refusing retention shorter than 7 days')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    run(load_config(args.config), args)


if __name__ == '__main__':
    main()
