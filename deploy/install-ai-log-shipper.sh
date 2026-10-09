#!/usr/bin/env bash
# Run on the relay host, never in its active app container.
set -euo pipefail
if [[ ${EUID} -ne 0 || $# -ne 1 ]]; then
  echo "Usage: sudo $0 /absolute/private/shipper.json" >&2
  exit 2
fi
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
CONFIG=$1
command -v python3 >/dev/null
command -v systemctl >/dev/null
SPOOL=$(python3 - "$CONFIG" <<'PY'
import json,re,sys
from pathlib import Path
p=Path(sys.argv[1])
if not p.is_absolute() or p.is_symlink() or p.stat().st_mode & 0o077:
    raise SystemExit('Configuration must be an absolute, private mode-0600 file')
c=json.loads(p.read_text())
for key in ('bootstrap_servers','username','password','source_id','topic','spool_dir'):
    if not isinstance(c.get(key),str) or not c[key]:raise SystemExit('Missing shipper configuration field: '+key)
current=Path('/etc/sub2api-ai-log/shipper.json')
if current.exists():
    old=json.loads(current.read_text())
    if any(old.get(k)!=c.get(k) for k in ('source_id','topic','spool_dir')):
        raise SystemExit('Source/topic/spool cannot change in place; preserve and drain the existing WAL first')
if re.fullmatch(r'[A-Za-z0-9_.-]{1,128}',c['source_id']) is None:
    raise SystemExit('Invalid source identity')
s=Path(c['spool_dir'])
if not s.is_absolute() or s.resolve()!=s or re.fullmatch(r'/[A-Za-z0-9_./-]+',str(s)) is None:
    raise SystemExit('Spool must be an absolute path without symlinks or special characters')
if not str(s).startswith('/var/lib/') or s==Path('/var/lib'):
    raise SystemExit('Choose a dedicated spool beneath /var/lib')
print(s)
PY
)
# The application container writes as UID 1000. The host's matching account
# may have a different primary GID (for example a cloud image's admin group).
if ! getent passwd 1000 >/dev/null; then
  if getent passwd sub2api-ai-log >/dev/null; then
    echo "sub2api-ai-log already exists with another UID; resolve the UID 1000 mapping first" >&2
    exit 2
  fi
  useradd --uid 1000 --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin sub2api-ai-log
fi
SHIPPER_GID=$(getent passwd 1000 | cut -d: -f4)
getent group "$SHIPPER_GID" >/dev/null
install -d -m 0755 /opt/sub2api-ai-log
install -d -m 0700 -o 1000 -g "$SHIPPER_GID" "$SPOOL"
install -d -m 0700 -o 1000 -g "$SHIPPER_GID" /etc/sub2api-ai-log
# Prepare runtime before replacing the live configuration. The running process
# retains its loaded credentials and WAL ownership until systemd stops it.
install -m 0644 "$ROOT/tools/ai-log-pipeline/shipper.py" /opt/sub2api-ai-log/shipper.py
install -m 0644 "$ROOT/tools/ai-log-pipeline/shipper_control.py" /opt/sub2api-ai-log/shipper_control.py
python3 -m venv /opt/sub2api-ai-log/venv
/opt/sub2api-ai-log/venv/bin/python -m pip install --only-binary=:all: 'confluent-kafka==2.12.0'
python3 - "$CONFIG" "$SHIPPER_GID" <<'PYCONFIG'
import json,os,sys,tempfile
from pathlib import Path
sys.path.insert(0,'/opt/sub2api-ai-log')
from shipper_control import checksum
source=Path(sys.argv[1]); target=Path('/etc/sub2api-ai-log/shipper.json')
raw=source.read_bytes(); config=json.loads(raw)
if config.get('control') and checksum(config)!=config.get('config_digest'):
    raise SystemExit('Configuration digest mismatch')
if target.exists() and source!=target:
    backup=target.with_suffix('.previous.json')
    fd=os.open(backup,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as out:
        out.write(target.read_bytes());out.flush();os.fsync(out.fileno())
    os.chown(backup,1000,int(sys.argv[2]))
fd,temp=tempfile.mkstemp(dir=target.parent)
os.fchmod(fd,0o600);os.fchown(fd,1000,int(sys.argv[2]))
with os.fdopen(fd,'wb') as out:
    out.write(raw);out.flush();os.fsync(out.fileno())
os.replace(temp,target)
fd=os.open(target.parent,os.O_RDONLY|os.O_DIRECTORY)
os.fsync(fd);os.close(fd)
PYCONFIG
cat > /etc/systemd/system/sub2api-ai-log-shipper.service <<EOF
[Unit]
Description=Sub2api AI log WAL shipper
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0
[Service]
User=1000
Group=$SHIPPER_GID
ExecStart=/opt/sub2api-ai-log/venv/bin/python /opt/sub2api-ai-log/shipper.py --config /etc/sub2api-ai-log/shipper.json
Restart=on-failure
RestartSec=5
TimeoutStopSec=180
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=$SPOOL
MemoryMax=512M
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable sub2api-ai-log-shipper
systemctl restart sub2api-ai-log-shipper
sleep 3
if ! systemctl is-active --quiet sub2api-ai-log-shipper; then
  echo "Shipper failed to start; WAL remains intact. Inspect sanitized journal error classes." >&2
  if [[ -f /etc/sub2api-ai-log/shipper.previous.json ]]; then
    mv -f /etc/sub2api-ai-log/shipper.previous.json /etc/sub2api-ai-log/shipper.json
    systemctl restart sub2api-ai-log-shipper
    echo "Restored previous configuration." >&2
  fi
  exit 1
fi
systemctl is-active sub2api-ai-log-shipper
echo "Shipper installed. Mount the same host spool into both blue/green app containers."

python3 - /etc/sub2api-ai-log/shipper.json <<'PYSUMMARY'
import json,sys
c=json.load(open(sys.argv[1]))
print('AI_LOG_SOURCE_ID='+c['source_id'])
print('SUB2API_AI_LOG_SPOOL_HOST_DIR='+c['spool_dir'])
print('Enable AI_LOG_ENABLED=true when deploying the app with the shared WAL mount.')
PYSUMMARY
