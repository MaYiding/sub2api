import json
from pathlib import Path
import tempfile
import unittest
import uuid

import zstandard

import session_tool
from sessionizer import Captures
from sessions import Store
from turns import canonical, classify, echo_form, item_hash, node_id, parse_response

SIG = 'S' * 200  # Thinking signatures/encrypted reasoning make model output unique.
# A deterministic classifier answers identical input with byte-identical output.
VERDICT = json.dumps({'model': 'pair-classifier', 'results': [{'label': 'SAME', 'score': 1.375}, {'label': 'DIFF', 'score': -2.5703}],
                      'usage': {'pairs': 2}, 'version': 'synthetic'})


def capture(route, body, response, *, status=200, hint=None, user=1, t0='2026-10-19T00:00:00Z', t1=None, ctype='application/json', **end):
    start = {'route': route, 'user_id': user, 'api_key_id': 7, 'protocol': 'http'}
    if hint:
        start['session-id'] = hint
    finish = {'status': status, 'response_content_type': ctype, 'request_body_complete': True, 'client_canceled': False, 'dropped_events': 0}
    finish.update(end)
    raw = response if isinstance(response, bytes) else json.dumps(response).encode()
    return {'capture_id': str(uuid.uuid4()), 'source_id': 'src-a', 'start': start, 'end': finish, 'sequence_gap': False, 't0': t0, 't1': t1 or t0, 'bytes': 1,
            'streams': {'http.request.body': bytearray(json.dumps(body).encode()), 'http.response.body': bytearray(raw)}}


def anthropic(messages, reply, **kw):
    out = {'type': 'message', 'id': 'msg_x', 'model': 'm', 'stop_reason': 'end_turn', 'content': reply, 'usage': {'input_tokens': 1}}
    return capture('/v1/messages', {'model': 'm', 'system': 'sys', 'messages': messages}, out, **kw)


def assistant(text):
    return [{'type': 'thinking', 'thinking': 't', 'signature': SIG}, {'type': 'text', 'text': text}]


