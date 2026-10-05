"""作ったデータが壊れていないか確かめる（壊れたまま公開しないため）。

    python check_tiles.py site [最小マス数]
"""
import gzip
import json
import os
import sys

root = os.path.join(sys.argv[1], 'v1')
min_cells = int(sys.argv[2]) if len(sys.argv) > 2 else 300  # 日本全体なら数百マス
m = json.load(open(os.path.join(root, 'manifest.json'), encoding='utf-8'))
problems = []
counts = {}
for layer in ('roads', 'food', 'spots', 'rests'):
    cells = m['tiles'].get(layer, [])
    if len(cells) < min_cells:
        problems.append(f'{layer}: マスが少なすぎます（{len(cells)}）')
    n = 0
    for cell in cells:
        with gzip.open(os.path.join(root, layer, f'{cell}.json.gz')) as f:
            n += len(json.load(f)['elements'])
    counts[layer] = n
print('件数:', counts)
pref = 0
with gzip.open(os.path.join(root, 'spots', m['tiles']['spots'][0] + '.json.gz')) as f:
    els = json.load(f)['elements']
pref = sum(1 for e in els if 'prefectureCode' in e.get('tags', {}))
if els and pref / len(els) < 0.5:
    problems.append(f'都道府県が付いていないスポットが多すぎます（{pref}/{len(els)}）')
if problems:
    print('\n'.join(problems))
    sys.exit(1)
print('OK')
