"""作ったデータが壊れていないか確かめる（壊れたまま公開しないため）。

    python check_tiles.py site [最小マス数]
"""
import gzip
import json
import os
import sys

root = os.path.join(sys.argv[1], 'v1')
# 日本全体なら各種類100マス以上になる（関東だけの試作は30前後）
min_cells = int(sys.argv[2]) if len(sys.argv) > 2 else 100
m = json.load(open(os.path.join(root, 'manifest.json'), encoding='utf-8'))
problems = []
for layer in ('roads', 'food', 'spots', 'rests'):
    cells = m['tiles'].get(layer, [])
    n = 0
    with_pref = 0
    for cell in cells:
        with gzip.open(os.path.join(root, layer, f'{cell}.json.gz')) as f:
            els = json.load(f)['elements']
        n += len(els)
        with_pref += sum(1 for e in els if 'prefectureCode' in e.get('tags', {}))
    share = with_pref / n if n else 0
    print(f'{layer}: {len(cells)}マス・{n}件・都道府県つき {share:.0%}')
    if len(cells) < min_cells:
        problems.append(f'{layer}: マスが少なすぎます（{len(cells)} < {min_cells}）')
    if n == 0:
        problems.append(f'{layer}: データが空です')
    # 海の上の灯台などは県の外になるので、全体の8割に付いていれば良しとする
    elif layer != 'roads' and share < 0.8:
        problems.append(f'{layer}: 都道府県が付いていないものが多すぎます（{share:.0%}）')
# 名前で選ぶコース（湖・島の一周と観光道路）。日本全体なら数百件になる
idx_path = os.path.join(root, 'courses_index.json.gz')
if not os.path.exists(idx_path):
    problems.append('courses_index.json.gz がありません')
else:
    with gzip.open(idx_path) as f:
        idx = json.load(f)['courses']
    loops = sum(1 for c in idx if c[1] != 'road')
    roads = len(idx) - loops
    course_cells = set(m['tiles'].get('courses', []))
    print(f'courses: 一周 {loops}件・観光道路 {roads}件・{len(course_cells)}マス')
    need = max(3, min_cells // 4)
    if loops < need or roads < max(1, need // 4):
        problems.append(f'courses: コースが少なすぎます（一周 {loops}・道路 {roads}）')
    missing = [c[0] for c in idx if c[6] not in course_cells]
    if missing:
        problems.append(f'courses: {missing[0]} などのマスがありません')
if problems:
    print('\n'.join(problems))
    sys.exit(1)
print('OK')