def chat(messages, message, *, stream=False, trace=None, model='m', **kw):
    """An OpenAI-compatible chat capture; `message` is what the server returned."""
    body = {'model': model, 'messages': messages, 'stream': stream}
    if stream:
        deltas = [{'role': 'assistant'}] + [{k: v} for k, v in message.items() if k != 'role' and v is not None]
        chunks = [{'id': 'c1', 'model': model, 'choices': [{'index': 0, 'delta': d}]} for d in deltas]
        chunks[-1]['choices'][0]['finish_reason'] = 'stop'
        response = b''.join(b'data: ' + json.dumps(c).encode() + b'\n\n' for c in chunks) + b'data: [DONE]\n\n'
        cap = capture('/v1/chat/completions', body, response, ctype='text/event-stream', **kw)
    else:
        response = {'id': 'c1', 'model': model, 'choices': [{'index': 0, 'message': message, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 1}}
        cap = capture('/v1/chat/completions', body, response, **kw)
    if trace:
        cap['start']['x-trace-id'] = trace
    return cap


def vllm_message(content, reasoning):
    # vLLM's non-stream message: reasoning next to the answer, unused fields null.
    return {'role': 'assistant', 'content': content, 'reasoning': reasoning, 'refusal': None, 'annotations': None, 'audio': None, 'function_call': None}


def lines(root, relative):
    with open(Path(root) / relative, 'rb') as handle:
        data = zstandard.ZstdDecompressor().stream_reader(handle).read()
    records = [json.loads(line) for line in data.splitlines()]
    # Every line, including spliced node/context lines, must be canonical JSON.
    assert [canonical(r) for r in records] == data.splitlines()
    return records


class TurnTests(unittest.TestCase):
    def test_output_echo_matches_next_input(self):
        output = {'type': 'reasoning', 'id': 'rs_1', 'summary': [], 'content': [], 'encrypted_content': 'x', 'status': 'completed', 'metadata': {'turn_id': 'a'}}
        echoed = {'type': 'reasoning', 'id': 'rs_1', 'summary': [], 'encrypted_content': 'x'}
        self.assertEqual(item_hash('responses', output), item_hash('responses', echoed))
        self.assertEqual(echo_form('messages', {'role': 'user', 'content': 'hi'}),
                         echo_form('messages', {'role': 'user', 'content': [{'type': 'text', 'text': 'hi', 'cache_control': {'type': 'ephemeral'}}]}))

    def test_chat_reasoning_is_stored_but_not_hashed(self):
        echoed = {'role': 'assistant', 'content': 'answer'}
        for key in ('reasoning', 'reasoning_content'):
            returned = dict(echoed, **{key: 'thinking'})
            self.assertEqual(item_hash('chat', returned), item_hash('chat', echoed))
            self.assertNotEqual(item_hash('chat', returned, node_hash=1), item_hash('chat', echoed, node_hash=1))
        # Other APIs and reasoning-free chat items hash as before.
        self.assertEqual(item_hash('chat', echoed), item_hash('chat', echoed, node_hash=1))
        item = {'type': 'reasoning', 'id': 'rs_1', 'encrypted_content': 'x'}
        self.assertEqual(item_hash('responses', item), item_hash('responses', item, node_hash=1))
        turn, _, _ = classify(chat([{'role': 'user', 'content': 'q'}], vllm_message('a', 'thinking')))
        self.assertEqual(turn['output'][0]['reasoning'], 'thinking')

    def test_chat_stream_keeps_reasoning_under_the_server_key(self):
        for key in ('reasoning', 'reasoning_content'):
            cap = chat([{'role': 'user', 'content': 'q'}], {'role': 'assistant', key: 'step one', 'content': 'answer'}, stream=True)
            turn, reason, _ = classify(cap)
            self.assertEqual(reason, 'kept')
            self.assertEqual(turn['output'], [{'role': 'assistant', 'content': 'answer', key: 'step one'}])

    def test_stream_parsers_require_terminal_events(self):
        sse = (b'event: message_start\ndata: {"type":"message_start","message":{"id":"m1","model":"x","usage":{}}}\n\n'
               b'data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"t1","name":"Bash","input":{}}}\n\n'
               b'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\\"a\\":"}}\n\n'
               b'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"1}"}}\n\n'
               b'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":3}}\n\n')
        self.assertIsNone(parse_response('messages', 'text/event-stream', sse))
        result = parse_response('messages', 'text/event-stream', sse + b'data: {"type":"message_stop"}\n\n')
        self.assertEqual(result['output'][0]['content'][0]['input'], {'a': 1})
        chat = b'data: {"id":"c","choices":[{"index":0,"delta":{"content":"he"}}]}\n\ndata: {"choices":[{"index":0,"delta":{"content":"y"},"finish_reason":"stop"}]}\n\n'
        self.assertIsNone(parse_response('chat', 'text/event-stream', chat))
        self.assertEqual(parse_response('chat', 'text/event-stream', chat + b'data: [DONE]\n\n')['output'][0]['content'], 'hey')
        responses = b'event: response.completed\ndata: {"type":"response.completed","response":{"id":"r","status":"completed","output":[{"type":"message","role":"assistant","content":[]}]}}\n\n'
        self.assertEqual(parse_response('responses', 'text/event-stream', responses)['response_id'], 'r')

    def test_incomplete_requests_are_not_turns(self):
        msgs = [{'role': 'user', 'content': 'q'}]
        self.assertEqual(classify(anthropic(msgs, assistant('a'), status=502))[1], 'http_502')
        self.assertEqual(classify(anthropic(msgs, assistant('a'), client_canceled=True))[1], 'aborted')
        partial = capture('/v1/messages', {'messages': msgs}, b'event: message_start\ndata: {}\n\n', ctype='text/event-stream')
        self.assertEqual(classify(partial)[1], 'response_incomplete')
        expired = anthropic(msgs, assistant('a'))
        expired['end'] = None
        self.assertEqual(classify(expired)[1], 'no_end')
        self.assertEqual(classify(anthropic(msgs, assistant('a'), wal_dropped_events=2))[1], 'capture_gap')


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'out'
        self.store = Store(str(Path(self.tmp.name) / 'state.sqlite'), self.root, idle_flush=60, reopen_window=3600)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def add(self, cap):
        turn, reason, hint = classify(cap)
        if turn is None:
            self.store.record_drop(cap, reason, hint, 1)
            return None
        return self.store.ingest(turn, 1)

    def test_prefix_turns_collapse_and_failed_requests_are_counted(self):
        u1, u2 = {'role': 'user', 'content': 'q1'}, {'role': 'user', 'content': 'q2'}
        s1 = self.add(anthropic([u1], assistant('a1'), hint='S'))
        self.add(anthropic([u1, {'role': 'assistant', 'content': assistant('a1')}, u2], assistant('a2'), status=502, hint='S'))
        s2 = self.add(anthropic([u1, {'role': 'assistant', 'content': assistant('a1')}, u2], assistant('a2'), hint='S', t0='2026-10-19T00:01:00Z'))
        self.assertEqual(s1, s2)
        written, _ = self.store.flush(10**10)
        self.assertEqual(written, 1)
        path = self.store.db.execute('SELECT path FROM sessions').fetchone()[0]
        records = lines(self.root, path)
        header = records[0]
        self.assertEqual((header['turns'], header['nodes'], len(header['leaves'])), (2, 4, 1))
        self.assertEqual(header['dropped_requests'], {'http_502': 1})
        nodes = [r for r in records if r['type'] == 'node']
        self.assertEqual([n['origin'] for n in nodes], ['input', 'output', 'input', 'output'])
        self.assertEqual(nodes[1]['parent'], nodes[0]['id'])

    def test_fork_with_new_client_id_joins_by_lineage(self):
        u1 = {'role': 'user', 'content': 'q1'}
        a1 = {'role': 'assistant', 'content': assistant('a1')}
        base = self.add(anthropic([u1], assistant('a1'), hint='A'))
        fork = self.add(anthropic([u1, a1, {'role': 'user', 'content': 'other'}], assistant('b'), hint='B'))
        self.assertEqual(base, fork)
        self.store.flush(10**10)
        header = lines(self.root, self.store.relative_path(base))[0]
        self.assertEqual(header['branch_points'], 0)
        self.add(anthropic([u1, a1, {'role': 'user', 'content': 'third'}], assistant('c'), hint='A', t0='2026-10-19T00:02:00Z'))
        self.store.flush(10**10)
        header = lines(self.root, self.store.relative_path(base))[0]
        self.assertEqual((header['branch_points'], header['version'], sorted(header['client_session_ids'])), (1, 2, ['h:A', 'h:B']))

    def test_shared_prompt_without_shared_output_stays_separate(self):
        u1 = {'role': 'user', 'content': 'same CLAUDE.md context'}
        first = self.add(anthropic([u1], assistant('x')))
        second = self.add(anthropic([u1], assistant('y')))
        self.assertNotEqual(first, second)
        short = {'role': 'assistant', 'content': [{'type': 'text', 'text': 'OK'}]}
        third = self.add(anthropic([u1, short, {'role': 'user', 'content': 'go'}], assistant('z')))
        self.assertNotIn(third, (first, second))

    def test_out_of_order_turns_build_same_tree(self):
        u1, u2 = {'role': 'user', 'content': 'q1'}, {'role': 'user', 'content': 'q2'}
        a1 = {'role': 'assistant', 'content': assistant('a1')}
        late = self.add(anthropic([u1, a1, u2], assistant('a2'), hint='B'))
        early = self.add(anthropic([u1], assistant('a1'), hint='A'))
        self.assertEqual(late, early)
        self.store.flush(10**10)
        header = lines(self.root, self.store.relative_path(late))[0]
        self.assertEqual((header['turns'], header['nodes'], header['roots']), (2, 4, 1))
        origins = self.store.db.execute('SELECT origin, turn IS NOT NULL FROM nodes WHERE session_id=? ORDER BY seq', (late,)).fetchall()
        self.assertEqual(origins[1], ('output', 1))

    def test_lineage_merges_two_client_sessions(self):
        u1 = {'role': 'user', 'content': 'q1'}
        a1 = {'role': 'assistant', 'content': assistant('a1')}
        first = self.add(anthropic([u1], assistant('a1'), hint='A'))
        other = self.add(anthropic([{'role': 'user', 'content': 'unrelated'}], assistant('b1'), hint='B'))
        self.store.flush(10**10)
        self.assertTrue((self.root / self.store.relative_path(other)).exists())
        joined = self.add(anthropic([u1, a1, {'role': 'user', 'content': 'q2'}], assistant('a2'), hint='B'))
        self.assertEqual(joined, first)
        self.store.flush(10**10)
        self.assertFalse((self.root / self.store.relative_path(other)).exists())
        header = lines(self.root, self.store.relative_path(first))[0]
        self.assertEqual((header['turns'], header['roots'], header['merged_from'], header['client_session_ids']), (3, 2, [other], ['h:A', 'h:B']))

    def test_identical_outputs_with_different_hints_stay_separate(self):
        pair = [{'role': 'user', 'content': '{"a": "event one", "b": "event two"}'}]
        verdict = {'role': 'assistant', 'content': VERDICT}
        first = self.add(chat(pair, verdict, trace='T1'))
        second = self.add(chat(pair, verdict, trace='T2', stream=True))
        third = self.add(chat(pair, verdict))
        self.assertEqual(len({first, second, third}), 3)
        self.assertEqual(self.add(chat(pair, verdict, trace='T1')), first)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM sessions WHERE state='merged'").fetchone()[0], 0)
        self.store.flush(10**10)
        header = lines(self.root, self.store.relative_path(first))[0]
        self.assertEqual((header['turns'], header['nodes'], len(header['leaves']), header['client_session_ids']), (2, 2, 1, ['h:T1']))

    def test_repeated_output_does_not_join_a_continued_conversation(self):
        u1 = {'role': 'user', 'content': 'q1'}
        a1 = {'role': 'assistant', 'content': VERDICT}
        base = self.add(chat([u1], a1, trace='X'))
        self.assertEqual(self.add(chat([u1, a1, {'role': 'user', 'content': 'q2'}], {'role': 'assistant', 'content': VERDICT + ' again'}, trace='X')), base)
        # Same input, same output, but nobody echoed it in this conversation.
        other = self.add(chat([u1], a1, trace='Y'))
        self.assertNotEqual(other, base)
        # Echoing it is a continuation again, of the session that produced it last.
        self.assertEqual(self.add(chat([u1, a1, {'role': 'user', 'content': 'q3'}], {'role': 'assistant', 'content': 'b' * 200})), other)

    def test_chat_reasoning_echo_stays_linear(self):
        u1, u2 = {'role': 'user', 'content': 'q1'}, {'role': 'user', 'content': 'q2'}
        answer = 'first answer ' * 12
        first = self.add(chat([u1], vllm_message(answer, 'why ' * 10), hint='C'))
        # The client echoes role+content only; the second reply streams `delta.reasoning`.
        second = self.add(chat([u1, {'role': 'assistant', 'content': answer}, u2], {'role': 'assistant', 'reasoning': 'more', 'content': 'second answer'},
                               stream=True, hint='C', t0='2026-10-19T00:01:00Z'))
        self.assertEqual(first, second)
        self.store.flush(10**10)
        path = self.root / self.store.relative_path(first)
        records = lines(self.root, self.store.relative_path(first))
        header = records[0]
        self.assertEqual((header['node_hash'], header['turns'], header['nodes'], len(header['leaves']), header['branch_points']), (2, 2, 4, 1, 0))
        nodes = [r for r in records if r['type'] == 'node']
        self.assertEqual([n['origin'] for n in nodes], ['input', 'output', 'input', 'output'])
        self.assertEqual((nodes[1]['item']['reasoning'], nodes[3]['item']['reasoning']), ('why ' * 10, 'more'))
        self.assertEqual(session_tool.check(path)[1], 0)

    def test_short_answer_with_long_reasoning_never_links(self):
        u1 = {'role': 'user', 'content': 'hello'}
        a = self.add(chat([u1], vllm_message('OK', 'a' * 300), trace='A'))
        b = self.add(chat([u1], vllm_message('OK', 'b' * 300), trace='B'))
        # Reasoning is not hashed, so even echoed back it cannot make "OK" distinctive enough to link on.
        echoed = {'role': 'assistant', 'content': 'OK', 'reasoning': 'a' * 300}
        later = self.add(chat([u1, echoed, {'role': 'user', 'content': 'go on'}], {'role': 'assistant', 'content': 'c' * 200}, trace='A'))
        self.assertEqual((later, len({a, b})), (a, 2))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM sessions WHERE state='merged'").fetchone()[0], 0)

    def test_export_lists_every_turn_of_identical_requests(self):
        pair = [{'role': 'user', 'content': 'pair'}]
        verdict = {'role': 'assistant', 'content': VERDICT}
        caps = [chat(pair, verdict, hint='J'), chat(pair, verdict, hint='J', t0='2026-10-19T00:01:00Z')]
        session = {self.add(c) for c in caps}
        self.assertEqual(len(session), 1)
        self.store.flush(10**10)
        path = self.root / self.store.relative_path(session.pop())
        ids = [c['capture_id'] for c in caps]
        per_turn = list(session_tool.export(path, every_turn=True))
        self.assertEqual([r['capture_id'] for r in per_turn], ids)
        self.assertEqual(per_turn[0]['items'], per_turn[1]['items'])
        per_leaf = list(session_tool.export(path))
        self.assertEqual([(r['capture_id'], r['capture_ids']) for r in per_leaf], [(ids[0], ids)])

    def test_verify_accepts_ids_written_before_node_hash_2(self):
        self.add(chat([{'role': 'user', 'content': 'q'}], vllm_message('answer', 'thinking'), hint='L'))
        self.store.flush(10**10)
        path = self.root / self.store.db.execute('SELECT path FROM sessions').fetchone()[0]
        with open(path, 'rb') as handle:
            data = zstandard.ZstdDecompressor().stream_reader(handle).read()
        records = [json.loads(line) for line in data.splitlines()]
        root, output = [r for r in records if r['type'] == 'node']
        legacy = node_id(bytes.fromhex(root['id']), item_hash('chat', output['item'], node_hash=1)).hex()
        old = Path(self.tmp.name) / 'old.jsonl.zst'
        for name, text in (('old', data.replace(output['id'].encode(), legacy.encode()).replace(b'"node_hash":2,', b'')),
                           ('tampered', data.replace(b'"content":"answer"', b'"content":"other"'))):
            old.write_bytes(zstandard.ZstdCompressor().compress(text))
            if name == 'old':
                self.assertEqual(session_tool.check(old)[1], 1)
            else:
                with self.assertRaisesRegex(ValueError, 'does not match'):
                    session_tool.check(old)

    def test_sealed_session_starts_new_file_with_resumed_from(self):
        u1 = {'role': 'user', 'content': 'q1'}
        a1 = {'role': 'assistant', 'content': assistant('a1')}
        old = self.add(anthropic([u1], assistant('a1'), hint='A'))
        self.store.flush(10**10)
        self.assertEqual(self.store.seal(10**10), 1)
        new = self.add(anthropic([u1, a1, {'role': 'user', 'content': 'later'}], assistant('a2'), hint='A'))
        self.assertNotEqual(old, new)
        self.assertEqual(self.store.db.execute('SELECT resumed_from FROM sessions WHERE id=?', (new,)).fetchone()[0], old)
        self.store.gc(10**10)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM blobs').fetchone()[0], 5)


class CaptureTests(unittest.TestCase):
    def test_watermark_holds_open_captures(self):
        captures = Captures(timeout=60)
        def event(cid, seq, kind, offset):
            return captures.add('t', 0, offset, {'capture_id': cid, 'event_id': str(uuid.uuid4()), 'source_id': 's', 'sequence': seq, 'kind': kind,
                                                 'timestamp': '2026-10-19T00:00:%02dZ' % offset, 'data': b'', 'metadata': {}})
        event('a', 0, 'capture.start', 10)
        event('b', 0, 'capture.start', 11)
        self.assertIsNotNone(event('b', 1, 'capture.end', 12))
        self.assertEqual(captures.watermarks(), {('t', 0): 10})
        done = event('a', 2, 'capture.end', 13)
        self.assertTrue(done['sequence_gap'])
        self.assertEqual(captures.watermarks(), {('t', 0): 14})


if __name__ == '__main__':
    unittest.main()
