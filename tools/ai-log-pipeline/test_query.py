import importlib.util
import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
import uuid

from fastapi.testclient import TestClient
import protocol
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

    def test_latest_requests_accept_full_key_only_in_header(self):
        full_key = 'sk-internal-test-key-1234567890'
        with patch.object(self.api, 'query', return_value=[]) as query:
            response = self.client.get('/v1/requests/latest', headers={**self.headers, 'X-Sub2API-Key': full_key})
            self.assertEqual(response.status_code, 200)
            sql, parameters = query.call_args.args
            self.assertIn('e.api_key = {api_key:String}', sql)
            self.assertEqual(parameters['api_key'], full_key)
            self.assertNotIn(full_key, sql)

    def test_latest_requests_support_legacy_api_key_id(self):
        with patch.object(self.api, 'query', return_value=[]) as query:
            response = self.client.get('/v1/requests/latest', params={'api_key_id': 7}, headers=self.headers)
            self.assertEqual(response.status_code, 200)
            sql, parameters = query.call_args.args
            self.assertIn('e.api_key_id = {api_key_id:Int64}', sql)
            self.assertEqual(parameters['api_key_id'], 7)

    def test_request_columns_extract_json_strings_without_simdjson(self):
        columns = self.api._request_columns()
        self.assertIn('extract(metadata_json, \'"route"[[:space:]]*:[[:space:]]*"([^\"]*)"\') AS route', columns)
        self.assertIn('extract(metadata_json, \'"method"[[:space:]]*:[[:space:]]*"([^\"]*)"\') AS method', columns)
        self.assertIn('extract(metadata_json, \'"request_id"[[:space:]]*:[[:space:]]*"([^\"]*)"\') AS request_id', columns)
        self.assertNotIn('JSONExtractString', columns)

    def test_api_key_stats_keep_one_row_per_key(self):
        with patch.object(self.api, 'query', return_value=[]) as query:
            response = self.client.get('/v1/api-keys/stats', params={'window_seconds': 3600}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        sql, parameters = query.call_args.args
        self.assertIn('uniqExact(e.capture_id) AS request_count', sql)
        self.assertIn("argMaxIf(e.api_key_name, e.event_time, e.api_key_name != '') AS api_key_name", sql)
        self.assertIn("argMaxIf(e.api_key, e.event_time, e.api_key NOT IN ('', '[REDACTED]')) AS api_key", sql)
        self.assertIn('GROUP BY e.source_id, e.api_key_id ORDER BY request_count DESC', sql)
        # One row beyond the limit is read so the response can report truncation.
        self.assertEqual(parameters['limit'], 101)
        self.assertEqual((response.json()['mode'], response.json()['truncated']), ('full', False))

    def test_withheld_key_literal_is_never_shown_as_a_key(self):
        self.assertEqual(self.api.REDACTED, protocol.scrub_metadata({'api_key': 'sk-secret'})['api_key'])
        self.assertIn("if(e.api_key = '[REDACTED]', '', e.api_key) AS api_key", self.api._request_columns())
        metadata = {'api_key_capture': True, 'api_key': '[REDACTED]', 'api_key_id': 7, 'api_key_name': 'worker'}
        self.assertEqual(self.api._public_api_key_metadata(metadata), {'api_key_id': 7, 'api_key_name': 'worker'})

    def test_api_key_stats_brief_mode_keeps_name_key_and_count_only(self):
        rows = [{'source_id': 'source-a', 'api_key_id': 7, 'api_key_name': 'worker', 'api_key': 'sk-secret', 'api_key_prefix': 'sk-secre',
                 'api_key_last4': 'cret', 'api_key_fingerprint': 'f' * 64, 'request_count': 5, 'first_seen': 'a', 'last_seen': 'b'},
                {'source_id': 'source-b', 'api_key_id': 9, 'api_key_name': '', 'api_key': '', 'api_key_prefix': '', 'api_key_last4': '',
                 'api_key_fingerprint': '', 'request_count': 2, 'first_seen': 'a', 'last_seen': 'b'}]
        with patch.object(self.api, 'query', return_value=rows):
            response = self.client.get('/v1/api-keys/stats', params={'mode': 'brief', 'limit': 2}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([sorted(row) for row in response.json()['stats']], [['api_key', 'api_key_name', 'request_count']] * 2)
        self.assertEqual(response.json()['stats'][0], {'api_key_name': 'worker', 'api_key': 'sk-secret', 'request_count': 5})
        # A key its relay never named is still listed, with its identity in place of the name.
        self.assertEqual(response.json()['stats'][1], {'api_key_name': '(unnamed api_key_id=9 at source-b)', 'api_key': '', 'request_count': 2})
        self.assertEqual((response.json()['mode'], response.json()['truncated']), ('brief', False))

    def test_api_key_stats_report_truncation(self):
        row = {'source_id': 'source-a', 'api_key_id': 7, 'api_key_name': 'worker', 'api_key': 'sk-secret', 'request_count': 5}
        with patch.object(self.api, 'query', return_value=[row, dict(row)]):
            response = self.client.get('/v1/api-keys/stats', params={'limit': 1}, headers=self.headers)
        body = response.json()
        self.assertEqual((body['truncated'], len(body['stats'])), (True, 1))

    def test_api_key_stats_reject_an_unknown_mode(self):
        response = self.client.get('/v1/api-keys/stats', params={'mode': 'tiny'}, headers=self.headers)
        self.assertEqual(response.status_code, 422)

    def test_window_of_zero_covers_everything_still_retained(self):
        for path in ('/v1/api-keys/stats', '/v1/requests/latest'):
            with patch.object(self.api, 'query', return_value=[]) as query:
                self.client.get(path, params={'window_seconds': 0}, headers=self.headers)
                self.assertNotIn('parseDateTime64BestEffort({cutoff:String}', query.call_args.args[0])
                self.assertNotIn('cutoff', query.call_args.args[1])
                self.client.get(path, params={'window_seconds': 60}, headers=self.headers)
                self.assertIn("e.event_time >= parseDateTime64BestEffort({cutoff:String}, 3, 'UTC')", query.call_args.args[0])
                self.assertIn('cutoff', query.call_args.args[1])

    def test_capture_exposes_captured_api_key_projection(self):
        capture = str(uuid.uuid4())
        events = [{'tenant_id': 'default', 'source_id': 'source-a', 'capture_id': capture,
                   'event_id': str(uuid.uuid4()), 'sequence': 0, 'kind': 'capture.start',
                   'metadata': {'api_key_capture': True, 'api_key_id': 7, 'api_key': 'sk-secret', 'api_key_name': 'worker',
                                'api_key_prefix': 'sk-secre', 'api_key_last4': 'cret',
                                'api_key_fingerprint': hashlib.sha256(b'sk-secret').hexdigest()},
                   'data_base64': ''}]
        with patch.object(self.api, 'query', return_value=[{'archive_key': 'pack'}]), \
             patch.object(self.api, 'read_object', return_value=b'pack'), \
             patch.object(self.api, 'read_pack', side_effect=lambda _: iter(events)):
            response = self.client.get('/v1/captures/'+capture, headers=self.headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['api_key']['api_key_id'], 7)
            self.assertEqual(response.json()['api_key']['api_key'], 'sk-secret')


if __name__ == '__main__':
    unittest.main()
