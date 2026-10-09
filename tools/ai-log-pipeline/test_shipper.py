import fcntl
import json
from pathlib import Path
import tempfile
import unittest

import shipper


class Producer:
    def __init__(self, fail=False):
        self.fail = fail
        self.messages = []
        self.callbacks = []

    def produce(self, topic, **kwargs):
        self.messages.append((topic, kwargs['key'], kwargs['value']))
        self.callbacks.append(kwargs['on_delivery'])

    def poll(self, _timeout):
        pass

    def flush(self, _timeout):
        if self.fail:
            return len(self.callbacks)
        callbacks, self.callbacks = self.callbacks, []
        for callback in callbacks:
            callback(None, None)
        return 0


class ShipperTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'segment.ready'
        self.event = {'schema_version': 1, 'event_id': 'event-1', 'source_id': 'source', 'capture_id': 'capture-1'}
        self.path.write_text(json.dumps(self.event)+'\n')

    def test_ambiguous_delivery_retains_file_and_retry_keeps_identity(self):
        failed = Producer(fail=True)
        with self.assertRaises(RuntimeError):
            shipper.ship_segment(self.path, failed, 'raw', 'source')
        self.assertTrue(self.path.exists())
        success = Producer()
        self.assertEqual(shipper.ship_segment(self.path, success, 'raw', 'source'), 1)
        self.assertEqual(failed.messages, success.messages)
        self.assertFalse(self.path.exists())

    def test_active_writer_is_not_read_or_deleted(self):
        with self.path.open('rb') as writer:
            fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
            producer = Producer()
            self.assertEqual(shipper.ship_segment(self.path, producer, 'raw', 'source'), 0)
            self.assertEqual(producer.messages, [])
        self.assertTrue(self.path.exists())

    def test_partial_tail_is_retained(self):
        self.path.write_bytes(self.path.read_bytes()+b'{"partial":')
        with self.assertRaises(ValueError):
            shipper.ship_segment(self.path, Producer(), 'raw', 'source')
        self.assertTrue(self.path.exists())

    def test_wrong_source_and_symlinks_rejected(self):
        with self.assertRaises(ValueError):
            shipper.ship_segment(self.path, Producer(), 'raw', 'other')
        link = self.path.parent/'link.ready'
        link.symlink_to(self.path)
        with self.assertRaises(OSError):
            shipper.ship_segment(link, Producer(), 'raw', 'source')

    def test_invalid_json_shapes_remain_recoverable(self):
        for value in ([], None, dict(self.event, capture_id=23)):
            with self.subTest(value=value):
                self.path.write_text(json.dumps(value)+'\n')
                with self.assertRaises(ValueError):
                    shipper.ship_segment(self.path, Producer(), 'raw', 'source')
                self.assertTrue(self.path.exists())

    def test_tls_and_idempotence_cannot_be_disabled_by_config(self):
        config = shipper.producer_config({'bootstrap_servers': 'example:443', 'username': 'user', 'password': 'private', 'source_id': 'source', 'security.protocol': 'PLAINTEXT'})
        self.assertEqual(config['security.protocol'], 'SASL_SSL')
        self.assertEqual(config['acks'], 'all')
        self.assertTrue(config['enable.idempotence'])
        self.assertTrue(config['enable.ssl.certificate.verification'])


if __name__ == '__main__':
    unittest.main()
