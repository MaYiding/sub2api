"""Small durable control registry. No Kafka privilege or plaintext secret storage."""
from contextlib import closing, contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time
import uuid

from cryptography.fernet import Fernet


class Conflict(ValueError):
    pass


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def config_digest(value):
    fields = {key: value[key] for key in ('source_id', 'topic', 'username', 'config_version', 'credential_id', 'probe')}
    return digest(json.dumps(fields, sort_keys=True, separators=(',', ':')))


class Registry:
    def __init__(self, config):
        self.config = config
        self.path = Path(config['database'])
        self.cipher = Fernet(Path(config['encryption_key_file']).read_bytes().strip())

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA synchronous=FULL')
        return db

    @contextmanager
    def transaction(self):
        db = self.connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
            CREATE TABLE IF NOT EXISTS sources (
              id TEXT PRIMARY KEY, name TEXT NOT NULL, region TEXT NOT NULL,
              environment TEXT NOT NULL, topic TEXT UNIQUE NOT NULL,
              state TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
              retention_gib INTEGER NOT NULL, quota_bps INTEGER NOT NULL,
              created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
              heartbeat_at INTEGER, heartbeat_json TEXT, last_credential TEXT,
              observed_json TEXT NOT NULL DEFAULT '{}');
            CREATE TABLE IF NOT EXISTS credentials (
              id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id),
              generation INTEGER NOT NULL, principal TEXT UNIQUE NOT NULL,
              state TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL,
              material BLOB, config_digest TEXT NOT NULL,
              probe_json TEXT NOT NULL, created_at INTEGER NOT NULL,
              expires_at INTEGER NOT NULL, acknowledged_at INTEGER,
              verified_at INTEGER, revoked_at INTEGER,
              UNIQUE(source_id,generation));
            CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id),
              credential_id TEXT, kind TEXT NOT NULL, state TEXT NOT NULL,
              attempts INTEGER NOT NULL DEFAULT 0, retry_at INTEGER NOT NULL DEFAULT 0,
              claimed_at INTEGER, error_class TEXT, created_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS requests (
              key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS audit (
              id INTEGER PRIMARY KEY, at INTEGER NOT NULL, actor TEXT NOT NULL,
              action TEXT NOT NULL, target TEXT NOT NULL, detail TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions (
              token_hash TEXT PRIMARY KEY, csrf_hash TEXT NOT NULL, expires_at INTEGER NOT NULL);
            ''')
            if 'topic_ready' not in {row[1] for row in db.execute('PRAGMA table_info(sources)')}:
                db.execute('ALTER TABLE sources ADD COLUMN topic_ready INTEGER NOT NULL DEFAULT 0')
                db.execute("UPDATE sources SET topic_ready=1 WHERE state IN ('ready','active','disabled','disabling')")
        os.chmod(self.path, 0o660)

    @staticmethod
    def audit(db, actor, action, target, detail=''):
        db.execute('INSERT INTO audit(at,actor,action,target,detail) VALUES(?,?,?,?,?)',
                   (int(time.time()), actor, action, target, detail[:256]))

    @staticmethod
    def fields(data):
        result = {}
        for name in ('name', 'region', 'environment'):
            value = data.get(name, 'production' if name == 'environment' else '')
            if not isinstance(value, str) or len(value.strip()) > 128 or any(ord(c) < 32 for c in value):
                raise ValueError('invalid source label')
            result[name] = value.strip()
        if not result['name']:
            raise ValueError('source name is required')
        for name, default, low, high in [('retention_gib', 192, 6, 384), ('quota_bps', 2097152, 65536, 16777216)]:
            value = data.get(name, default)
            if type(value) is not int or not low <= value <= high:
                raise ValueError('invalid source capacity or rate')
            result[name] = value
        return result

    def new_credential(self, db, source):
        generation = db.execute('SELECT coalesce(max(generation),0)+1 FROM credentials WHERE source_id=?', (source['id'],)).fetchone()[0]
        cid = 'cred-' + uuid.uuid4().hex
        principal = 'ingest-' + source['id'] + '-g' + str(generation)
        token = secrets.token_urlsafe(36)
        now = int(time.time())
        probe = {'event_id': str(uuid.uuid4()), 'capture_id': str(uuid.uuid4()),
                 'trace_id': 'source-probe-' + uuid.uuid4().hex,
                 'nonce': secrets.token_urlsafe(24), 'timestamp': now}
        bundle = {'bootstrap_servers': self.config['public_brokers'], 'username': principal,
                  'password': secrets.token_urlsafe(36), 'topic': source['topic'],
                  'source_id': source['id'], 'source_name': source['name'],
                  'spool_dir': '/var/lib/sub2api-ai-log/spool',
                  'config_version': generation, 'credential_id': cid, 'probe': probe,
                  'control': {'endpoint': self.config['public_origin'], 'source_token': token}}
        checksum = config_digest(bundle)
        bundle['config_digest'] = checksum
        encrypted = self.cipher.encrypt(json.dumps(bundle).encode())
        db.execute('''INSERT INTO credentials(id,source_id,generation,principal,state,token_hash,material,
                      config_digest,probe_json,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                   (cid, source['id'], generation, principal, 'pending', digest(token), encrypted,
                    checksum, json.dumps(probe), now, now + 86400))
        return cid

    def enqueue(self, db, source, kind, cid=None):
        jid = 'job-' + uuid.uuid4().hex
        db.execute('INSERT INTO jobs(id,source_id,credential_id,kind,state,created_at) VALUES(?,?,?,?,?,?)',
                   (jid, source, cid, kind, 'pending', int(time.time())))
        return jid

    def mutate(self, action, data, key, source_id=None, actor='admin'):
        if not isinstance(key, str) or not 8 <= len(key) <= 160:
            raise ValueError('Idempotency-Key is required')
        fingerprint = digest(json.dumps([action, source_id, data], sort_keys=True))
        with self.transaction() as db:
            previous = db.execute('SELECT * FROM requests WHERE key=?', (key,)).fetchone()
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise Conflict('idempotency key reused with different input')
                return json.loads(previous['result_json'])
            if action == 'create':
                fields = self.fields(data)
                used = db.execute('SELECT coalesce(sum(retention_gib),0) FROM sources').fetchone()[0]
                if used + fields['retention_gib'] > self.config.get('raw_budget_gib', 384):
                    raise Conflict('raw topic capacity budget exhausted')
                source_id = 'src-' + secrets.token_hex(6)
                now = int(time.time())
                db.execute('''INSERT INTO sources(id,name,region,environment,topic,state,retention_gib,quota_bps,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?)''',
                           (source_id, fields['name'], fields['region'], fields['environment'],
                            'ai.raw.' + source_id + '.v1', 'provisioning', fields['retention_gib'], fields['quota_bps'], now, now))
                source = dict(db.execute('SELECT * FROM sources WHERE id=?', (source_id,)).fetchone())
                cid = self.new_credential(db, source)
                jid = self.enqueue(db, source_id, 'create', cid)
            else:
                source = db.execute('SELECT * FROM sources WHERE id=?', (source_id,)).fetchone()
                if not source:
                    raise ValueError('unknown source')
                if action != 'disable' and db.execute("SELECT 1 FROM jobs WHERE source_id=? AND state IN ('pending','running')", (source_id,)).fetchone():
                    raise Conflict('source has an unfinished operation')
                cid = None
                if action == 'enable':
                    if source['state'] != 'disabled':
                        raise Conflict('source is not disabled')
                    cid = self.new_credential(db, dict(source))
                    db.execute("UPDATE sources SET state='provisioning',updated_at=? WHERE id=?", (int(time.time()), source_id))
                    jid = self.enqueue(db, source_id, 'enable', cid)
                elif action == 'rotate':
                    if source['state'] in ('disabled', 'disabling'):
                        raise Conflict('source is disabled')
                    pending = db.execute("SELECT count(*) FROM credentials WHERE source_id=? AND state!='revoked'", (source_id,)).fetchone()[0]
                    if pending >= 2:
                        raise Conflict('finish the existing rotation first')
                    cid = self.new_credential(db, dict(source))
                    jid = self.enqueue(db, source_id, 'rotate', cid)
                elif action == 'confirm':
                    cid = data.get('credential_id')
                    credential = db.execute('SELECT * FROM credentials WHERE id=? AND source_id=?', (cid, source_id)).fetchone()
                    latest = db.execute("SELECT id FROM credentials WHERE source_id=? AND state!='revoked' ORDER BY generation DESC LIMIT 1", (source_id,)).fetchone()
                    if not credential or source['state'] in ('disabled', 'disabling') or credential['state'] not in ('ready', 'active') or cid != latest['id'] or not credential['verified_at'] or not credential['acknowledged_at']:
                        raise Conflict('latest credentials need source acknowledgement and archived probe')
                    jid = self.enqueue(db, source_id, 'confirm', cid)
                elif action == 'cancel_rotation':
                    credentials = db.execute("SELECT * FROM credentials WHERE source_id=? AND state!='revoked' ORDER BY generation DESC", (source_id,)).fetchall()
                    if len(credentials) != 2 or source['state'] in ('disabled', 'disabling'):
                        raise Conflict('no pending rotation to cancel')
                    cid = credentials[0]['id']
                    jid = self.enqueue(db, source_id, 'cancel_rotation', cid)
                elif action == 'disable':
                    db.execute("UPDATE jobs SET state='cancelled' WHERE source_id=? AND state='pending'", (source_id,))
                    db.execute("UPDATE sources SET state='disabling',updated_at=? WHERE id=?", (int(time.time()), source_id))
                    jid = self.enqueue(db, source_id, 'disable')
                elif action == 'rename':
                    fields = self.fields(dict(source) | data)
                    db.execute('UPDATE sources SET name=?,region=?,environment=?,revision=revision+1,updated_at=? WHERE id=?',
                               (fields['name'], fields['region'], fields['environment'], int(time.time()), source_id))
                    jid = self.enqueue(db, source_id, 'rename')
                else:
                    raise ValueError('unknown operation')
            result = {'source_id': source_id, 'job_id': jid, 'credential_id': cid}
            db.execute('INSERT INTO requests VALUES(?,?,?)', (key, fingerprint, json.dumps(result)))
            self.audit(db, actor, action, source_id)
            return result

    def sources(self):
        with closing(self.connect()) as db:
            result = []
            for row in db.execute('SELECT * FROM sources ORDER BY created_at,id'):
                item = dict(row)
                item['heartbeat'] = json.loads(item.pop('heartbeat_json') or '{}')
                item['observed'] = json.loads(item.pop('observed_json') or '{}')
                item['credentials'] = [dict(c) for c in db.execute('''SELECT id,generation,principal,state,created_at,expires_at,
                   acknowledged_at,verified_at,revoked_at FROM credentials WHERE source_id=? ORDER BY generation DESC''', (row['id'],))]
                item['jobs'] = [dict(c) for c in db.execute('SELECT id,kind,state,attempts,error_class FROM jobs WHERE source_id=? ORDER BY created_at DESC LIMIT 5', (row['id'],))]
                result.append(item)
            return result

    def bundle(self, source_id, cid=None):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM credentials WHERE source_id=? AND state!='revoked' AND (? IS NULL OR id=?) ORDER BY generation DESC LIMIT 1", (source_id, cid, cid)).fetchone()
            if not row or row['state'] not in ('ready', 'active') or row['expires_at'] < time.time() or not row['material']:
                raise Conflict('configuration is not ready or download window expired; rotate credentials')
            self.audit(db, 'admin', 'bundle.download', source_id, row['id'])
            return json.loads(self.cipher.decrypt(row['material']))

    def heartbeat(self, token, payload):
        with self.transaction() as db:
            row = db.execute("SELECT c.* FROM credentials c JOIN sources s ON s.id=c.source_id WHERE c.token_hash=? AND c.state IN ('ready','active') AND s.state NOT IN ('disabled','disabling')", (digest(token),)).fetchone()
            if not row or payload.get('source_id') != row['source_id'] or payload.get('credential_id') != row['id']:
                raise PermissionError('invalid source identity')
            if not hmac.compare_digest(str(payload.get('config_digest', '')), row['config_digest']):
                raise PermissionError('invalid configuration digest')
            fields = {}
            for name in ('pending_bytes', 'oldest_age_seconds', 'delivered_events', 'failures', 'last_delivery_at'):
                value = payload.get(name, 0)
                if type(value) is not int or not 0 <= value <= 2**63-1:
                    raise ValueError('invalid heartbeat counter')
                fields[name] = value
            for name in ('hostname', 'instance_id', 'shipper_version'):
                value = payload.get(name, '')
                if not isinstance(value, str) or len(value) > 128:
                    raise ValueError('invalid heartbeat label')
                fields[name] = value
            now = int(time.time())
            db.execute('UPDATE credentials SET acknowledged_at=? WHERE id=?', (now, row['id']))
            db.execute('UPDATE sources SET heartbeat_at=?,heartbeat_json=?,last_credential=? WHERE id=?',
                       (now, json.dumps(fields), row['id'], row['source_id']))
            return {'ok': True, 'source_id': row['source_id']}

    def claim_job(self):
        with self.transaction() as db:
            now = int(time.time())
            db.execute("UPDATE jobs SET state='pending' WHERE state='running' AND claimed_at<?", (now-300,))
            row = db.execute("SELECT * FROM jobs WHERE state='pending' AND retry_at<=? ORDER BY created_at,id LIMIT 1", (now,)).fetchone()
            if row:
                db.execute("UPDATE jobs SET state='running',attempts=attempts+1,claimed_at=? WHERE id=?", (now, row['id']))
                return dict(row)

    def finish_job(self, job, error=None):
        with self.transaction() as db:
            if error:
                db.execute("UPDATE jobs SET state='pending',error_class=?,retry_at=? WHERE id=?",
                           (type(error).__name__, int(time.time()) + min(300, 5*2**min(job['attempts'], 6)), job['id']))
            else:
                db.execute("UPDATE jobs SET state='done',error_class=NULL WHERE id=?", (job['id'],))
                self.audit(db, 'provisioner', job['kind'] + '.applied', job['source_id'])

    def maintenance(self):
        with self.transaction() as db:
            now = int(time.time())
            # Pending jobs still need the original password for idempotent recovery.
            db.execute("UPDATE credentials SET material=NULL WHERE expires_at<? AND state!='pending'", (now,))
            db.execute('DELETE FROM sessions WHERE expires_at<?', (now,))

    def backup(self, destination):
        with closing(self.connect()) as source, closing(sqlite3.connect(destination)) as target:
            source.backup(target)
