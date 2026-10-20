"""Durable conversation trees on SSD and one compressed JSONL file per session.

Each session is a tree of unique items (ChatGPT-export/oasst style
id/parent nodes). A turn inserts its input+output path; a request whose
content is a prefix of a later request therefore adds no new nodes. Turns join
a session through the deepest shared model-generated node (fork/resume
lineage) or, failing that, the client's declared session id.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

import zstandard

from turns import LINEAGE_MIN_BYTES, canonical, is_generated, node_path

SCHEMA = '''
CREATE TABLE IF NOT EXISTS offsets(topic TEXT, partition INTEGER, next_offset INTEGER, PRIMARY KEY(topic, partition));
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS blobs(hash BLOB PRIMARY KEY, data BLOB NOT NULL, size INTEGER NOT NULL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, source_id TEXT, user_id INTEGER, state TEXT, merged_into TEXT,
  first_ts REAL, last_ts REAL, dirty INTEGER, version INTEGER DEFAULT 0, flushed_at REAL, path TEXT, file_bytes INTEGER,
  resumed_from TEXT, merged_from TEXT DEFAULT '[]', node_seq INTEGER DEFAULT 0, inferred INTEGER);
CREATE INDEX IF NOT EXISTS sessions_state ON sessions(state, dirty, last_ts);
CREATE TABLE IF NOT EXISTS nodes(session_id TEXT, node_id BLOB, parent_id BLOB, seq INTEGER, origin TEXT, turn TEXT,
  api TEXT, blob BLOB, PRIMARY KEY(session_id, node_id)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS nodes_blob ON nodes(blob);
CREATE TABLE IF NOT EXISTS contexts(session_id TEXT, ctx_id BLOB, blob BLOB, PRIMARY KEY(session_id, ctx_id)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS contexts_blob ON contexts(blob);
CREATE TABLE IF NOT EXISTS turns(capture_id TEXT PRIMARY KEY, session_id TEXT, t_start TEXT, meta TEXT);
CREATE INDEX IF NOT EXISTS turns_session ON turns(session_id);
CREATE TABLE IF NOT EXISTS hints(source_id TEXT, user_id INTEGER, hint TEXT, session_id TEXT, PRIMARY KEY(source_id, user_id, hint)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS hints_session ON hints(session_id);
CREATE TABLE IF NOT EXISTS lineage(source_id TEXT, user_id INTEGER, node_id BLOB, session_id TEXT, PRIMARY KEY(source_id, user_id, node_id)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS lineage_session ON lineage(session_id);
CREATE TABLE IF NOT EXISTS dropped(capture_id TEXT PRIMARY KEY, source_id TEXT, user_id INTEGER, hint TEXT, reason TEXT, ts REAL, bytes INTEGER);
CREATE INDEX IF NOT EXISTS dropped_hint ON dropped(source_id, user_id, hint);
CREATE INDEX IF NOT EXISTS dropped_ts ON dropped(ts);
CREATE TABLE IF NOT EXISTS counters(day TEXT, source_id TEXT, reason TEXT, captures INTEGER, bytes INTEGER, PRIMARY KEY(day, source_id, reason)) WITHOUT ROWID;
'''
FILE_SCHEMA = 'qingtianji.ai-session.v1'


def instant(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def slim_usage(usage):
    # Responses usage.attribution is a per-item token breakdown (~20 KB per
    # turn, a quarter of all output bytes); totals and details are kept.
    if isinstance(usage, dict) and 'attribution' in usage:
        return {k: v for k, v in usage.items() if k != 'attribution'}
    return usage


def chunks(values, size=900):
    for start in range(0, len(values), size):
        yield values[start:start+size]


class Store:
    def __init__(self, path, output_root, idle_flush=1800, reopen_window=7*86400, max_unflushed=6*3600, tz_offset_hours=8):
        self.db = sqlite3.connect(path, isolation_level=None, timeout=60)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA cache_size=-262144')
        self.db.executescript(SCHEMA)
        self.output_root = Path(output_root)
        self.idle_flush, self.reopen_window, self.max_unflushed = idle_flush, reopen_window, max_unflushed
        self.tz = timezone(timedelta(hours=tz_offset_hours))
        self.zc_blob = zstandard.ZstdCompressor(level=3)
        self.zd = zstandard.ZstdDecompressor()
        self.in_tx = False
        self.garbage = True

    # -- transactions -------------------------------------------------------
    def begin(self):
        if not self.in_tx:
            self.db.execute('BEGIN IMMEDIATE')
            self.in_tx = True

    def commit(self, offsets=None):
        self.begin()
        for (topic, partition), value in (offsets or {}).items():
            self.db.execute('INSERT INTO offsets VALUES(?,?,?) ON CONFLICT(topic, partition) DO UPDATE SET next_offset=excluded.next_offset', (topic, partition, value))
        self.db.execute('COMMIT')
        self.in_tx = False

    def get_meta(self, key):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return row and row[0]

    def set_meta(self, key, value):
        self.begin()
        self.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, value))
        self.commit()

    def offsets(self):
        return {(t, p): o for t, p, o in self.db.execute('SELECT topic, partition, next_offset FROM offsets')}

    def known(self, capture_id):
        return self.db.execute('SELECT 1 FROM turns WHERE capture_id=? UNION ALL SELECT 1 FROM dropped WHERE capture_id=? LIMIT 1', (capture_id, capture_id)).fetchone() is not None

    # -- blobs --------------------------------------------------------------
    def put_blob(self, raw):
        digest = hashlib.sha256(raw).digest()
        self.db.execute('INSERT OR IGNORE INTO blobs VALUES(?,?,?)', (digest, self.zc_blob.compress(raw), len(raw)))
        return digest

    def get_blob(self, digest):
        row = self.db.execute('SELECT data FROM blobs WHERE hash=?', (digest,)).fetchone()
        return self.zd.decompress(row[0])

    # -- ingest -------------------------------------------------------------
    def record_drop(self, capture, reason, hint, size):
        start = capture.get('start') or {}
        ts = instant(capture['t1'])
        self.begin()
        cursor = self.db.execute('INSERT OR IGNORE INTO dropped VALUES(?,?,?,?,?,?,?)', (capture['capture_id'], capture['source_id'], start.get('user_id'), hint, reason, ts, size))
        if cursor.rowcount:
            self._count(capture['source_id'], reason, ts, size)

    def _count(self, source, reason, ts, size):
        day = datetime.fromtimestamp(ts, self.tz).strftime('%Y-%m-%d')
        self.db.execute('INSERT INTO counters VALUES(?,?,?,1,?) ON CONFLICT DO UPDATE SET captures=captures+1, bytes=bytes+excluded.bytes', (day, source, reason, size))

    def resolve(self, session_id):
        while session_id:
            row = self.db.execute('SELECT state, merged_into FROM sessions WHERE id=?', (session_id,)).fetchone()
            if row is None or row[0] != 'merged':
                return session_id, (row[0] if row else None)
            session_id = row[1]
        return None, None

    def _lineage(self, source, user, path, generated):
        candidates = [path[i] for i in range(len(path)) if generated[i]]
        found = {}
        for part in chunks(candidates):
            sql = 'SELECT node_id, session_id FROM lineage WHERE source_id=? AND user_id IS ? AND node_id IN (%s)' % ','.join('?'*len(part))
            found.update(self.db.execute(sql, (source, user, *part)).fetchall())
        for i in range(len(path)-1, -1, -1):
            if path[i] in found:
                return found[path[i]], i
        return None, -1

    def ingest(self, turn, size):
        """Insert one complete turn; returns the session id. Idempotent per capture."""
        self.begin()
        if self.db.execute('SELECT 1 FROM turns WHERE capture_id=?', (turn['capture_id'],)).fetchone():
            return None
        api, source, user = turn['api'], turn['source_id'], turn['user_id']
        items = turn['items'] + turn['output']
        n_in = len(turn['items'])
        raws = [canonical(x) for x in items]
        path = node_path(api, items, raws)
        generated = [(i >= n_in or is_generated(x)) and len(raws[i]) >= LINEAGE_MIN_BYTES for i, x in enumerate(items)]
        ts_start, ts_end = instant(turn['t_start']), instant(turn['t_end'])

        # Outputs count too: a later request may have echoed them before this turn arrived.
        session, depth = self._lineage(source, user, path, generated)
        session, state = self.resolve(session)
        resumed_from = None
        if state == 'sealed':
            session, resumed_from = None, session
        hinted = None
        if turn['hint']:
            row = self.db.execute('SELECT session_id FROM hints WHERE source_id=? AND user_id IS ? AND hint=?', (source, user, turn['hint'])).fetchone()
            if row:
                hinted, hinted_state = self.resolve(row[0])
                if hinted_state != 'active':
                    hinted = None
        if session and hinted and hinted != session:
            self.merge(session, hinted)
        session = session or hinted
        if session is None:
            session = self._new_session(source, user, ts_start, resumed_from, inferred=not turn['hint'])

        known = set()
        for part in chunks(path):
            sql = 'SELECT node_id FROM nodes WHERE session_id=? AND node_id IN (%s)' % ','.join('?'*len(part))
            known.update(r[0] for r in self.db.execute(sql, (session, *part)))
        seq = self.db.execute('SELECT node_seq FROM sessions WHERE id=?', (session,)).fetchone()[0]
        parent = None
        for i, node in enumerate(path):
            origin = 'output' if i >= n_in else 'input'
            if node not in known:
                seq += 1
                self.db.execute('INSERT INTO nodes VALUES(?,?,?,?,?,?,?,?)', (session, node, parent, seq, origin, turn['capture_id'] if origin == 'output' else None, api, self.put_blob(raws[i])))
            elif origin == 'output':
                # A later request may have echoed this output before the
                # producing turn arrived; record which turn generated it.
                self.db.execute("UPDATE nodes SET origin='output', turn=? WHERE session_id=? AND node_id=? AND turn IS NULL", (turn['capture_id'], session, node))
            if generated[i]:
                self.db.execute('INSERT OR REPLACE INTO lineage VALUES(?,?,?,?)', (source, user, node, session))
            parent = node
        ctx_raw = canonical(turn['context'])
        ctx_id = hashlib.sha256(ctx_raw).digest()[:16]
        self.db.execute('INSERT OR IGNORE INTO contexts VALUES(?,?,?)', (session, ctx_id, self.put_blob(ctx_raw)))
        meta = {k: turn[k] for k in ('capture_id', 'api', 'route', 't_start', 't_end', 'request_model', 'model', 'stream', 'usage', 'stop', 'response_id',
                                       'hint', 'window', 'subagent', 'api_key_id', 'group_id', 'params')}
        meta['usage'] = slim_usage(meta['usage'])
        meta.update(context=ctx_id.hex(), input_leaf=path[n_in-1].hex(), output_leaf=path[-1].hex() if len(path) > n_in else None,
                    n_input=n_in, n_output=len(turn['output']), lineage_depth=depth)
        self.db.execute('INSERT INTO turns VALUES(?,?,?,?)', (turn['capture_id'], session, turn['t_start'], json.dumps(meta, ensure_ascii=False)))
        if turn['hint']:
            self.db.execute('INSERT OR REPLACE INTO hints VALUES(?,?,?,?)', (source, user, turn['hint'], session))
        self.db.execute('UPDATE sessions SET node_seq=?, dirty=1, first_ts=min(first_ts, ?), last_ts=max(last_ts, ?) WHERE id=?', (seq, ts_start, ts_end, session))
        self._count(source, 'kept', ts_end, size)
        return session

    def _new_session(self, source, user, ts, resumed_from, inferred):
        stamp = datetime.fromtimestamp(ts, self.tz).strftime('%Y%m%dT%H%M%S')
        session = '%s-u%s-%s' % (stamp, user if user is not None else 'x', uuid.uuid4().hex[:10])
        self.db.execute("INSERT INTO sessions(id, source_id, user_id, state, first_ts, last_ts, dirty, resumed_from, inferred) VALUES(?,?,?,'active',?,?,1,?,?)",
                        (session, source, user, ts, ts, resumed_from, int(inferred)))
        return session

    def merge(self, dst, src):
        """Fold src into dst after a fork/resume links them."""
        offset = self.db.execute('SELECT node_seq FROM sessions WHERE id=?', (dst,)).fetchone()[0]
        moved = self.db.execute('SELECT max(seq) FROM nodes WHERE session_id=?', (src,)).fetchone()[0] or 0
        self.db.execute('UPDATE OR IGNORE nodes SET session_id=?, seq=seq+? WHERE session_id=?', (dst, offset, src))
        self.db.execute('DELETE FROM nodes WHERE session_id=?', (src,))
        self.db.execute('UPDATE OR IGNORE contexts SET session_id=? WHERE session_id=?', (dst, src))
        self.db.execute('DELETE FROM contexts WHERE session_id=?', (src,))
        for table in ('turns', 'hints', 'lineage'):
            self.db.execute('UPDATE %s SET session_id=? WHERE session_id=?' % table, (dst, src))
        first, last, merged_from = self.db.execute('SELECT first_ts, last_ts, merged_from FROM sessions WHERE id=?', (src,)).fetchone()
        sources = json.loads(self.db.execute('SELECT merged_from FROM sessions WHERE id=?', (dst,)).fetchone()[0]) + [src] + json.loads(merged_from)
        self.db.execute('UPDATE sessions SET node_seq=node_seq+?, first_ts=min(first_ts,?), last_ts=max(last_ts,?), dirty=1, merged_from=? WHERE id=?', (moved, first, last, json.dumps(sources), dst))
        self.db.execute("UPDATE sessions SET state='merged', merged_into=?, dirty=1 WHERE id=?", (dst, src))
        self.garbage = True

    # -- output -------------------------------------------------------------
    def due(self, clock):
        return [r[0] for r in self.db.execute("SELECT id FROM sessions WHERE state='active' AND dirty=1 AND (last_ts<=? OR coalesce(flushed_at, first_ts)<=?) ORDER BY last_ts LIMIT 200",
                                              (clock-self.idle_flush, clock-self.max_unflushed))]

    def relative_path(self, session):
        source, first = self.db.execute('SELECT source_id, first_ts FROM sessions WHERE id=?', (session,)).fetchone()
        day = datetime.fromtimestamp(first, self.tz).strftime('%Y-%m-%d')
        return 'source=%s/dt=%s/%s.jsonl.zst' % (source, day, session)

    def render(self, session):
        """Yield the JSONL lines of a session file: header, contexts, nodes, turns."""
        row = self.db.execute('SELECT source_id, user_id, first_ts, last_ts, version, resumed_from, merged_from, inferred FROM sessions WHERE id=?', (session,)).fetchone()
        source, user, first, last, version, resumed_from, merged_from, inferred = row
        turns = [json.loads(m) for (m,) in self.db.execute('SELECT meta FROM turns WHERE session_id=? ORDER BY t_start, capture_id', (session,))]
        for t in turns:
            t['usage'] = slim_usage(t['usage'])
        nodes = self.db.execute('SELECT node_id, parent_id, origin, turn, api, blob FROM nodes WHERE session_id=? ORDER BY seq', (session,)).fetchall()
        children = {}
        for node_id, parent, *_ in nodes:
            children[parent] = children.get(parent, 0) + 1
        leaves = [n[0].hex() for n in nodes if n[0] not in children]
        models, hints, dropped = {}, sorted({t['hint'] for t in turns if t['hint']}), {}
        for t in turns:
            models[t['model'] or t['request_model'] or ''] = models.get(t['model'] or t['request_model'] or '', 0) + 1
        for hint in hints:
            for reason, count in self.db.execute('SELECT reason, count(*) FROM dropped WHERE source_id=? AND user_id IS ? AND hint=? GROUP BY reason', (source, user, hint)):
                dropped[reason] = dropped.get(reason, 0) + count
        header = {'type': 'session', 'schema': FILE_SCHEMA, 'session_id': session, 'version': version+1, 'source_id': source, 'user_id': user,
                  'first_time': datetime.fromtimestamp(first, timezone.utc).isoformat(), 'last_time': datetime.fromtimestamp(last, timezone.utc).isoformat(),
                  'client_session_ids': hints, 'inferred': bool(inferred), 'resumed_from': resumed_from, 'merged_from': json.loads(merged_from),
                  'turns': len(turns), 'nodes': len(nodes), 'roots': children.get(None, 0), 'leaves': leaves,
                  'branch_points': sum(1 for k, v in children.items() if k is not None and v > 1), 'models': models,
                  'api_key_ids': sorted({t['api_key_id'] for t in turns if t['api_key_id'] is not None}), 'dropped_requests': dropped,
                  'written_at': datetime.now(timezone.utc).isoformat()}
        yield header
        # Blobs already hold canonical JSON; splice them into canonical (sorted-key)
        # lines instead of parsing and re-encoding every item.
        for ctx_id, digest in self.db.execute('SELECT ctx_id, blob FROM contexts WHERE session_id=? ORDER BY ctx_id', (session,)).fetchall():
            yield b'{"id":"%s","type":"context","value":%s}' % (ctx_id.hex().encode(), self.get_blob(digest))
        for node_id, parent, origin, turn, api, digest in nodes:
            yield b'{"api":%s,"id":"%s","item":%s,"origin":%s,"parent":%s,"turn":%s,"type":"node"}' % (
                canonical(api), node_id.hex().encode(), self.get_blob(digest), canonical(origin),
                canonical(parent.hex() if parent else None), canonical(turn))
        for t in turns:
            yield dict(t, type='turn')

    def write(self, session):
        """Render to a temp file and rename; the caller syncs once per batch."""
        relative = self.relative_path(session)
        target = self.output_root / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        temp = target.with_name('.' + target.name + '.tmp')
        # Level 3 + long-distance matching: within 5% of level 12 on session files, ~7x faster.
        params = zstandard.ZstdCompressionParameters.from_level(3, window_log=27, enable_ldm=True)
        raw = 0
        with open(temp, 'wb') as handle:
            with zstandard.ZstdCompressor(compression_params=params).stream_writer(handle, closefd=False) as writer:
                for record in self.render(session):
                    line = (record if isinstance(record, bytes) else canonical(record)) + b'\n'
                    raw += len(line)
                    writer.write(line)
        os.chmod(temp, 0o640)
        os.replace(temp, target)
        return relative, target.stat().st_size, raw

    def flush(self, clock, limit=200, budget_seconds=20.0):
        """Write due sessions, sync the filesystem once, then mark them clean.

        A crash before the final commit leaves the sessions dirty, so they are
        rewritten on restart; the state database never points at unsynced files.
        """
        started, done = time.monotonic(), []
        for session in self.due(clock)[:limit]:
            done.append((session,) + self.write(session)[:2])
            if time.monotonic() - started >= budget_seconds:
                break
        if not done:
            return 0, 0
        os.sync()
        self.begin()
        for session, relative, size in done:
            self.db.execute('UPDATE sessions SET dirty=0, version=version+1, flushed_at=?, path=?, file_bytes=? WHERE id=?', (clock, relative, size, session))
            for (old,) in self.db.execute("SELECT path FROM sessions WHERE state='merged' AND merged_into=? AND path IS NOT NULL", (session,)).fetchall():
                (self.output_root / old).unlink(missing_ok=True)
            self.db.execute("UPDATE sessions SET path=NULL, dirty=0 WHERE state='merged' AND merged_into=?", (session,))
        self.commit()
        return len(done), sum(size for _s, _r, size in done)

    def seal(self, clock, limit=100):
        """Drop SSD content of sessions idle past the reopen window; keep leaf lineage."""
        sealed = 0
        rows = self.db.execute("SELECT id, source_id, user_id FROM sessions WHERE state='active' AND dirty=0 AND last_ts<=? LIMIT ?", (clock-self.reopen_window, limit)).fetchall()
        for session, source, user in rows:
            self.begin()
            parents = {p for (p,) in self.db.execute('SELECT parent_id FROM nodes WHERE session_id=? AND parent_id IS NOT NULL', (session,))}
            tips = []
            for node_id, parent in self.db.execute('SELECT node_id, parent_id FROM nodes WHERE session_id=?', (session,)).fetchall():
                if node_id not in parents:
                    tips.append(node_id)
            keep = {n for (n,) in self.db.execute('SELECT node_id FROM lineage WHERE session_id=?', (session,))}
            self.db.execute('DELETE FROM lineage WHERE session_id=?', (session,))
            # Retain the deepest generated ancestor of each leaf for resumed_from links.
            by_id = dict(self.db.execute('SELECT node_id, parent_id FROM nodes WHERE session_id=?', (session,)).fetchall())
            for tip in tips:
                node = tip
                while node is not None and node not in keep:
                    node = by_id.get(node)
                if node is not None:
                    self.db.execute('INSERT OR REPLACE INTO lineage VALUES(?,?,?,?)', (source, user, node, session))
            for table in ('nodes', 'contexts', 'turns', 'hints'):
                self.db.execute('DELETE FROM %s WHERE session_id=?' % table, (session,))
            self.db.execute("UPDATE sessions SET state='sealed' WHERE id=?", (session,))
            self.garbage = True
            self.commit()
            sealed += 1
        return sealed

    def gc(self, clock, lineage_days=90):
        self.begin()
        self.db.execute('DELETE FROM dropped WHERE ts<?', (clock-self.reopen_window-86400,))
        self.db.execute("DELETE FROM lineage WHERE session_id IN (SELECT id FROM sessions WHERE state='sealed' AND last_ts<?)", (clock-lineage_days*86400,))
        if self.garbage:
            # Blobs only become unreferenced when sessions are sealed or merged.
            self.db.execute('DELETE FROM blobs WHERE hash NOT IN (SELECT blob FROM nodes) AND hash NOT IN (SELECT blob FROM contexts)')
            self.garbage = False
        self.commit()

    def catalog_rows(self, day):
        start = datetime.strptime(day, '%Y-%m-%d').replace(tzinfo=self.tz).timestamp()
        return self.db.execute("SELECT id, source_id, user_id, state, first_ts, last_ts, version, path, file_bytes, resumed_from, merged_from, inferred FROM sessions WHERE first_ts>=? AND first_ts<? AND path IS NOT NULL AND state!='merged'",
                               (start, start+86400)).fetchall()

    def stats(self):
        # Cheap queries only: this runs every few seconds next to ingestion.
        counts = dict(self.db.execute('SELECT state, count(*) FROM sessions GROUP BY state').fetchall())
        counts['dirty'] = self.db.execute("SELECT count(*) FROM sessions WHERE state='active' AND dirty=1").fetchone()[0]
        pages, size, free = (self.db.execute('PRAGMA ' + p).fetchone()[0] for p in ('page_count', 'page_size', 'freelist_count'))
        counts['state_db_bytes'], counts['state_db_free_bytes'] = pages*size, free*size
        return counts
