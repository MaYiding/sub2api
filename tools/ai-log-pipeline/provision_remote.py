"""Root-owned forced-command helpers. Input is bounded JSON, never shell code.

Install with a fixed --role; the SSH account receives no shell or forwarding.
Kafka secrets reach kafka-configs through a private temporary file, not argv.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

from source_bindings import validate_snapshot

DEADLINE = None


def run(tool, args):
    timeout = min(75, DEADLINE-time.monotonic()) if DEADLINE else 75
    if timeout <= 0:
        raise TimeoutError('helper deadline exceeded')
    result = subprocess.run(['/opt/kafka/bin/kafka-'+tool+'.sh', '--bootstrap-server', '10.10.20.230:9092',
                             '--command-config', '/etc/kafka/admin.properties', *args],
                            capture_output=True, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError('Kafka administration failed')
    return result.stdout.decode()


def user_config(principal, values):
    directory = Path('/run/ai-log-kafka-admin')
    directory.mkdir(mode=0o700, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=directory) as config:
        os.chmod(config.name, 0o600)
        config.write('\n'.join(k+'='+str(v) for k, v in values.items())+'\n')
        config.flush()
        run('configs', ['--alter', '--entity-type', 'users', '--entity-name', principal, '--add-config-file', config.name])


def kafka(value):
    source = value.get('source_id', '')
    if re.fullmatch(r'src-[a-f0-9]{12}', source) is None:
        raise ValueError('invalid source')
    principal = value.get('principal', '')
    if re.fullmatch('ingest-'+source+r'-g[1-9][0-9]{0,5}', principal) is None:
        raise ValueError('invalid principal')
    topic = 'ai.raw.'+source+'.v1'
    operation = value.get('operation')
    if operation in ('apply', 'quota'):
        quota = value.get('quota_bps')
        if type(quota) is not int or not 32768 <= quota <= 16777216:
            raise ValueError('invalid quota')
        if operation == 'quota':
            user_config(principal, {'producer_byte_rate': quota})
            return {'ok': True}
        budget = value.get('retention_gib')
        password = value.get('password', '')
        if type(budget) is not int or not 6 <= budget <= 384 or re.fullmatch(r'[A-Za-z0-9_-]{40,100}', password) is None:
            raise ValueError('invalid source settings')
        settings = {'cleanup.policy': 'delete', 'min.insync.replicas': '2',
                    'retention.ms': str(72*3600*1000), 'retention.bytes': str(budget*1024**3//6),
                    'segment.bytes': str(256*1024**2), 'segment.ms': '3600000',
                    'max.message.bytes': '1048576', 'unclean.leader.election.enable': 'false'}
        args = ['--create', '--if-not-exists', '--topic', topic, '--partitions', '6', '--replication-factor', '3']
        for key, item in settings.items():
            args += ['--config', key+'='+item]
        run('topics', args)
        description = run('topics', ['--describe', '--topic', topic])
        if 'PartitionCount: 6' not in description or 'ReplicationFactor: 3' not in description:
            raise ValueError('existing source topic has unexpected topology')
        run('configs', ['--alter', '--entity-type', 'topics', '--entity-name', topic,
                        '--add-config', ','.join(k+'='+v for k, v in settings.items())])
        # Provisioning retries use the same encrypted password and identity.
        user_config(principal, {'SCRAM-SHA-512': 'iterations=8192,password='+password})
        user_config(principal, {'producer_byte_rate': quota})
        run('acls', ['--add', '--allow-principal', 'User:CN=cleaner', '--operation', 'Read', '--operation', 'Describe', '--topic', topic])
        run('acls', ['--add', '--allow-principal', 'User:'+principal, '--operation', 'Write', '--operation', 'Describe', '--topic', topic])
        run('acls', ['--add', '--allow-principal', 'User:'+principal, '--operation', 'IdempotentWrite', '--cluster'])
    elif operation == 'revoke':
        # ACL revocation applies to already authenticated connections too.
        run('acls', ['--remove', '--force', '--allow-principal', 'User:'+principal, '--operation', 'Write', '--operation', 'Describe', '--topic', topic])
        run('acls', ['--remove', '--force', '--allow-principal', 'User:'+principal, '--operation', 'IdempotentWrite', '--cluster'])
        current = run('configs', ['--describe', '--entity-type', 'users', '--entity-name', principal])
        if 'SCRAM-SHA-512=' in current:
            run('configs', ['--alter', '--entity-type', 'users', '--entity-name', principal, '--delete-config', 'SCRAM-SHA-512'])
        if 'producer_byte_rate=' in current:
            run('configs', ['--alter', '--entity-type', 'users', '--entity-name', principal, '--delete-config', 'producer_byte_rate'])
    else:
        raise ValueError('unknown operation')
    return {'ok': True, 'source_id': source, 'operation': operation}


def bindings(value):
    path = Path('/etc/ai-logs/pipeline/source-bindings.json')
    if value.get('operation') == 'apply':
        snapshot = value['snapshot']
        mapping = validate_snapshot(snapshot)
        if path.exists():
            previous = validate_snapshot(json.loads(path.read_bytes()))
            if not set(previous).issubset(mapping):
                raise ValueError('historic source bindings cannot be removed')
            if any(mapping[k]['revision'] < v['revision'] for k, v in previous.items()):
                raise ValueError('binding revision cannot regress')
        raw = json.dumps(snapshot, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
        if not path.exists() or path.read_bytes() != raw:
            import grp
            fd, temp = tempfile.mkstemp(dir=path.parent)
            try:
                os.fchmod(fd, 0o640)
                os.fchown(fd, 0, grp.getgrnam('ailog').gr_gid)
                with os.fdopen(fd, 'wb') as out:
                    out.write(raw); out.flush(); os.fsync(out.fileno())
                os.replace(temp, path)
                directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)
    elif value.get('operation') != 'status':
        raise ValueError('unknown operation')
    expected = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    status_path = Path('/var/lib/ai-log-pipeline/cleaner-status.json')
    status = json.loads(status_path.read_bytes()) if status_path.exists() else {}
    return {'ok': True, 'expected_revision': expected, 'loaded_revision': status.get('binding_revision'), 'updated_at': status.get('updated_at', 0)}


def main():
    global DEADLINE
    parser = argparse.ArgumentParser()
    parser.add_argument('--role', choices=('kafka', 'bindings'), required=True)
    args = parser.parse_args()
    try:
        # A timed-out SSH caller must not leave overlapping apply/revoke
        # sequences. The helper finishes before the caller's 240-second limit.
        descriptor = os.open('/run/ai-log-provision-'+args.role+'.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        DEADLINE = time.monotonic()+210
        raw = sys.stdin.buffer.read((1 << 20)+1)
        if len(raw) > 1 << 20:
            raise ValueError('input too large')
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError('input must be an object')
        result = kafka(value) if args.role == 'kafka' else bindings(value)
        print(json.dumps(result))
    except Exception as error:
        print(json.dumps({'ok': False, 'error_class': type(error).__name__}))
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
