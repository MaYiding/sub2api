from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock

import archive_retention

NOW = datetime.now(timezone.utc)
PREFIX = 'v1/tenant=default/topic=ai.clean.default.v1/partition='


class FakeS3:
    def __init__(self, objects):
        self.objects = dict(objects)
        self.deleted = []

    def list_objects(self, Bucket, Prefix, Marker, MaxKeys):
        keys = sorted(k for k in self.objects if k.startswith(Prefix) and k > Marker)
        return {'Contents': [{'Key': k, 'LastModified': self.objects[k], 'Size': 10} for k in keys], 'IsTruncated': False}

    def delete_objects(self, Bucket, Delete, **_):
        for item in Delete['Objects']:
            self.deleted.append(item['Key'])
            self.objects.pop(item['Key'])
        return {}


def pack(partition, first, last, age_days):
    key = '%s%d/%020d-%020d.parquet' % (PREFIX, partition, first, last)
    when = NOW - timedelta(days=age_days)
    return {key: when, key + '.manifest.json': when}


class RetentionTests(unittest.TestCase):
    def run_job(self, objects, offsets, fresh=True, done=True, dry_run=False):
        client = FakeS3(objects)
        def prom(_url, expr):
            if 'updated_at' in expr:
                return [{'value': [0, '30' if fresh else '5000']}]
            if 'backfill_complete' in expr:
                return [{'value': [0, '1' if done else '0']}]
            return [{'metric': {'topic': 'ai.clean.default.v1', 'partition': str(p)}, 'value': [0, str(o)]} for p, o in offsets.items()]
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(archive_retention, 'prom', prom):
            args = types.SimpleNamespace(tenant='default', days=30, max_delete=100, prometheus='x', max_status_age=900,
                                         status=str(Path(tmp) / 's.json'), dry_run=dry_run)
            archive_retention.run({'s3': {'bucket': 'b'}}, args, client)
            return client, json.loads(Path(args.status).read_text())

    def test_deletes_only_old_and_consumed_packs_manifest_first(self):
        objects = {**pack(0, 0, 99, 40), **pack(0, 100, 199, 40), **pack(1, 0, 50, 10)}
        client, status = self.run_job(objects, {0: 150, 1: 999})
        self.assertEqual(status['deleted_packs'], 1)
        self.assertEqual(status['blocked_packs'], 1)  # offsets 100-199 not fully consumed
        self.assertEqual(status['retained_packs'], 1)
        self.assertTrue(client.deleted[0].endswith('.manifest.json'))
        self.assertEqual(sorted(client.deleted), sorted(k for k in pack(0, 0, 99, 40)))

    def test_untrusted_guard_deletes_nothing(self):
        objects = pack(0, 0, 99, 40)
        for kwargs in ({'fresh': False}, {'done': False}, {'dry_run': True}):
            client, status = self.run_job(objects, {0: 10**9}, **kwargs)
            self.assertEqual((client.deleted, status['deleted_packs']), ([], 0))


if __name__ == '__main__':
    unittest.main()
