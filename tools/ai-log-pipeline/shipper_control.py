"""Optional source heartbeat and reproducible end-to-end probe (stdlib only)."""
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import socket
import ssl
import threading
import time
import urllib.parse
import urllib.request
import uuid


def checksum(config):
    value = {k: config[k] for k in ('source_id', 'topic', 'username', 'config_version', 'credential_id', 'probe')}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def atomic_file(path, data):
    temp = path.with_name(path.name + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())
    temp.replace(path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def prepare_probe(config, spool):
    if not config.get('control'):
        return None
    if checksum(config) != config.get('config_digest'):
        raise ValueError('configuration digest mismatch')
    identity = spool / '.instance-id'
    if not identity.exists():
        atomic_file(identity, str(uuid.uuid4()).encode())
    instance = str(uuid.UUID(identity.read_text().strip()))
    probe = config['probe']
    # One stable probe per credential. A retry duplicates its immutable IDs.
    marker = spool / ('.probe-' + str(uuid.UUID(probe['event_id'])))
    if not marker.exists():
        event = {'schema_version': 1, 'event_id': probe['event_id'],
                 'capture_id': probe['capture_id'], 'trace_id': probe['trace_id'],
                 'source_id': config['source_id'], 'sequence': 0,
                 'timestamp': datetime.fromtimestamp(probe['timestamp'], timezone.utc).isoformat(),
                 'kind': 'source.probe', 'data_base64': '',
                 'metadata': {'credential_id': config['credential_id'], 'nonce': probe['nonce'],
                              'config_version': config['config_version']}}
        atomic_file(spool / ('probe-' + probe['event_id'] + '.ready'), (json.dumps(event)+'\n').encode())
        atomic_file(marker, b'prepared\n')
    return instance


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Heartbeat:
    def __init__(self, config, spool, instance):
        self.config, self.spool, self.instance = config, spool, instance
        endpoint = config['control']['endpoint'].rstrip('/')
        url = urllib.parse.urlsplit(endpoint)
        if url.scheme != 'https' or not url.hostname or url.username or url.password or url.path or url.query or url.fragment:
            raise ValueError('control endpoint must be an HTTPS origin')
        self.url = endpoint + '/agent/v1/heartbeat'
        context = ssl.create_default_context(cafile=config['control'].get('ca_file'))
        self.opener = urllib.request.build_opener(NoRedirect, urllib.request.HTTPSHandler(context=context))
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.values = {'delivered_events': 0, 'failures': 0, 'last_delivery_at': 0}
        self.thread = threading.Thread(target=self.run, name='source-heartbeat', daemon=True)

    def update(self, **values):
        with self.lock:
            self.values.update(values)

    def payload(self):
        pending, oldest, now = 0, time.time(), time.time()
        for entry in self.spool.iterdir():
            if entry.suffix not in ('.ready', '.open') or entry.is_symlink():
                continue
            try:
                info = entry.stat()
                pending += info.st_size
                oldest = min(oldest, info.st_mtime)
            except FileNotFoundError:
                continue
        with self.lock:
            value = dict(self.values)
        value.update(source_id=self.config['source_id'], credential_id=self.config['credential_id'],
                     config_digest=self.config['config_digest'], instance_id=self.instance,
                     hostname=socket.gethostname()[:128], shipper_version='2',
                     pending_bytes=pending, oldest_age_seconds=max(0, int(now-oldest)))
        return value

    def send(self):
        request = urllib.request.Request(self.url, data=json.dumps(self.payload()).encode(),
                  headers={'Content-Type': 'application/json', 'Authorization': 'Bearer '+self.config['control']['source_token']}, method='POST')
        with self.opener.open(request, timeout=10) as response:
            if response.status != 200:
                raise RuntimeError('heartbeat rejected')
            response.read(4096)

    def run(self):
        while not self.stop.is_set():
            try:
                self.send()
            except Exception as error:
                logging.getLogger('ai-log-shipper').warning('heartbeat_failed class=%s', type(error).__name__)
            self.stop.wait(30)

    def close(self):
        self.stop.set()
        self.thread.join(timeout=11)
