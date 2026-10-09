"""Single privileged reconciler. Web credentials cannot run remote helpers."""
import argparse
from contextlib import closing
import fcntl
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import time

import requests

from common import load_config, status
from registry import Registry

LOG = logging.getLogger('ai-log-control')
STOP = False


def remote(config, role, value):
    endpoint = config['helpers'][role]
    result = subprocess.run(['ssh', '-T', '-i', endpoint['key'], '-o', 'IdentitiesOnly=yes',
              '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ControlMaster=no',
              '-o', 'UserKnownHostsFile='+config['known_hosts'], '-o', 'ConnectTimeout=10',
              '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2', endpoint['host']],
              input=json.dumps(value).encode(), capture_output=True, timeout=240)
    if result.returncode:
        # stderr may contain authentication material in third-party tooling.
        raise RuntimeError('remote helper failed')
    response = json.loads(result.stdout)
    if response.get('ok') is not True:
        raise RuntimeError('remote helper rejected operation')
    return response


class Provisioner:
    def __init__(self, config, executor=remote):
        self.config, self.registry, self.execute = config, Registry(config), executor

    def snapshot(self):
        return {'version': 1, 'bindings': {s['topic']: {'source_id': s['id'], 'tenant_id': 'default',
                    'name': s['name'], 'region': s['region'], 'revision': s['revision']}
                    for s in self.registry.sources() if s['topic_ready']}}

    def publish(self):
        result = self.execute(self.config, 'bindings', {'operation': 'apply', 'snapshot': self.snapshot()})
        deadline = time.monotonic()+45
        while result['expected_revision'] != result['loaded_revision'] or time.time()-result['updated_at'] > 60:
            if time.monotonic() >= deadline:
                raise TimeoutError('cleaner has not acknowledged source bindings')
            time.sleep(2)
            result = self.execute(self.config, 'bindings', {'operation': 'status'})

    def credentials(self, source_id):
        with closing(self.registry.connect()) as db:
            return [dict(c) for c in db.execute("SELECT * FROM credentials WHERE source_id=? AND state!='revoked' ORDER BY generation", (source_id,))]

    def apply(self, job):
        source = next(s for s in self.registry.sources() if s['id'] == job['source_id'])
        credentials = self.credentials(source['id'])
        if job['kind'] in ('create', 'rotate', 'enable'):
            if source['state'] in ('disabling', 'disabled'):
                return  # The queued disable reconciles every credential.
            credential = next(c for c in credentials if c['id'] == job['credential_id'])
            bundle = json.loads(self.registry.cipher.decrypt(credential['material']))
            quota = source['quota_bps']//len(credentials)
            for old in credentials:
                if old['id'] != credential['id']:
                    self.execute(self.config, 'kafka', {'operation': 'quota', 'source_id': source['id'], 'principal': old['principal'], 'quota_bps': quota})
            self.execute(self.config, 'kafka', {'operation': 'apply', 'source_id': source['id'], 'principal': credential['principal'],
                          'password': bundle['password'], 'retention_gib': source['retention_gib'], 'quota_bps': quota})
            # Concurrent registrations must never subscribe the cleaner to a
            # different source whose topic/ACL has not been created yet.
            with self.registry.transaction() as db:
                db.execute('UPDATE sources SET topic_ready=1 WHERE id=?', (source['id'],))
            self.publish()
            with self.registry.transaction() as db:
                current = db.execute('SELECT state FROM sources WHERE id=?', (source['id'],)).fetchone()[0]
                if current not in ('disabled', 'disabling'):
                    db.execute("UPDATE credentials SET state='ready',expires_at=? WHERE id=?", (int(time.time())+86400, credential['id']))
                    db.execute("UPDATE sources SET state=CASE WHEN state='active' THEN state ELSE 'ready' END WHERE id=?", (source['id'],))
        elif job['kind'] in ('confirm', 'disable', 'cancel_rotation'):
            revoked = [c for c in credentials if job['kind'] == 'disable' or (c['id'] == job['credential_id'] if job['kind'] == 'cancel_rotation' else c['id'] != job['credential_id'])]
            for credential in revoked:
                self.execute(self.config, 'kafka', {'operation': 'revoke', 'source_id': source['id'], 'principal': credential['principal']})
                with self.registry.transaction() as db:
                    db.execute("UPDATE credentials SET state='revoked',material=NULL,revoked_at=? WHERE id=?", (int(time.time()), credential['id']))
            if job['kind'] in ('confirm', 'cancel_rotation'):
                selected = next(c for c in credentials if c not in revoked)
                self.execute(self.config, 'kafka', {'operation': 'quota', 'source_id': source['id'], 'principal': selected['principal'], 'quota_bps': source['quota_bps']})
            with self.registry.transaction() as db:
                db.execute('UPDATE sources SET state=?,updated_at=? WHERE id=?', ('disabled' if job['kind'] == 'disable' else ('active' if selected['verified_at'] else 'ready'), int(time.time()), source['id']))
        elif job['kind'] == 'rename':
            self.publish()
        else:
            raise ValueError('unknown job kind')

    def query(self, path, parameters=None):
        response = requests.get(self.config['query_endpoint']+path, params=parameters,
                    headers={'Authorization': 'Bearer '+self.config['query_token']}, timeout=(5, 30))
        if response.status_code != 200:
            raise RuntimeError('query verification failed')
        return response.json()

    def observe(self):
        activity = self.query('/v1/sources/activity')['sources']
        by_source = {}
        for row in activity:
            by_source.setdefault(row['source_id'], {})[row['kind']] = row
        with self.registry.transaction() as db:
            for sid, observed in by_source.items():
                db.execute('UPDATE sources SET observed_json=? WHERE id=?', (json.dumps(observed), sid))
        for source in self.registry.sources():
            if source['state'] in ('disabled', 'disabling'):
                continue
            for credential in self.credentials(source['id']):
                if credential['verified_at'] or not credential['acknowledged_at'] or credential['state'] not in ('ready', 'active'):
                    continue
                probe = json.loads(credential['probe_json'])
                replay = self.query('/v1/captures/'+probe['capture_id'], {'source_id': source['id']})
                for event in replay['events']:
                    if (event['event_id'] == probe['event_id'] and event['source_id'] == source['id']
                        and event.get('source_verified') is True and event.get('raw_topic') == source['topic']
                        and event['kind'] == 'source.probe' and event['metadata'].get('nonce') == probe['nonce']
                        and event['metadata'].get('credential_id') == credential['id']):
                        with self.registry.transaction() as db:
                            db.execute("UPDATE credentials SET verified_at=?,state='active' WHERE id=? AND state='ready'", (int(time.time()), credential['id']))
                            db.execute("UPDATE sources SET state='active' WHERE id=? AND state='ready'", (source['id'],))
                            self.registry.audit(db, 'provisioner', 'probe.archived', source['id'], credential['id'])
                        break


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    config = load_config(args.config)
    worker = Provisioner(config)
    descriptor = os.open(str(worker.registry.path)+'.provisioner.lock', os.O_CREAT | os.O_RDWR, 0o660)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    def stop(_signal, _frame):
        global STOP
        STOP = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    logging.basicConfig(level=logging.INFO)
    observed_at, backed_up_at = 0, 0
    counters = {'jobs_applied': 0, 'failures': 0, 'observe_success_at': 0}
    while not STOP:
        job = worker.registry.claim_job()
        if job:
            try:
                worker.apply(job)
            except Exception as error:
                worker.registry.finish_job(job, error)
                counters['failures'] += 1
                LOG.error('job_failed id=%s class=%s', job['id'], type(error).__name__)
            else:
                worker.registry.finish_job(job)
                counters['jobs_applied'] += 1
        now = int(time.time())
        if now-observed_at >= 30:
            try:
                worker.observe()
                worker.registry.maintenance()
                counters['observe_success_at'] = now
            except Exception as error:
                LOG.error('observation_failed class=%s', type(error).__name__)
            observed_at = now
        if now-backed_up_at >= 3600:
            worker.registry.backup(config['backup_path'])
            backed_up_at = now
        counters['updated_at'] = int(time.time())
        status(config, counters)
        if not job:
            time.sleep(2)


if __name__ == '__main__':
    main()
