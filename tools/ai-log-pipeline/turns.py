"""Parse gateway captures into conversation turns and hash items canonically.

A capture is one client request seen by the gateway. A turn is a capture that
succeeded and was recorded completely: (context, input items, output items).
Items are hashed in the shape clients echo them back, so the output of turn k
and the matching input items of turn k+1 become the same tree node.
"""
import hashlib
import json

API_BY_ROUTE = {'/v1/responses': 'responses', '/responses': 'responses', '/v1/messages': 'messages',
                '/v1/chat/completions': 'chat', '/chat/completions': 'chat'}
# Response-only fields that clients drop when echoing output items back.
RESPONSE_ONLY = {'status', 'metadata', 'internal_chat_message_metadata_passthrough'}
# Chat completions return the model's thinking next to the answer; most clients
# echo only role+content. Kept in the stored item, left out of the hash.
CHAT_REASONING = ('reasoning', 'reasoning_content')
# Version of the echo rules behind node ids, written to file headers. 1 (or no
# header field): chat reasoning was hashed. 2: it is ignored.
NODE_HASH = 2
CONTEXT_KEYS = {'responses': ('instructions', 'tools', 'tool_choice', 'text', 'reasoning', 'include', 'parallel_tool_calls'),
                'messages': ('system', 'tools', 'tool_choice', 'thinking', 'output_config'),
                'chat': ('tools', 'tool_choice', 'response_format', 'reasoning_effort')}
GENERATED_TYPES = {'reasoning', 'function_call', 'custom_tool_call', 'web_search_call', 'local_shell_call',
                   'file_search_call', 'computer_call', 'image_generation_call', 'code_interpreter_call', 'mcp_call'}
# Model output whose echo form is shorter than this may repeat by chance
# ("OK"), so it never links two conversations on its own.
LINEAGE_MIN_BYTES = 128
SESSION_HEADERS = ('x-claude-code-session-id', 'session-id', 'thread-id', 'x-session-id', 'session_id', 'x-agent-run-id', 'x-trace-id')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def _strip(value):
    if isinstance(value, dict):
        return {k: _strip(v) for k, v in value.items() if k != 'cache_control' and v is not None and v != [] and v != {}}
    if isinstance(value, list):
        return [_strip(v) for v in value]
    return value


def echo_form(api, item, node_hash=NODE_HASH):
    """Normalize an item to the shape clients send back in later requests."""
    if not isinstance(item, dict):
        return item
    item = _strip(item)
    if api == 'responses':
        item = {k: v for k, v in item.items() if k not in RESPONSE_ONLY}
    elif api == 'chat' and node_hash >= 2:
        item = {k: v for k, v in item.items() if k not in CHAT_REASONING}
    if isinstance(item.get('content'), str) and 'role' in item:
        item['content'] = [{'type': 'text', 'text': item['content']}]
    return item


_ECHO_CACHE = {}


def item_echo(api, item, raw=None):
    """(hash, size) of the echo form. With the item's canonical bytes, repeats are
    cached: every turn resends the whole history, so almost all items were seen before."""
    if raw is None:
        form = canonical(echo_form(api, item))
        return hashlib.sha256(form).digest(), len(form)
    key = hashlib.sha256(api.encode() + raw).digest()
    value = _ECHO_CACHE.get(key)
    if value is None:
        if len(_ECHO_CACHE) > 500000:
            _ECHO_CACHE.clear()
        form = canonical(echo_form(api, item))
        value = _ECHO_CACHE[key] = (hashlib.sha256(form).digest(), len(form))
    return value


def item_hash(api, item, raw=None, node_hash=NODE_HASH):
    if node_hash != NODE_HASH:
        return hashlib.sha256(canonical(echo_form(api, item, node_hash))).digest()
    return item_echo(api, item, raw)[0]


def is_generated(item):
    if not isinstance(item, dict):
        return False
    return item.get('role') == 'assistant' or item.get('type') in GENERATED_TYPES


def node_id(parent, digest):
    """A node id commits to its item and, through the parent id, every ancestor."""
    return hashlib.sha256((parent or b'') + digest).digest()[:16]


def chain(digests):
    parent, path = None, []
    for digest in digests:
        parent = node_id(parent, digest)
        path.append(parent)
    return path


def node_path(api, items, raws=None, node_hash=NODE_HASH):
    return chain(item_hash(api, item, raws[i] if raws else None, node_hash) for i, item in enumerate(items))


