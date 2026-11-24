import hashlib
import unittest

from test_protocol import event
from worker import event_row


class Message:
    def __init__(self, offset=17):
        self.position = offset

    def partition(self):
        return 3

    def offset(self):
        return self.position


class WorkerProjectionTests(unittest.TestCase):
    def test_event_row_projects_explicit_plaintext_key(self):
        key = 'sk-internal-test-key'
        value = event(metadata={
            'api_key_capture': True,
            'api_key': key,
            'api_key_id': 7,
            'api_key_name': 'worker',
            'api_key_prefix': 'sk-inter',
            'api_key_last4': '-key',
            'api_key_fingerprint': hashlib.sha256(key.encode()).hexdigest(),
        })

        row = event_row(value, Message())

        self.assertEqual(row['api_key'], key)
        self.assertEqual(row['api_key_id'], 7)
        self.assertEqual(row['api_key_name'], 'worker')
        self.assertEqual(row['api_key_prefix'], 'sk-inter')
        self.assertEqual(row['api_key_last4'], '-key')
        self.assertEqual(row['api_key_fingerprint'], hashlib.sha256(key.encode()).hexdigest())

    def test_event_row_does_not_project_unmarked_plaintext_key(self):
        value = event(metadata={
            'api_key': 'must-not-be-indexed',
            'api_key_id': 7,
            'api_key_name': 'worker',
        })

        row = event_row(value, Message())

        self.assertEqual(row['api_key'], '')
        self.assertEqual(row['api_key_id'], 7)
        self.assertEqual(row['api_key_name'], 'worker')
        self.assertEqual(row['api_key_prefix'], '')
        self.assertEqual(row['api_key_last4'], '')
        self.assertEqual(row['api_key_fingerprint'], '')


if __name__ == '__main__':
    unittest.main()
