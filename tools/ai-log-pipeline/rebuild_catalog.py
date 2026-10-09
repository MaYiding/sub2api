"""Rebuild permanent ClickHouse catalog from immutable S3 manifests (ListV1)."""
import argparse
import json
import re

from common import ch_insert, load_config, s3_client


def manifests(client, bucket, prefix):
    marker = ''
    while True:
        response = client.list_objects(Bucket=bucket, Prefix=prefix, Marker=marker, MaxKeys=1000)
        entries = response.get('Contents', [])
        for entry in entries:
            if entry['Key'].endswith('.parquet.manifest.json'):
                yield entry['Key']
        if not response.get('IsTruncated'):
            return
        following = response.get('NextMarker') or (entries[-1]['Key'] if entries else '')
        if not following or following <= marker:
            raise RuntimeError('S3 listing cursor did not advance')
        marker = following


def rebuild(config, tenant):
    if re.fullmatch(r'[a-z0-9_-]{1,64}', tenant) is None:
        raise ValueError('invalid tenant')
    client = s3_client(config)
    bucket = config['s3']['bucket']
    prefix = 'v1/tenant='+tenant+'/'
    count = 0
    for key in manifests(client, bucket, prefix):
        obj = client.get_object(Bucket=bucket, Key=key)
        try:
            if obj['ContentLength'] > 64<<20:
                raise ValueError('manifest exceeds recovery bound')
            value = json.loads(obj['Body'].read())
        finally:
            obj['Body'].close()
        if value.get('manifest_version') != 1 or value['tenant_id'] != tenant or value['archive_key']+'.manifest.json' != key:
            raise ValueError('manifest identity mismatch')
        head = client.head_object(Bucket=bucket, Key=value['archive_key'])
        if head.get('Metadata', {}).get('sha256') != value['summary']['sha256'] or head['ContentLength'] != value['summary']['archive_bytes']:
            raise ValueError('archive receipt verification failed')
        rows = []
        for entry in value['catalog']:
            if entry['tenant_id'] != tenant or entry['archive_key'] != value['archive_key']:
                raise ValueError('catalog membership mismatch')
            rows.append(dict(entry, version=0))
        ch_insert(config, 'archive_catalog', rows)
        ch_insert(config, 'archive_segments', [{'tenant_id': tenant, 'archive_key': value['archive_key'], 'topic': value['topic'], 'partition_id': value['partition'], 'first_offset': value['first_offset'], 'last_offset': value['last_offset'], 'sha256': value['summary']['sha256'], 'archive_bytes': value['summary']['archive_bytes'], 'event_count': value['summary']['events'], 'created_at': value['created_at'], 'version': 0}])
        count += 1
    return count


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True, help='Private config with read-only S3 and ClickHouse indexer credentials')
    parser.add_argument('--tenant', required=True)
    args = parser.parse_args()
    print('Rebuilt archive packs:', rebuild(load_config(args.config), args.tenant))
