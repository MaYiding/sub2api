import json
import logging
import os
from pathlib import Path
import re

LOG = logging.getLogger('ai-log-pipeline')


def load_config(path):
    config_path = Path(path)
    if config_path.stat().st_mode & 0o007:
        raise ValueError('configuration must not be world readable')
    return json.loads(config_path.read_text())


def kafka_config(config):
    return {'bootstrap.servers': config['brokers'], 'security.protocol': 'SSL', 'ssl.ca.location': config['tls']['ca'], 'ssl.certificate.location': config['tls']['cert'], 'ssl.key.location': config['tls']['key'], 'ssl.endpoint.identification.algorithm': 'https', 'enable.ssl.certificate.verification': True, 'client.id': config['instance']}


def consumer(config, group, topics):
    from confluent_kafka import Consumer
    client = Consumer(dict(kafka_config(config), **{'group.id': group, 'enable.auto.commit': False, 'enable.auto.offset.store': False, 'auto.offset.reset': 'earliest', 'isolation.level': 'read_committed', 'max.poll.interval.ms': 900000}))
    client.subscribe(topics)
    return client


def producer(config, transactional=False):
    from confluent_kafka import Producer
    settings = dict(kafka_config(config), **{'enable.idempotence': True, 'acks': 'all', 'compression.type': 'zstd', 'linger.ms': 20, 'message.max.bytes': 2097152})
    if transactional:
        settings['transactional.id'] = config['instance']
    return Producer(settings)


def offsets(messages):
    from confluent_kafka import TopicPartition
    positions = {}
    for message in messages:
        pair = (message.topic(), message.partition())
        positions[pair] = max(positions.get(pair, 0), message.offset()+1)
    return [TopicPartition(topic, partition, offset) for (topic, partition), offset in positions.items()]


def s3_client(config):
    import boto3
    from botocore.config import Config
    s3 = config['s3']
    return boto3.client('s3', endpoint_url=s3['endpoint'], aws_access_key_id=s3['access_key'], aws_secret_access_key=s3['secret_key'], region_name='us-east-1', verify=s3['ca'], config=Config(signature_version='s3v4', s3={'addressing_style': 'path'}, retries={'max_attempts': 5, 'mode': 'standard'}, connect_timeout=10, read_timeout=60, request_checksum_calculation='when_required', response_checksum_validation='when_required'))


def status(config, counters):
    path = Path(config['status_path'])
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(counters))
    os.chmod(temp, 0o640)
    temp.replace(path)


def ch_request(config, sql, data=None, parameters=None):
    import requests
    ch = config['clickhouse']
    args = {'query': sql, 'date_time_input_format': 'best_effort'}
    args.update({'param_'+key: value for key, value in (parameters or {}).items()})
    response = requests.post(ch['url'], params=args, data=data, auth=(ch['user'], ch['password']), timeout=(5, 60))
    if response.status_code != 200:
        # Response bodies can contain SQL values/log metadata.
        raise RuntimeError('ClickHouse request failed; status='+str(response.status_code))
    return response


def ch_insert(config, table, rows):
    if table not in {'events', 'archive_catalog', 'archive_segments'}:
        raise ValueError('unknown table')
    if rows:
        database = config['clickhouse'].get('database', 'ai_logs')
        if re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', database) is None:
            raise ValueError('invalid database identifier')
        data = b'\n'.join(json.dumps(row, separators=(',', ':')).encode() for row in rows)
        ch_request(config, 'INSERT INTO `'+database+'`.'+table+' FORMAT JSONEachRow', data)
