#!/usr/bin/python3
"""Root-owned local collector; exports counts/health only, never log bodies."""
from datetime import datetime,timezone
import json
from contextlib import closing
import sqlite3
import os
from pathlib import Path
import re
import socket
import subprocess
import time

values=[]
def metric(name,value,**labels):
    tags=','.join(key+'='+json.dumps(str(val)) for key,val in sorted(labels.items()))
    values.append('ai_log_'+name+('{'+tags+'}' if tags else '')+' '+str(value))
def run(args):
    result=subprocess.run(args,capture_output=True,text=True,timeout=40,env=dict(os.environ,KAFKA_HEAP_OPTS='-Xms128m -Xmx256m'))
    if result.returncode:raise RuntimeError('metric command failed')
    return result.stdout

hostname=socket.gethostname()
roles={'ai-kafka-01':['ai-kafka'],'ai-kafka-02':['ai-kafka'],'ai-kafka-03':['ai-kafka'],
       'ai-cleaner-01':['ai-log-cleaner'],'ai-query-01':['clickhouse-server','ai-log-indexer','ai-log-api','ai-log-control','ai-log-provisioner'],'ai-archive-01':['ai-archive','ai-log-archiver']}[hostname]
for unit in roles:
    result=subprocess.run(['systemctl','is-active',unit],capture_output=True,text=True)
    metric('service_up',int(result.returncode==0),unit=unit)
for role in ['cleaner','indexer','archiver']:
    path=Path('/var/lib/ai-log-pipeline/'+role+'-status.json')
    if path.exists():
        try:
            data=json.loads(path.read_text())
            for name,value in data.items():
                if type(value) in (int,float):metric('worker_'+name,value,component=role)
        except Exception:metric('worker_status_readable',0,component=role)
if hostname=='ai-query-01':
    try:
        data=json.loads(Path('/var/lib/ai-log-control/provisioner-status.json').read_text())
        for name,value in data.items():
            if type(value) in (int,float):metric('worker_'+name,value,component='provisioner')
        with closing(sqlite3.connect('file:/var/lib/ai-log-control/registry.sqlite?mode=ro',uri=True,timeout=5)) as db:
            for sid,seen,state in db.execute('SELECT id,heartbeat_at,state FROM sources'):
                metric('source_heartbeat_last_seen_timestamp',seen or 0,source_id=sid)
                metric('source_enabled',int(state not in ('disabled','disabling')),source_id=sid)
            for sid,created in db.execute("SELECT source_id,min(created_at) FROM credentials WHERE state IN ('ready','active') GROUP BY source_id"):
                metric('source_oldest_credential_created_timestamp',created,source_id=sid)
            for sid,created in db.execute("SELECT source_id,min(created_at) FROM jobs WHERE state IN ('pending','running') GROUP BY source_id"):
                metric('source_pending_job_age_seconds',int(time.time())-created,source_id=sid)
        metric('control_registry_readable',1)
    except Exception:metric('control_registry_readable',0)
for path in ['/srv/ai-logs/kafka','/srv/ai-logs/archive','/srv/ai-logs/metadata','/srv/ai-logs/clickhouse-hot','/srv/ai-logs/clickhouse-cold','/srv/ai-logs/state']:
    if Path(path).is_mount():
        info=os.statvfs(path)
        metric('disk_available_bytes',info.f_bavail*info.f_frsize,mount=path)
        metric('disk_size_bytes',info.f_blocks*info.f_frsize,mount=path)
certificates=[Path('/etc/kafka/tls/public-cert.pem'),Path('/etc/kafka/tls/internal-cert.pem'),Path('/etc/ai-logs/seaweed/archive.crt')]
for role in ['cleaner','indexer','archiver']:certificates.append(Path('/etc/ai-logs/pipeline/'+role+'.crt'))
for path in certificates:
    if path.exists():
        try:
            text=run(['openssl','x509','-in',str(path),'-noout','-enddate']).strip().split('=',1)[1]
            expires=datetime.strptime(text,'%b %d %H:%M:%S %Y %Z').replace(tzinfo=timezone.utc).timestamp()
            metric('certificate_expiry_timestamp',int(expires),certificate=path.name)
        except Exception:metric('certificate_readable',0,certificate=path.name)
if hostname=='ai-kafka-01':
    try:
        args=['--bootstrap-server','10.10.20.230:9092','--command-config','/etc/kafka/admin.properties']
        description=run(['/opt/kafka/bin/kafka-topics.sh']+args+['--describe','--topic','ai\..*'])
        counts=[len(value.split(',')) for value in re.findall(r'Isr:\s*([0-9,]+)',description)]
        assert len(counts)>=18
        for topic,cap in re.findall(r'^Topic:\s*(\S+).*?Configs:.*?retention\.bytes=(\d+)',description,re.M):
            metric('kafka_topic_byte_cap',int(cap),topic=topic)
        metric('kafka_under_replicated_partitions',sum(count<3 for count in counts))
        metric('kafka_partition_count',len(counts))
        groups=run(['/opt/kafka/bin/kafka-consumer-groups.sh']+args+['--all-groups','--describe'])
        for line in groups.splitlines():
            fields=line.split()
            if len(fields)>=6 and fields[0].startswith('ai-') and fields[2].isdigit():
                labels={'group':fields[0],'topic':fields[1],'partition':fields[2]}
                if fields[5].isdigit():metric('kafka_consumer_lag',int(fields[5]),**labels)
                if fields[3].isdigit():metric('kafka_committed_offset',int(fields[3]),**labels)
        metric('kafka_collector_success',1)
    except Exception:metric('kafka_collector_success',0)
if hostname.startswith('ai-kafka-'):
    check=Path('/etc/kafka/tls/certificate-checked-at')
    if check.exists():metric('certificate_distribution_last_success_timestamp',int(check.read_text()))
    root=Path('/srv/ai-logs/kafka/data')
    for directory in root.glob('ai.*-*'):
        if not directory.is_dir():continue
        topic,partition=directory.name.rsplit('-',1)
        if not partition.isdigit():continue
        size=sum(path.stat().st_size for path in directory.iterdir() if path.is_file() and path.suffix=='.log')
        metric('kafka_partition_bytes',size,topic=topic,partition=partition)
metric('collector_last_success_timestamp',int(time.time()))
target=Path('/var/lib/prometheus/node-exporter/ai-logs.prom')
temp=target.with_suffix('.tmp');temp.write_text('\n'.join(values)+'\n');temp.chmod(0o644);temp.replace(target)