def sse(raw):
    for block in raw.split(b'\n\n'):
        event, data = None, []
        for line in block.split(b'\n'):
            if line.startswith(b'event:'):
                event = line[6:].strip().decode('utf-8', 'replace')
            elif line.startswith(b'data:'):
                data.append(line[5:].strip())
        if data:
            yield event, b'\n'.join(data)


def _messages_stream(raw):
    blocks, stop, usage, model, message_id = {}, None, {}, None, None
    for _event, data in sse(raw):
        try:
            j = json.loads(data)
        except ValueError:
            continue
        kind = j.get('type')
        if kind == 'message_start':
            message = j.get('message') or {}
            model, message_id = message.get('model'), message.get('id')
            usage.update(message.get('usage') or {})
        elif kind == 'content_block_start':
            blocks[j['index']] = dict(j['content_block'])
        elif kind == 'content_block_delta':
            block, delta = blocks.setdefault(j['index'], {}), j.get('delta') or {}
            kind = delta.get('type')
            if kind == 'text_delta':
                block['text'] = block.get('text', '') + delta.get('text', '')
            elif kind == 'thinking_delta':
                block['thinking'] = block.get('thinking', '') + delta.get('thinking', '')
            elif kind == 'signature_delta':
                block['signature'] = block.get('signature', '') + delta.get('signature', '')
            elif kind == 'input_json_delta':
                block['_json'] = block.get('_json', '') + delta.get('partial_json', '')
            elif kind == 'citations_delta':
                block.setdefault('citations', []).append(delta.get('citation'))
        elif kind == 'message_delta':
            stop = (j.get('delta') or {}).get('stop_reason') or stop
            usage.update(j.get('usage') or {})
        elif kind == 'message_stop':
            if stop is None:
                return None
            content = []
            for index in sorted(blocks):
                block = blocks[index]
                if '_json' in block:
                    text = block.pop('_json')
                    try:
                        block['input'] = json.loads(text) if text else {}
                    except ValueError:
                        return None
                content.append(block)
            return {'output': [{'role': 'assistant', 'content': content}], 'usage': usage, 'model': model, 'stop': stop, 'response_id': message_id}
        elif kind == 'error':
            return None
    return None


def _chat_stream(raw):
    tools, text, reasoning = {}, [], {}
    finish = usage = model = response_id = None
    done = False
    for _event, data in sse(raw):
        if data == b'[DONE]':
            done = True
            continue
        try:
            j = json.loads(data)
        except ValueError:
            continue
        model, response_id, usage = j.get('model') or model, j.get('id') or response_id, j.get('usage') or usage
        for choice in j.get('choices') or []:
            if choice.get('index', 0) != 0:
                continue
            delta = choice.get('delta') or {}
            if delta.get('content'):
                text.append(delta['content'])
            # vLLM streams `reasoning`, DeepSeek-style servers `reasoning_content`;
            # keep the key the same server's non-stream message would carry.
            for key in CHAT_REASONING:
                if isinstance(delta.get(key), str) and delta[key]:
                    reasoning.setdefault(key, []).append(delta[key])
            for call in delta.get('tool_calls') or []:
                slot = tools.setdefault(call.get('index', 0), {'type': 'function', 'function': {'name': '', 'arguments': ''}})
                if call.get('id'):
                    slot['id'] = call['id']
                function = call.get('function') or {}
                slot['function']['name'] += function.get('name') or ''
                slot['function']['arguments'] += function.get('arguments') or ''
            finish = choice.get('finish_reason') or finish
    if not done or finish is None:
        return None
    message = {'role': 'assistant', 'content': ''.join(text) if text else None}
    for key, parts in reasoning.items():
        message[key] = ''.join(parts)
    if tools:
        message['tool_calls'] = [tools[i] for i in sorted(tools)]
    return {'output': [message], 'usage': usage, 'model': model, 'stop': finish, 'response_id': response_id}


def _responses_final(r):
    if not isinstance(r, dict) or r.get('status') != 'completed':
        return None
    return {'output': r.get('output') or [], 'usage': r.get('usage'), 'model': r.get('model'), 'stop': 'completed', 'response_id': r.get('id')}


