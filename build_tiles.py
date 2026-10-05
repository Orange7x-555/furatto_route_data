"""ふらっとルート用のスポットデータを OpenStreetMap (.osm.pbf) から作る。

出力はアプリの OverpassParser がそのまま読める形（Overpass の JSON と同じ要素）で、
0.5度ごとのマス × 種類（roads / food / spots / rests）に分けて gzip で保存する。

    python build_tiles.py japan-latest.osm.pbf out/

アプリ側の判定（lib/logic/overpass_parser.dart）と同じ条件で絞り込む。
"""
import gzip
import json
import math
import os
import re
import sys
import time
from collections import defaultdict

import osmium
from shapely.geometry import Point, LineString
from shapely.ops import polygonize, unary_union
from shapely.strtree import STRtree

CELL = 0.5  # マスの大きさ（度）
FORMAT_VERSION = 1

# アプリが読むタグだけを残す（ファイルを小さくする）
KEEP_TAGS = {
    'access', 'amenity', 'bath:type', 'brand', 'brand:ja', 'brand:wikidata',
    'contact:phone', 'contact:website', 'cuisine', 'ele', 'fee', 'garden:type',
    'heritage', 'highway', 'historic', 'image', 'leisure', 'man_made',
    'motor_vehicle', 'motorcar', 'motorcycle', 'mountain_pass', 'name',
    'name:en', 'name:ja', 'natural', 'oneway', 'opening_hours', 'parking',
    'phone', 'ref', 'scenic', 'shop', 'surface', 'tourism', 'water',
    'waterway', 'website', 'wikidata', 'wikimedia_commons', 'wikipedia',
    'wikipedia:ja',
}

FOOD = {'cafe', 'gourmet', 'sweets'}

WINDING_NAME = re.compile(
    r'峠|スカイライン|ライン$|スカイウェイ|パークウェイ|ドライブウェイ|山岳道路|登山道路|高原道路|観光道路|林道')
NOT_ROAD_NAME = re.compile(r'カフェ|喫茶|食堂|ホテル|旅館|レストラン|商店|茶屋|登山口')
ROAD_CLASSES = {'trunk', 'primary', 'secondary', 'tertiary', 'unclassified'}


def winding_road_name(name):
    return bool(WINDING_NAME.search(name)) and not NOT_ROAD_NAME.search(name)


def drivable_road(t):
    hw = t.get('highway')
    ok = hw in ROAD_CLASSES or (
        hw in ('service', 'track')
        and t.get('surface') in ('asphalt', 'paved', 'concrete')
        and any(t.get(k) in ('yes', 'designated')
                for k in ('motor_vehicle', 'motorcar', 'motorcycle')))
    return ok and t.get('access') not in ('private', 'no') and t.get('motor_vehicle') != 'no'


def poi_genres(t):
    """Overpass のクエリ（overpass_queries.dart の selectorsFor）と同じ条件"""
    g = set()
    name = t.get('name')
    has_name = bool(name)
    a, tour, leis, nat, shop = (t.get('amenity'), t.get('tourism'), t.get('leisure'),
                                t.get('natural'), t.get('shop'))
    if a == 'cafe' and has_name:
        g.add('cafe')
    if has_name and (leis == 'garden' or t.get('garden:type') == 'botanical'):
        g.add('flower')
    if has_name and (shop in ('confectionery', 'pastry', 'ice_cream') or a == 'ice_cream'):
        g.add('sweets')
    if has_name and tour in ('museum', 'gallery'):
        g.add('art')
    if has_name and (a == 'marketplace' or shop == 'farm'):
        g.add('market')
    if has_name and (tour == 'zoo' or (tour == 'attraction' and re.search('牧場|ふれあい', name))):
        g.add('farm')
    if tour == 'viewpoint':
        g.add('view')
    if has_name and (nat in ('beach', 'cape') or t.get('man_made') == 'lighthouse'):
        g.add('coast')
    if name and name.startswith('道の駅'):
        g.add('michinoeki')
    if has_name and (a == 'public_bath' or leis == 'spa'):
        g.add('onsen')
    if has_name and a == 'restaurant':
        g.add('gourmet')
    if has_name and (t.get('waterway') == 'waterfall'
                     or nat in ('gorge', 'valley', 'cave_entrance') or leis == 'park'):
        g.add('nature')
    if has_name and (t.get('waterway') == 'dam'
                     or (nat == 'water' and t.get('water') in ('lake', 'reservoir'))):
        g.add('lake')
    if has_name and (t.get('historic') in ('castle', 'ruins', 'monument', 'memorial')
                     or a == 'place_of_worship'):
        g.add('history')
    return g


def is_rest(t, is_node):
    name = t.get('name') or ''
    return (name.startswith('道の駅')
            or t.get('highway') in ('services', 'rest_area')
            or (is_node and t.get('shop') == 'convenience'))


