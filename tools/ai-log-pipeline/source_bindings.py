"""Immutable topic identity bindings, reloaded only between Kafka transactions."""
import hashlib
import json
from pathlib import Path
import re


def validate_snapshot(value):
    if not isinstance(value, dict) or value.get('version') != 1:
        raise ValueError('invalid binding snapshot')
    mapping = value.get('bindings')
    if not isinstance(mapping, dict) or len(mapping) > 1000:
        raise ValueError('invalid binding count')
    for topic, binding in mapping.items():
        if re.fullmatch(r'ai\.raw\.src-[a-f0-9]{12}\.v1', topic) is None:
            raise ValueError('invalid source topic')
        if not isinstance(binding, dict):
            raise ValueError('invalid source binding')
        if set(binding) - {'source_id', 'tenant_id', 'name', 'region', 'revision'}:
            raise ValueError('unknown binding attribute')
        if binding.get('source_id') != topic.removeprefix('ai.raw.').removesuffix('.v1'):
            raise ValueError('source/topic mismatch')
        if binding.get('tenant_id') != 'default':
            raise ValueError('unknown tenant')
        for key in ('name', 'region'):
            if not isinstance(binding.get(key, ''), str) or len(binding.get(key, '')) > 128:
                raise ValueError('invalid source label')
        if type(binding.get('revision')) is not int or binding['revision'] < 1:
            raise ValueError('invalid binding revision')
    return mapping


def binding_snapshot(config):
    mapping = {}
    for topic, value in config.get('raw_topics', {}).items():
        if isinstance(value, str):
            # Only the explicitly configured pre-registration topic is legacy.
            mapping[topic] = {'tenant_id': value, 'legacy': True}
        else:
            mapping.update(validate_snapshot({'version': 1, 'bindings': {topic: value}}))
    path = config.get('bindings_path')
    if not path:
        return mapping, 'legacy'
    raw = Path(path).read_bytes()
    if len(raw) > 1 << 20:
        raise ValueError('binding snapshot too large')
    mapping.update(validate_snapshot(json.loads(raw)))
    return mapping, hashlib.sha256(raw).hexdigest()
