import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
import uuid

from fastapi.testclient import TestClient
from registry import digest


class QueryIsolationTests(unittest.TestCase):
    def setUp(self):
        config = {'query_tokens': {digest('central-query'): 'default'}}
        spec = importlib.util.spec_from_file_location('query_isolation_test_app', Path(__file__).with_name('api.py'))
        self.api = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {'AI_LOG_CONFIG': 'unused'}), patch('common.load_config', return_value=config), patch('common.s3_client'):
            spec.loader.exec_module(self.api)
        self.client = TestClient(self.api.APP)
        self.headers = {'Authorization': 'Bearer central-query'}

    def test_shared_archive_capture_filter_and_dedup_keep_source_identity(self):
        capture = str(uuid.uuid4()); event = str(uuid.uuid4())
        events = [{'tenant_id': 'default', 'source_id': s, 'capture_id': capture, 'event_id': event,
                   'sequence': 0, 'kind': 'response', 'metadata': {}, 'data_base64': ''} for s in ('source-a','source-b')]
        with patch.object(self.api, 'query', return_value=[{'archive_key': 'pack'}]) as query, \
             patch.object(self.api, 'read_object', return_value=b'pack'), \
             patch.object(self.api, 'read_pack', side_effect=lambda _: iter(events+events)):
            response = self.client.get('/v1/captures/'+capture, params={'source_id': 'source-a'}, headers=self.headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual([e['source_id'] for e in response.json()['events']], ['source-a'])
            self.assertEqual(query.call_args.args[1]['source'], 'source-a')
            response = self.client.get('/v1/captures/'+capture, headers=self.headers)
            self.assertEqual({e['source_id'] for e in response.json()['events']}, {'source-a','source-b'})

    def test_source_credential_is_never_a_query_credential(self):
        response = self.client.get('/v1/traces/trace', headers={'Authorization': 'Bearer source-token'})
        self.assertEqual(response.status_code, 401)

    def test_trace_source_is_bound_as_a_parameter(self):
        source = "source' OR 1=1 --"
        with patch.object(self.api, 'query', return_value=[]) as query:
            response = self.client.get('/v1/traces/trace', params={'source_id': source}, headers=self.headers)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(source, query.call_args.args[0])
            self.assertEqual(query.call_args.args[1]['source'], source)


if __name__ == '__main__':
    unittest.main()
