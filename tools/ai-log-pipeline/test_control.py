from contextlib import closing
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
import uuid

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from control import create_app
from registry import Conflict, Registry, digest
from provisioner import Provisioner
from protocol import normalize, make_pack, read_pack
from source_bindings import validate_snapshot
from shipper_control import prepare_probe, Heartbeat


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        key = self.root/'key'
        key.write_bytes(Fernet.generate_key())
        self.config = {'database': str(self.root/'registry.sqlite'), 'encryption_key_file': str(key),
          'public_brokers': 'broker.example:443', 'public_origin': 'https://logs.example',
          'admin_networks': ['127.0.0.0/8'], 'trusted_proxy_ips': ['127.0.0.1'],
          'admin_token_hash': digest('admin-secret'), 'session_secret': 'session-secret',
          'admin_password_salt': '01'*16,
          'admin_password_hash': hashlib.scrypt(b'admin-password', salt=b'\x01'*16, n=16384, r=8, p=1).hex()}
        self.registry = Registry(self.config)
        self.registry.initialize()
        self.client = TestClient(create_app(self.config), base_url='https://logs.example', client=('127.0.0.1', 40000))
        self.client.headers.update({'X-Real-IP': '127.0.0.1'})

    def create(self, name='Test source', capacity=192):
        result = self.registry.mutate('create', {'name': name, 'retention_gib': capacity}, str(uuid.uuid4()))
        with self.registry.transaction() as db:
            db.execute("UPDATE credentials SET state='ready' WHERE id=?", (result['credential_id'],))
            db.execute("UPDATE jobs SET state='done' WHERE id=?", (result['job_id'],))
            db.execute("UPDATE sources SET state='ready',topic_ready=1 WHERE id=?", (result['source_id'],))
        return result, self.registry.bundle(result['source_id'])

    def heartbeat(self, bundle):
        return {'source_id': bundle['source_id'], 'credential_id': bundle['credential_id'], 'config_digest': bundle['config_digest']}

    def test_idempotency_capacity_and_secret_storage(self):
        key = str(uuid.uuid4())
        one = self.registry.mutate('create', {'name': 'A'}, key)
        self.assertEqual(self.registry.mutate('create', {'name': 'A'}, key), one)
        with self.assertRaises(Conflict):
            self.registry.mutate('create', {'name': 'B'}, key)
        two, bundle = self.create('B')
        with self.assertRaises(Conflict):
            self.registry.mutate('create', {'name': 'C', 'retention_gib': 6}, str(uuid.uuid4()))
        public = json.dumps(self.registry.sources())
        for secret in (bundle['password'], bundle['control']['source_token']):
            self.assertNotIn(secret, public)
            for path in self.root.glob('registry.sqlite*'):
                self.assertNotIn(secret.encode(), path.read_bytes())

    def test_source_token_cannot_spoof_another_source_or_act_as_admin(self):
        _, a = self.create('A'); _, b = self.create('B')
        payload = self.heartbeat(a)
        token = a['control']['source_token']
        self.assertTrue(self.registry.heartbeat(token, payload)['ok'])
        with self.assertRaises(PermissionError):
            self.registry.heartbeat(token, self.heartbeat(b))
        with self.assertRaises(PermissionError):
            self.registry.heartbeat(token, dict(payload, config_digest='fake'))
        response = self.client.get('/control/v1/sources', headers={'Authorization': 'Bearer '+token})
        self.assertEqual(response.status_code, 401)
        response = self.client.post('/agent/v1/heartbeat', json=self.heartbeat(b), headers={'Authorization': 'Bearer '+token})
        self.assertEqual(response.status_code, 401)

    def test_browser_login_download_csrf_and_network_restrictions(self):
        created, _ = self.create()
        response = self.client.post('/control/v1/login', json={'username': 'admin', 'password': 'admin-password'}, headers={'Origin': 'https://logs.example'})
        self.assertEqual(response.status_code, 200)
        csrf = response.json()['csrf']
        self.assertIn('Secure', response.headers['set-cookie'])
        path = '/control/v1/sources/'+created['source_id']+'/bundle'
        self.assertEqual(self.client.post(path).status_code, 403)
        self.assertEqual(self.client.post(path, headers={'Origin': 'https://evil.example', 'X-CSRF-Token': csrf}).status_code, 403)
        response = self.client.post(path, headers={'Origin': 'https://logs.example', 'X-CSRF-Token': csrf})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(self.client.get('/control/v1/sources', headers={'X-Real-IP': '203.0.113.10'}).status_code, 403)

    def test_rotation_requires_acknowledgement_and_archived_probe(self):
        created, _ = self.create()
        rotated = self.registry.mutate('rotate', {}, str(uuid.uuid4()), created['source_id'])
        with self.registry.transaction() as db:
            db.execute("UPDATE jobs SET state='done' WHERE id=?", (rotated['job_id'],))
            db.execute("UPDATE credentials SET state='ready' WHERE id=?", (rotated['credential_id'],))
        bundle = self.registry.bundle(created['source_id'])
        data = {'credential_id': rotated['credential_id']}
        with self.assertRaises(Conflict):
            self.registry.mutate('confirm', data, str(uuid.uuid4()), created['source_id'])
        self.registry.heartbeat(bundle['control']['source_token'], self.heartbeat(bundle))
        with self.assertRaises(Conflict):
            self.registry.mutate('confirm', data, str(uuid.uuid4()), created['source_id'])
        with self.registry.transaction() as db:
            db.execute('UPDATE credentials SET verified_at=? WHERE id=?', (int(time.time()), rotated['credential_id']))
        self.assertTrue(self.registry.mutate('confirm', data, str(uuid.uuid4()), created['source_id'])['job_id'])

    def test_disable_revokes_all_generations_and_retains_binding(self):
        created, bundle = self.create()
        disabled = self.registry.mutate('disable', {}, str(uuid.uuid4()), created['source_id'])
        with self.assertRaises(PermissionError):
            self.registry.heartbeat(bundle['control']['source_token'], self.heartbeat(bundle))
        calls = []
        worker = Provisioner(self.config, lambda config, role, value: calls.append((role, value)) or {'ok': True})
        job = self.registry.claim_job(); self.assertEqual(job['id'], disabled['job_id'])
        worker.apply(job); self.registry.finish_job(job)
        self.assertEqual(calls[0][1]['operation'], 'revoke')
        self.assertEqual(self.registry.sources()[0]['state'], 'disabled')
        self.assertIn(bundle['topic'], worker.snapshot()['bindings'])
        with self.assertRaises(Conflict):
            self.registry.bundle(created['source_id'])
        enabled = self.registry.mutate('enable', {}, str(uuid.uuid4()), created['source_id'])
        self.assertEqual(enabled['source_id'], created['source_id'])
        self.assertNotEqual(enabled['credential_id'], bundle['credential_id'])
        with self.assertRaises(PermissionError):
            self.registry.heartbeat(bundle['control']['source_token'], self.heartbeat(bundle))

    def test_expired_material_removed_and_registry_backup_restorable(self):
        created, bundle = self.create()
        with self.registry.transaction() as db:
            db.execute('UPDATE credentials SET expires_at=0')
        self.registry.maintenance()
        with self.assertRaises(Conflict):
            self.registry.bundle(created['source_id'])
        self.registry.backup(self.root/'backup.sqlite')
        restored = Registry(dict(self.config, database=str(self.root/'backup.sqlite')))
        self.assertEqual(restored.sources()[0]['id'], created['source_id'])
        with closing(restored.connect()) as db:
            self.assertIsNone(db.execute('SELECT material FROM credentials').fetchone()[0])

    def test_concurrent_registration_does_not_publish_uncreated_topic(self):
        _, ready = self.create('Ready')
        pending = self.registry.mutate('create', {'name': 'Not created'}, str(uuid.uuid4()))
        snapshot = Provisioner(self.config).snapshot()
        self.assertEqual(set(snapshot['bindings']), {ready['topic']})
        self.assertNotIn('ai.raw.'+pending['source_id']+'.v1', snapshot['bindings'])

    def test_cancel_rotation_restores_download_of_previous_generation(self):
        source, first = self.create()
        rotated = self.registry.mutate('rotate', {}, str(uuid.uuid4()), source['source_id'])
        with self.registry.transaction() as db:
            db.execute("UPDATE jobs SET state='done' WHERE id=?", (rotated['job_id'],))
            db.execute("UPDATE credentials SET state='ready' WHERE id=?", (rotated['credential_id'],))
        self.registry.mutate('cancel_rotation', {}, str(uuid.uuid4()), source['source_id'])
        worker = Provisioner(self.config, lambda config, role, value: {'ok': True})
        job = self.registry.claim_job(); worker.apply(job); self.registry.finish_job(job)
        self.assertEqual(self.registry.bundle(source['source_id'])['credential_id'], first['credential_id'])

    def test_provenance_is_authoritative_and_archive_keeps_colliding_ids(self):
        _, a = self.create('A'); _, b = self.create('B')
        event = {'schema_version': 1, 'event_id': str(uuid.uuid4()), 'capture_id': str(uuid.uuid4()),
                 'trace_id': 'same-trace', 'sequence': 0, 'kind': 'response', 'timestamp': '2026-10-09T00:00:00Z',
                 'data_base64': 'aGk=', 'source_name': 'forged', 'source_verified': True}
        bindings = Provisioner(self.config).snapshot()
        validate_snapshot(bindings)
        with self.assertRaises(ValueError):
            validate_snapshot({'version': 1, 'bindings': {a['topic']: dict(bindings['bindings'][a['topic']], legacy=True)}})
        normalized = []
        for bundle in (a, b):
            binding = bindings['bindings'][bundle['topic']]
            raw = json.dumps(dict(event, source_id=bundle['source_id'])).encode()
            result = normalize(raw, 'default', binding)
            self.assertEqual(result['source_name'], binding['name'])
            self.assertTrue(result['source_verified'])
            normalized.append(result)
        with self.assertRaises(ValueError):
            normalize(json.dumps(dict(event, source_id=b['source_id'])).encode(), 'default', bindings['bindings'][a['topic']])
        blob, stats = make_pack(normalized)
        self.assertEqual(stats['events'], 2)
        self.assertEqual({e['source_id'] for e in read_pack(blob)}, {a['source_id'], b['source_id']})

    def test_probe_and_instance_stable_and_heartbeat_failure_does_not_remove_wal(self):
        _, bundle = self.create()
        spool = self.root/'spool'; spool.mkdir()
        instance = prepare_probe(bundle, spool)
        path = next(spool.glob('*.ready'))
        original = path.read_bytes()
        self.assertEqual(instance, prepare_probe(bundle, spool))
        self.assertEqual(path.read_bytes(), original)
        heartbeat = Heartbeat(bundle, spool, instance)
        class Offline:
            def open(self, *args, **kwargs):
                raise OSError('offline')
        heartbeat.opener = Offline()
        with self.assertRaises(OSError):
            heartbeat.send()
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(heartbeat.payload()['pending_bytes'], len(original))


if __name__ == '__main__':
    unittest.main()
