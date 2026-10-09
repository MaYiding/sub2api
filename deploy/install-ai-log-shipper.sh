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
s=Path(c['spool_dir'])
if not s.is_absolute() or s.resolve()!=s or re.fullmatch(r'/[A-Za-z0-9_./-]+',str(s)) is None:
    raise SystemExit('Spool must be an absolute path without symlinks or special characters')
if not str(s).startswith('/var/lib/') or s==Path('/var/lib'):
    raise SystemExit('Choose a dedicated spool beneath /var/lib')
print(s)
PY
)
install -d -m 0755 /opt/sub2api-ai-log
install -d -m 0700 -o 1000 -g 1000 "$SPOOL"
install -d -m 0700 -o 1000 -g 1000 /etc/sub2api-ai-log
if [[ "$CONFIG" != /etc/sub2api-ai-log/shipper.json ]]; then
  install -m 0600 -o 1000 -g 1000 "$CONFIG" /etc/sub2api-ai-log/shipper.json
else
  chown 1000:1000 "$CONFIG"
  chmod 0600 "$CONFIG"
fi
install -m 0644 "$ROOT/tools/ai-log-pipeline/shipper.py" /opt/sub2api-ai-log/shipper.py
python3 -m venv /opt/sub2api-ai-log/venv
/opt/sub2api-ai-log/venv/bin/python -m pip install --only-binary=:all: 'confluent-kafka==2.12.0'
cat > /etc/systemd/system/sub2api-ai-log-shipper.service <<EOF
[Unit]
Description=Sub2api AI log WAL shipper
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0
[Service]
User=1000
Group=1000
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
systemctl is-active sub2api-ai-log-shipper
echo "Shipper installed. Mount the same host spool into both blue/green app containers."
