import json
from pathlib import Path
import tempfile
import unittest
import uuid

import zstandard

from sessionizer import Captures
from sessions import Store
from turns import canonical, classify, echo_form, item_hash, parse_response

SIG = 'S' * 200  # Thinking signatures/encrypted reasoning make model output unique.


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
