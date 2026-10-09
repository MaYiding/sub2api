import unittest

from protocol import canonical
from test_protocol import event
from worker import archive_batch
from rebuild_catalog import manifests


class Message:
    def __init__(self, value, offset=0):
        self.data = canonical(value)
        self.position = offset
    def value(self): return self.data
    def offset(self): return self.position
    def topic(self): return 'ai.clean.default.v1'
    def partition(self): return 2


class S3:
    def __init__(self, fail=False, corrupt=False):
        self.fail, self.corrupt = fail, corrupt
        self.objects = {}
    def put_object(self, **args):
        if self.fail: raise ConnectionError('synthetic outage')
        self.objects[args['Key']] = args
    def head_object(self, **args):
        item = self.objects[args['Key']]
        return {'ContentLength': len(item['Body'])+(1 if self.corrupt else 0), 'Metadata': item['Metadata']}


class Receipts:
    def __init__(self, pending=0):
        self.values, self.pending = [], pending
    def produce(self, *args, **kwargs): self.values.append(kwargs['value'])
    def poll(self, _timeout): pass
    def flush(self, _timeout): return self.pending


class ArchiveTests(unittest.TestCase):
    def test_storage_failure_never_emits_commit_receipt(self):
        for s3 in (S3(fail=True), S3(corrupt=True)):
            receipts = Receipts()
            with self.assertRaises((ConnectionError, RuntimeError)):
                archive_batch({'s3': {'bucket': 'archive'}}, [Message(event(b'complete body'))], s3, receipts)
            self.assertFalse(receipts.values)

    def test_receipt_failure_leaves_durable_pack_for_replay(self):
        s3, receipts = S3(), Receipts(pending=1)
        messages = [Message(event(b'original bytes'))]
        with self.assertRaises(RuntimeError):
            archive_batch({'s3': {'bucket': 'archive'}}, messages, s3, receipts)
        keys = set(s3.objects)
        result = archive_batch({'s3': {'bucket': 'archive'}}, messages, s3, Receipts())
        self.assertEqual(set(s3.objects), keys)
        self.assertEqual(result['events'], 1)

    def test_recovery_uses_v1_pagination_without_skipping_keys(self):
        class Listing:
            def list_objects(self, **args):
                marker = args['Marker']
                if not marker:
                    return {'IsTruncated': True, 'Contents': [{'Key': 'a.parquet'}, {'Key': 'a.parquet.manifest.json'}]}
                if marker == 'a.parquet.manifest.json':
                    return {'IsTruncated': False, 'Contents': [{'Key': 'b.parquet.manifest.json'}]}
                raise AssertionError('unexpected cursor')
        self.assertEqual(list(manifests(Listing(), 'bucket', 'prefix')), ['a.parquet.manifest.json', 'b.parquet.manifest.json'])


if __name__ == '__main__':
    unittest.main()