def is_pass(t):
    return t.get('mountain_pass') == 'yes' or t.get('natural') == 'saddle'


# ---- geo_utils.dart と同じ計算 ----
R = 6371008.8


def dist(a, b):
    la1, la2 = math.radians(a[0]), math.radians(b[0])
    dlat = la2 - la1
    dlon = math.radians(b[1] - a[1])
    h = math.sin(dlat / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(dlon / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(h)))


def bearing(a, b):
    la1, la2 = math.radians(a[0]), math.radians(b[0])
    dlon = math.radians(b[1] - a[1])
    y = math.sin(dlon) * math.cos(la2)
    x = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def length_m(pts):
    return sum(dist(pts[i - 1], pts[i]) for i in range(1, len(pts)))


def curvature(pts):
    if len(pts) < 3:
        return 0.0
    p = [pts[0]]
    for i in range(1, len(pts)):
        if dist(p[-1], pts[i]) >= 30 or i == len(pts) - 1:
            p.append(pts[i])
    if len(p) < 3:
        return 0.0
    ln = length_m(p)
    if ln < 300:
        return 0.0
    turn = 0.0
    for i in range(1, len(p) - 1):
        d = abs(bearing(p[i], p[i + 1]) - bearing(p[i - 1], p[i])) % 360
        if d > 180:
            d = 360 - d
        turn += min(d, 120)
    return turn / (ln / 1000.0)


def winding_geometry(t, line):
    """overpass_parser.dart の windingGeometry（少しゆるめ。最終判定はアプリ）"""
    if not drivable_road(t) or len(line) < 5 or t.get('surface') in (
            'unpaved', 'gravel', 'dirt', 'ground', 'sand'):
        return False
    ln = length_m(line)
    if ln < 950:
        return False
    direct = dist(line[0], line[-1])
    return direct > 100 and ln / direct >= 1.12 and curvature(line) >= 110


def seg_dist_m(p, a, b):
    scale = math.cos(math.radians(p[0]))
    ax, ay = (a[1] - p[1]) * scale, a[0] - p[0]
    dx, dy = (b[1] - a[1]) * scale, b[0] - a[0]
    l2 = dx * dx + dy * dy
    t = 0.0 if l2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / l2))
    return dist(p, (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))


def identity(t):
    """road_sections.dart の _identity と同じ"""
    name = t.get('name:ja') or t.get('name') or t.get('ref') or ''
    cls = t.get('highway') if not name else ''
    return f"{name}|{cls}|{t.get('oneway', 'no')}|{t.get('motorcycle')}|{t.get('motorcar')}"


def trim(tags):
    return {k: v for k, v in tags.items() if k in KEEP_TAGS}


def cell_of(lat, lon):
    return f"{math.floor(lat / CELL)}_{math.floor(lon / CELL)}"


def r5(x):
    return round(x, 5)


