"""Read, verify and linearize session files written by sessionizer.py.

  python session_tool.py verify FILE...       recompute every node id from stored items
  python session_tool.py export FILE [--all]  one JSON line per conversation path
                                              (leaves only unless --all: every turn)
"""
import argparse
import json
import sys

import zstandard

from turns import node_path


def read(path):
    with open(path, 'rb') as handle:
        data = zstandard.ZstdDecompressor().stream_reader(handle).read()
    records = [json.loads(line) for line in data.splitlines()]
    header = records[0]
    if header.get('type') != 'session' or header.get('schema') != 'qingtianji.ai-session.v1':
        raise ValueError('not a session file')
    contexts = {r['id']: r['value'] for r in records if r['type'] == 'context'}
    nodes = {r['id']: r for r in records if r['type'] == 'node'}
    turns = [r for r in records if r['type'] == 'turn']
    return header, contexts, nodes, turns


def lineage(nodes, leaf):
    path = []
    while leaf is not None:
        path.append(nodes[leaf])
        leaf = nodes[leaf]['parent']
    return path[::-1]


def verify(path):
    header, contexts, nodes, turns = read(path)
    for node in nodes.values():
        if node['parent'] is not None and node['parent'] not in nodes:
            raise ValueError('dangling parent ' + node['id'])
    checked = set()
    for leaf in header['leaves']:
        chain = lineage(nodes, leaf)
        ids = [n.hex() for n in node_path(chain[-1]['api'], [n['item'] for n in chain])]
        if ids != [n['id'] for n in chain]:
            raise ValueError('node ids do not match stored items under leaf ' + leaf)
        checked.update(ids)
    if checked != set(nodes):
        raise ValueError('nodes not reachable from leaves')
    for turn in turns:
        if turn['context'] not in contexts or turn['input_leaf'] not in nodes or (turn['output_leaf'] and turn['output_leaf'] not in nodes):
            raise ValueError('turn references missing data ' + turn['capture_id'])
    return header


def export(path, every_turn=False):
    header, contexts, nodes, turns = read(path)
    by_leaf = {}
    for turn in turns:
        by_leaf.setdefault(turn['output_leaf'] or turn['input_leaf'], turn)
    targets = [t['output_leaf'] or t['input_leaf'] for t in turns] if every_turn else header['leaves']
    for leaf in dict.fromkeys(targets):
        turn = by_leaf.get(leaf)
        chain = lineage(nodes, leaf)
        yield {'session_id': header['session_id'], 'leaf': leaf, 'api': chain[-1]['api'], 'capture_id': turn and turn['capture_id'],
               'model': turn and (turn['model'] or turn['request_model']), 'context': turn and contexts[turn['context']],
               'items': [n['item'] for n in chain], 'generated': [n['origin'] == 'output' for n in chain]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['verify', 'export'])
    parser.add_argument('files', nargs='+')
    parser.add_argument('--all', action='store_true')
    args = parser.parse_args()
    failed = 0
    for path in args.files:
        try:
            if args.command == 'verify':
                header = verify(path)
                print(json.dumps({'file': path, 'ok': True, 'turns': header['turns'], 'nodes': header['nodes'], 'leaves': len(header['leaves'])}))
            else:
                for record in export(path, args.all):
                    sys.stdout.write(json.dumps(record, ensure_ascii=False) + '\n')
        except Exception as error:
            failed += 1
            print(json.dumps({'file': path, 'ok': False, 'error': str(error)[:200]}), file=sys.stderr)
    raise SystemExit(1 if failed else 0)


if __name__ == '__main__':
    main()