def parse_response(api, content_type, raw):
    """Return the final assistant output, or None unless the response completed."""
    if not raw:
        return None
    if 'event-stream' in (content_type or ''):
        if api == 'responses':
            for event, data in sse(raw):
                if event in ('response.completed', 'response.incomplete', 'response.failed') or b'"type":"response.completed"' in data[:64]:
                    try:
                        return _responses_final(json.loads(data).get('response'))
                    except (ValueError, AttributeError):
                        return None
            return None
        return _messages_stream(raw) if api == 'messages' else _chat_stream(raw)
    try:
        j = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(j, dict):
        return None
    if api == 'responses':
        return _responses_final(j)
    if api == 'messages':
        if j.get('type') != 'message' or not j.get('stop_reason'):
            return None
        return {'output': [{'role': 'assistant', 'content': j.get('content') or []}], 'usage': j.get('usage'), 'model': j.get('model'), 'stop': j.get('stop_reason'), 'response_id': j.get('id')}
    choice = (j.get('choices') or [None])[0]
    if not isinstance(choice, dict) or not choice.get('finish_reason'):
        return None
    return {'output': [choice.get('message') or {}], 'usage': j.get('usage'), 'model': j.get('model'), 'stop': choice.get('finish_reason'), 'response_id': j.get('id')}


def session_hint(api, start, body):
    """Client-declared conversation id. Prefix lineage is only the fallback."""
    if start.get('x-claude-code-session-id'):
        return 'cc:' + str(start['x-claude-code-session-id'])
    if api == 'messages' and isinstance(body, dict) and isinstance(body.get('metadata'), dict):
        uid = body['metadata'].get('user_id')
        if isinstance(uid, str) and uid.startswith('{'):
            try:
                uid = json.loads(uid)
            except ValueError:
                uid = None
        if isinstance(uid, dict) and isinstance(uid.get('session_id'), str):
            return 'cc:' + uid['session_id'][:128]
        if isinstance(uid, str) and '_session_' in uid:
            return 'cc:' + uid.rsplit('_session_', 1)[1][:128]
    for header in SESSION_HEADERS[1:]:
        if start.get(header):
            return 'h:' + str(start[header])
    if api == 'responses' and isinstance(body, dict) and isinstance(body.get('prompt_cache_key'), str):
        return 'pck:' + body['prompt_cache_key'][:128]
    return None


def classify(capture):
    """Return (turn, reason, hint). Only complete successful requests yield a turn."""
    start, end = capture.get('start') or {}, capture.get('end') or {}
    if not start:
        return None, 'missing_start', None
    api = API_BY_ROUTE.get(start.get('route') or '')
    if api is None:
        return None, 'route:' + (start.get('route') or 'none'), None
    if start.get('protocol') == 'websocket':
        return None, 'websocket', None
    try:
        body = json.loads(bytes(capture['streams'].get('http.request.body', b'')))
    except ValueError:
        body = None
    hint = session_hint(api, start, body)
    if not end:
        return None, 'no_end', hint  # Expired without capture.end (gateway crash or lost tail).
    if end.get('status') != 200:
        return None, 'http_%s' % end.get('status'), hint
    if end.get('client_canceled') or not end.get('request_body_complete', True) or end.get('response_write_failed'):
        return None, 'aborted', hint
    if end.get('dropped_events') or end.get('wal_dropped_events') or capture.get('sequence_gap'):
        return None, 'capture_gap', hint
    if not isinstance(body, dict):
        return None, 'request_unparsed', hint
    if api == 'responses':
        items = body.get('input')
        if isinstance(items, str):
            items = [{'role': 'user', 'content': items}]
        if body.get('previous_response_id'):
            return None, 'previous_response_id', hint
    else:
        items = body.get('messages')
    if not isinstance(items, list) or not items:
        return None, 'no_items', hint
    result = parse_response(api, end.get('response_content_type'), bytes(capture['streams'].get('http.response.body', b'')))
    if result is None:
        return None, 'response_incomplete', hint
    context = {k: body[k] for k in CONTEXT_KEYS[api] if k in body}
    params = {k: v for k, v in body.items() if k not in CONTEXT_KEYS[api] and k not in ('input', 'messages', 'model', 'stream', 'metadata')}
    return {
        'api': api, 'route': start['route'], 'capture_id': capture['capture_id'], 'source_id': capture['source_id'],
        'user_id': start.get('user_id'), 'api_key_id': start.get('api_key_id'), 'group_id': start.get('group_id'),
        'hint': hint, 'window': start.get('x-codex-window-id'), 'subagent': start.get('x-openai-subagent'),
        't_start': capture['t0'], 't_end': capture['t1'], 'request_model': body.get('model'), 'model': result['model'],
        'stream': bool(body.get('stream')), 'usage': result['usage'], 'stop': result['stop'], 'response_id': result['response_id'],
        'context': context, 'items': items, 'output': [x for x in result['output'] if x is not None], 'params': params,
    }, 'kept', hint