def main(src, out_dir):
    t0 = time.time()
    road_filter = [('highway', v) for v in sorted(ROAD_CLASSES | {'track'})]
    poi_keys = ('amenity', 'tourism', 'leisure', 'natural', 'shop', 'man_made', 'waterway',
                'historic', 'garden:type', 'mountain_pass', 'highway', 'name', 'boundary')

    # ---- 1回目：点・リレーション ----
    passes = []          # (id, lat, lon, tags)
    elements = []        # (layers:set, record dict, (lat, lon))
    relations = []       # (id, tags, [way ids], kind)
    for o in osmium.FileProcessor(src, osmium.osm.NODE | osmium.osm.RELATION) \
            .with_filter(osmium.filter.EmptyTagFilter()) \
            .with_filter(osmium.filter.KeyFilter(*poi_keys)):
        tags = dict(o.tags)
        if o.is_node():
            if not o.location.valid():
                continue
            lat, lon = o.location.lat, o.location.lon
            if is_pass(tags):
                passes.append((o.id, lat, lon, tags))
            layers = layers_for(tags, True)
            if layers:
                elements.append((layers, {'type': 'node', 'id': o.id, 'lat': r5(lat),
                                          'lon': r5(lon), 'tags': trim(tags)}, (lat, lon)))
        else:
            ways = [m.ref for m in o.members if m.type == 'w']
            if not ways:
                continue
            if (tags.get('boundary') == 'administrative' and tags.get('admin_level') == '4'
                    and (tags.get('ISO3166-2') or '').startswith('JP-')):
                relations.append((o.id, tags, ways, 'pref'))
            elif tags.get('type') == 'multipolygon' and layers_for(tags, False):
                relations.append((o.id, tags, ways, 'poi'))
    print(f'pass1: {len(passes)} passes, {len(elements)} nodes, {len(relations)} relations '
          f'({time.time() - t0:.0f}s)', flush=True)

    # 峠の近くを素早く調べるための格子（約0.01度）
    pass_grid = defaultdict(list)
    pass_ids = set()
    for pid, lat, lon, _ in passes:
        pass_grid[(int(lat * 100), int(lon * 100))].append((lat, lon))
        pass_ids.add(pid)

    def near_pass(line):
        seen = set()
        for lat, lon in line:
            key = (int(lat * 100), int(lon * 100))
            if key in seen:
                continue
            seen.add(key)
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    for p in pass_grid.get((key[0] + dy, key[1] + dx), ()):
                        for i in range(1, len(line)):
                            if seg_dist_m(p, line[i - 1], line[i]) <= 120:
                                return True
        return False

    # 峠から5km以内か（約0.05度の格子で近いものだけ調べる）
    pass_grid5 = defaultdict(list)
    for _, lat, lon, _ in passes:
        pass_grid5[(int(lat * 20), int(lon * 20))].append((lat, lon))

    def within_5km_of_pass(line):
        for lat, lon in line[:: max(1, len(line) // 8)] + [line[-1]]:
            key = (int(lat * 20), int(lon * 20))
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    for p in pass_grid5.get((key[0] + dy, key[1] + dx), ()):
                        if dist(p, (lat, lon)) <= 5000:
                            return True
        return False

    member_ways = {w for _, _, ws, _ in relations for w in ws}

    # ---- 2回目：線（位置つき） ----
    seeds = {}           # 峠の候補になる道（形つき）
    light = {}           # 名前のある道の端点（つながりを調べる用）
    member_coords = {}
    for o in osmium.FileProcessor(src, osmium.osm.NODE | osmium.osm.WAY).with_locations() \
            .with_filter(osmium.filter.EntityFilter(osmium.osm.WAY)) \
            .with_filter(osmium.filter.KeyFilter(*poi_keys)):
        wid = o.id
        hw = o.tags.get('highway')
        if hw is not None and hw not in ROAD_CLASSES and hw not in (
                'track', 'service', 'services', 'rest_area') and 'name' not in o.tags:
            continue  # 歩道・住宅地の道など（大量）は早めに捨てる
        tags = dict(o.tags)
        coords = None
        layers = layers_for(tags, False)
        if layers:
            coords = [(n.location.lat, n.location.lon) for n in o.nodes if n.location.valid()]
            if coords:
                lats = [c[0] for c in coords]
                lons = [c[1] for c in coords]
                c = ((min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2)
                elements.append((layers, {'type': 'way', 'id': wid,
                                          'center': {'lat': r5(c[0]), 'lon': r5(c[1])},
                                          'tags': trim(tags)}, c))
        if tags.get('highway') not in ROAD_CLASSES | {'track', 'service'} or not drivable_road(tags):
            continue
        nodes = [n.ref for n in o.nodes]
        if coords is None:
            coords = [(n.location.lat, n.location.lon) for n in o.nodes if n.location.valid()]
        if len(coords) < 2 or len(coords) != len(nodes):
            continue
        name = tags.get('name') or ''
        seed = (winding_road_name(name) or tags.get('scenic') == 'yes'
                or any(n in pass_ids for n in nodes)
                or winding_geometry(tags, coords)
                or (pass_grid and near_pass(coords)))
        if seed:
            seeds[wid] = (tags, nodes, coords)
        elif ((name and tags.get('highway') in ('secondary', 'tertiary', 'unclassified'))
              or (pass_grid and within_5km_of_pass(coords))):
            # つながる区間の候補。以前の Overpass の検索と同じ範囲にそろえる
            # （名前つきの県道など＋峠から5km以内の道）。国道を何十kmもつなげないため
            light[wid] = (identity(tags), nodes[0], nodes[-1])
    print(f'pass2: {len(seeds)} road seeds, {len(light)} named roads '
          f'({time.time() - t0:.0f}s)', flush=True)

    # 同じ道（名前・種類が同じ）でつながる区間を足す。アプリで入口〜出口をつなげるため
    by_end = defaultdict(list)
    for wid, (ident, a, b) in light.items():
        by_end[(ident, a)].append(wid)
        by_end[(ident, b)].append(wid)
    extra = set()
    for wid, (tags, nodes, _) in seeds.items():
        ident = identity(tags)
        todo = [nodes[0], nodes[-1]]
        guard = 0
        while todo and guard < 300:
            guard += 1
            end = todo.pop()
            for nxt in by_end.get((ident, end), ()):
                if nxt in extra:
                    continue
                extra.add(nxt)
                _, a, b = light[nxt]
                todo.extend((a, b))
    print(f'continuations: {len(extra)} ways', flush=True)

    # ---- 3回目：つながる区間と、リレーションを作る線の形 ----
    wanted = extra | member_ways
    if wanted:
        for o in osmium.FileProcessor(src, osmium.osm.NODE | osmium.osm.WAY).with_locations() \
                .with_filter(osmium.filter.EntityFilter(osmium.osm.WAY)) \
                .with_filter(osmium.filter.IdFilter(wanted)):
            coords = [(n.location.lat, n.location.lon) for n in o.nodes if n.location.valid()]
            if o.id in member_ways:
                member_coords[o.id] = coords
            if o.id in extra and len(coords) == len(o.nodes) and len(coords) >= 2:
                seeds[o.id] = (dict(o.tags), [n.ref for n in o.nodes], coords)
    print(f'pass3: {len(seeds)} roads total ({time.time() - t0:.0f}s)', flush=True)

    # ---- 都道府県の形 ----
    prefs = []
    for rid, tags, ways, kind in relations:
        if kind != 'pref':
            continue
        lines = [LineString(member_coords[w]) for w in ways
                 if w in member_coords and len(member_coords[w]) >= 2]
        # shapely は (x=経度, y=緯度)
        lines = [LineString([(c[1], c[0]) for c in l.coords]) for l in lines]
        polys = list(polygonize(unary_union(lines))) if lines else []
        if polys:
            prefs.append((tags['ISO3166-2'], unary_union(polys)))
    print(f'prefectures: {len(prefs)}', flush=True)
    tree = STRtree([g for _, g in prefs]) if prefs else None

    def pref_of(lat, lon):
        if tree is None:
            return None
        pt = Point(lon, lat)
        for i in tree.query(pt):
            if prefs[i][1].covers(pt):
                return prefs[i][0]
        return None

    # 面のスポット（リレーション）
    for rid, tags, ways, kind in relations:
        if kind != 'poi':
            continue
        pts = [c for w in ways for c in member_coords.get(w, ())]
        if not pts:
            continue
        lats = [c[0] for c in pts]
        lons = [c[1] for c in pts]
        c = ((min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2)
        elements.append((layers_for(tags, False), {'type': 'relation', 'id': rid,
                                                   'center': {'lat': r5(c[0]), 'lon': r5(c[1])},
                                                   'tags': trim(tags)}, c))

    # ---- マスに分けて書き出す ----
    tiles = defaultdict(list)
    for layers, rec, (lat, lon) in elements:
        code = pref_of(lat, lon)
        if code:
            rec['tags']['prefectureCode'] = code
        for layer in layers:
            tiles[(layer, cell_of(lat, lon))].append(rec)
    for pid, lat, lon, tags in passes:
        # 峠は roads にも入れる（道と一緒に読むため）
        rec = {'type': 'node', 'id': pid, 'lat': r5(lat), 'lon': r5(lon), 'tags': trim(tags)}
        code = pref_of(lat, lon)
        if code:
            rec['tags']['prefectureCode'] = code
        tiles[('roads', cell_of(lat, lon))].append(rec)
    for wid, (tags, nodes, coords) in seeds.items():
        mid = coords[len(coords) // 2]
        t = trim(tags)
        code = pref_of(*mid)
        if code:
            t['prefectureCode'] = code
        # 形は [緯度, 経度, 緯度, 経度, ...] の平たい配列（アプリで Overpass の形に戻す）
        rec = {'type': 'way', 'id': wid, 'nodes': nodes,
               'g': [v for c in coords for v in (r5(c[0]), r5(c[1]))], 'tags': t}
        tiles[('roads', cell_of(*mid))].append(rec)

    os.makedirs(out_dir, exist_ok=True)
    total = 0
    index = defaultdict(list)
    for (layer, cell), recs in sorted(tiles.items()):
        d = os.path.join(out_dir, f'v{FORMAT_VERSION}', layer)
        os.makedirs(d, exist_ok=True)
        data = json.dumps({'elements': recs}, ensure_ascii=False, separators=(',', ':'))
        path = os.path.join(d, f'{cell}.json.gz')
        with gzip.open(path, 'wb', compresslevel=9) as f:
            f.write(data.encode('utf-8'))
        total += os.path.getsize(path)
        index[layer].append(cell)
    manifest = {
        'format': FORMAT_VERSION,
        'cell': CELL,
        'built': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'source': os.path.basename(src),
        'attribution': '© OpenStreetMap contributors (ODbL)',
        'tiles': {k: sorted(v) for k, v in index.items()},
    }
    with open(os.path.join(out_dir, f'v{FORMAT_VERSION}', 'manifest.json'), 'w',
              encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, separators=(',', ':'))
    print(f'wrote {len(tiles)} tiles, {total / 1e6:.1f} MB gz ({time.time() - t0:.0f}s)')


def layers_for(tags, is_node):
    layers = set()
    g = poi_genres(tags)
    if g & FOOD:
        layers.add('food')
    if g - FOOD:
        layers.add('spots')
    if is_rest(tags, is_node):
        layers.add('rests')
    return layers


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
