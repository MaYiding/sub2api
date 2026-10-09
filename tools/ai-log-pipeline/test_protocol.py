import base64
from datetime import datetime, timezone
import hashlib
import io
import json
import random
import unittest
import uuid

from protocol import canonical, catalog_entries, content_chunks, make_pack, normalize, read_pack


def event(data=b'', **changes):
    value = {'schema_version': 1, 'event_id': str(uuid.uuid4()),
             'capture_id': str(uuid.uuid4()), 'source_id': 'test-source',
             'trace_id': 'test-trace', 'sequence': 0,
             'timestamp': datetime.now(timezone.utc).isoformat(),
             'kind': 'http.request', 'data_base64': base64.b64encode(data).decode(),
             'metadata': {}}
    value.update(changes)
    return normalize(canonical(value), 'default')


class ProtocolTests(unittest.TestCase):
    def test_binary_unicode_and_repeated_context_replay_exactly(self):
        rng = random.Random(7)
        data = bytes(rng.randrange(256) for _ in range(65536))
        first = event(data)
        second = event(data, capture_id=first['capture_id'], sequence=1)
        third = event(('中文工具输出\x00🙂'*200).encode())
        original = [first, second, third, event()]
        blob, summary = make_pack(original+[first])
        restored = list(read_pack(blob))
        self.assertEqual(restored, original)
        self.assertEqual(summary['events'], 4)
        self.assertLess(summary['unique_body_bytes'], summary['raw_body_bytes'])
        self.assertEqual(hashlib.sha256(blob).hexdigest(), summary['sha256'])

    def test_shifted_content_still_shares_chunks(self):
        rng = random.Random(31)
        data = bytes(rng.randrange(256) for _ in range(65536))
        before = set(content_chunks(data))
        after = set(content_chunks(b'prefix changed\n'+data))
        self.assertGreater(len(before & after), 0)
        self.assertEqual(b''.join(content_chunks(data)), data)
        self.assertTrue(all(len(chunk) <= 32768 for chunk in before))

    def test_metadata_credentials_removed_bodies_not_summarized(self):
        body = b'{"secret":"user supplied tool output must remain intact"}'
        value = event(body, tenant_id='attacker', metadata={'Authorization': 'bearer secret', 'nested': [{'password': 'secret'}]})
        self.assertEqual(value['tenant_id'], 'default')
        self.assertEqual(value['metadata']['Authorization'], '[REDACTED]')
        self.assertEqual(value['metadata']['nested'][0]['password'], '[REDACTED]')
        self.assertEqual(base64.b64decode(value['data_base64']), body)

    def test_invalid_schema_and_oversized_frames_rejected(self):
        for changes in ({'sequence': True}, {'message_id': {}}, {'parts': 2, 'part': 2}, {'timestamp': '2026-10-09'}, {'schema_version': 99}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                event(**changes)
        with self.assertRaises(ValueError):
            event(b'x'*65537)

    def test_modified_block_is_detected(self):
        import pyarrow as pa
        import pyarrow.parquet as pq
        blob, _ = make_pack([event(b'original')])
        table = pq.read_table(io.BytesIO(blob))
        rows = table.to_pylist()
        rows[-1]['body'] = b'changed'
        buffer = io.BytesIO()
        pq.write_table(pa.Table.from_pylist(rows, schema=table.schema), buffer)
        with self.assertRaises(ValueError):
            list(read_pack(buffer.getvalue()))

    def test_catalog_compares_timezones_as_instants(self):
        first = event(timestamp='2026-10-09T09:00:00+08:00')
        second = event(timestamp='2026-10-09T02:00:00+00:00', capture_id=first['capture_id'])
        row = catalog_entries([first, second], 'pack')[0]
        self.assertEqual(row['first_time'], first['timestamp'])
        self.assertEqual(row['last_time'], second['timestamp'])


if __name__ == '__main__':
    unittest.main()
