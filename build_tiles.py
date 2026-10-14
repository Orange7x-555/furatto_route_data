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


PHOTO_NAME = re.compile('フォトスポット|撮影スポット|映え|インスタ|天空の|恋人の聖地|ブランコ')


def is_photo_spot(t):
    """映えスポット（overpass_parser.dart の photoSpot と同じ条件）"""
    name = t.get('name') or ''
    if not name:
        return False
    if t.get('tourism') in ('artwork', 'attraction'):
        return True
    if t.get('natural') in ('arch', 'rock', 'stone'):
        return True
    # お店・宿は名前に「映え」などが付いていても外す
    if 'amenity' in t or 'shop' in t or t.get('tourism') in ('hotel', 'guest_house', 'motel'):
        return False
    return bool(PHOTO_NAME.search(name))


def poi_genres(t):
    """Overpass のクエリ（overpass_queries.dart の selectorsFor）と同じ条件"""
    g = set()
    name = t.get('name')
    has_name = bool(name)
    a, tour, leis, nat, shop = (t.get('amenity'), t.get('tourism'), t.get('leisure'),
                                t.get('natural'), t.get('shop'))
    if is_photo_spot(t):
        g.add('photo')
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


# 冬季閉鎖など、期間つきの通行止め（アプリの seasonal_closure.dart が読む）
CLOSURE_TAGS = {
    'access:conditional', 'vehicle:conditional', 'motor_vehicle:conditional',
    'motorcar:conditional', 'motorcycle:conditional',
}
WINTER_NOTE = re.compile('冬[季期]閉鎖|冬[季期](通行止|閉鎖|全面通行止)|冬季.*通行止め')


def trim(tags):
    out = {k: v for k, v in tags.items() if k in KEEP_TAGS or k in CLOSURE_TAGS}
    # 説明文は大きいので、冬季閉鎖の書き込みがあるときだけ残す
    for k in ('note', 'description'):
        v = tags.get(k)
        if v and WINTER_NOTE.search(v):
            out[k] = v[:200]
    return out


# ---- 名前で選ぶコース（courses）：湖・島・半島の一周と、名前の付いた観光道路 ----
LOOP_AREA_KM2 = {'lake': (1.0, 1000.0), 'island': (3.0, 1000.0), 'peninsula': (10.0, 3000.0)}
# 外周から少しずらした線に沿って地点を置き、アプリで近くの道につなぐ。
# 湖は外側（湖のまわりの道）、島・半島は内側（海沿いの道）へずらす
LOOP_OFFSET_M = {'lake': 300.0, 'island': -250.0, 'peninsula': -250.0}
LOOP_SAMPLE_M = 500.0      # 一周できる道があるか調べる間隔
LOOP_ROAD_NEAR_M = 1000.0  # 調べる点からこの距離に車の通れる道があれば「道がある」
LOOP_MIN_COVERAGE = 0.85   # 外周の85%以上に道があれば一周コースにする
COURSE_ROAD_MIN_M = 5000.0
# 観光道路・景色の良い道の名前（街道は「さざなみ街道」のように、かなの名前だけ。
# 「甲州街道」のような昔の街道は国道で長すぎるため外す）
COURSE_ROAD_NAME = re.compile(
    r'スカイライン|ライン$|スカイウェイ|パークウェイ|ドライブウェイ|ハイウェイ$|観光道路|高原道路|山岳道路'
    r'|シーサイド|サンセット|オーシャン|湖岸|湖畔|湖周|海岸線|いろは坂|ロード$|公園線$|スーパー林道|[ぁ-んァ-ヶー浜]街道$')


def loop_kind(t):
    if not t.get('name'):
        return None
    name = t['name']
    if t.get('natural') == 'water' and (t.get('water') in ('lake', 'reservoir') or name.endswith('湖')):
        return 'lake'
    if t.get('place') == 'island':
        return 'island'
    if t.get('place') == 'peninsula' or t.get('natural') == 'peninsula':
        return 'peninsula'
    return None


def course_road_name(name):
    # 「○○停車場線」は駅へ行く道なので外す
    return (bool(name) and bool(COURSE_ROAD_NAME.search(name))
            and not NOT_ROAD_NAME.search(name) and '停車場' not in name)


class LocalProjection:
    """緯度経度 ↔ メートル（狭い範囲なら十分な精度の平面）"""

    def __init__(self, lat0):
        self.kx = 111320.0 * math.cos(math.radians(lat0))
        self.ky = 110540.0

    def to_xy(self, pts):
        return [(lon * self.kx, lat * self.ky) for lat, lon in pts]

    def to_ll(self, xy):
        return [(y / self.ky, x / self.kx) for x, y in xy]


def loop_ring(kind, ring):
    """外周（緯度経度のリスト）から、コース用にずらした左回りの線と面積(km²)を作る。
    向きはアプリで決める（左回り＝日本の左側通行では水辺側の車線を走る向き）"""
    from shapely.geometry import Polygon
    from shapely.geometry.polygon import orient
    if len(ring) < 4:
        return None
    lat0 = sum(p[0] for p in ring) / len(ring)
    proj = LocalProjection(lat0)
    try:
        poly = Polygon(proj.to_xy(ring)).buffer(0)
    except Exception:
        return None
    if poly.is_empty:
        return None
    if poly.geom_type == 'MultiPolygon':
        poly = max(poly.geoms, key=lambda g: g.area)
    km2 = poly.area / 1e6
    lo, hi = LOOP_AREA_KM2[kind]
    if not lo <= km2 <= hi:
        return None
    moved = poly.buffer(LOOP_OFFSET_M[kind], resolution=4)
    if moved.is_empty:
        return None
    if moved.geom_type == 'MultiPolygon':
        moved = max(moved.geoms, key=lambda g: g.area)
    moved = orient(moved.simplify(80, preserve_topology=True), 1.0)
    coords = proj.to_ll(list(moved.exterior.coords))
    if len(coords) < 4:
        return None
    return coords, km2


def sample_line(pts, step_m):
    """線の上に step_m ごとの点を置く"""
    out = [pts[0]]
    carry = 0.0
    for i in range(1, len(pts)):
        a, b = pts[i - 1], pts[i]
        d = dist(a, b)
        pos = step_m - carry
        while pos <= d:
            t = pos / d
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
            pos += step_m
        carry = (carry + d) % step_m
    return out


ROAD_RANK = {'trunk': 3, 'primary': 3, 'secondary': 2, 'tertiary': 1, 'unclassified': 0}
RANK_BONUS_M = 120.0  # 太い道ほど、この距離ぶん近いものとして選ぶ


class LoopCoverage:
    """一周コースの外周の点ごとに、近くの車の通れる道の点を記録する。
    近くに道がある点の割合で「一周できるか」を決め、道の上の点をコースの形にする
    （アプリはこの点を通過点にするので、海や田んぼの中の点にならない）"""
    CELL = 50  # 0.02度ごとのマス

    def __init__(self, loops):
        self.samples = []   # [(lat, lon, loop_index)]
        self.best = []      # [(score, lat, lon) or None]
        self.grid = defaultdict(list)
        self.coarse = set()
        for li, loop in enumerate(loops):
            for p in sample_line(loop['ring'], LOOP_SAMPLE_M):
                i = len(self.samples)
                self.samples.append((p[0], p[1], li))
                self.best.append(None)
                self.grid[(int(p[0] * self.CELL), int(p[1] * self.CELL))].append(i)
                ck = (int(p[0] * 10), int(p[1] * 10))
                # まわりのマスも入れておき、道の点ごとに1回だけ調べれば済むようにする
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        self.coarse.add((ck[0] + dy, ck[1] + dx))

    def add_road(self, coords, highway):
        if not self.samples or highway not in ROAD_RANK:
            return
        bonus = ROAD_RANK[highway] * RANK_BONUS_M
        coarse = self.coarse
        for lat, lon in coords:
            if (int(lat * 10), int(lon * 10)) not in coarse:
                continue
            key = (int(lat * self.CELL), int(lon * self.CELL))
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    for i in self.grid.get((key[0] + dy, key[1] + dx), ()):
                        s = self.samples[i]
                        d = dist((s[0], s[1]), (lat, lon))
                        if d > LOOP_ROAD_NEAR_M:
                            continue
                        score = d - bonus
                        if self.best[i] is None or score < self.best[i][0]:
                            self.best[i] = (score, lat, lon)

    def ratio(self, li):
        n = c = 0
        for (_, _, l), b in zip(self.samples, self.best):
            if l == li:
                n += 1
                c += b is not None
        return c / n if n else 0.0

    def road_ring(self, li):
        """外周の順に並べた、道の上の点（同じ点が続くときは1つにする）"""
        out = []
        for (_, _, l), b in zip(self.samples, self.best):
            if l != li or b is None:
                continue
            p = (b[1], b[2])
            if not out or out[-1] != p:
                out.append(p)
        if len(out) > 1 and out[0] == out[-1]:
            out.pop()
        return out


CHAIN_GAP_M = 1500.0  # 名前の付いていないトンネル・橋をはさんでも、同じ道としてつなぐ距離


def chain_named_roads(named):
    """同じ名前の道を、端の近いものどうしで1本の線にまとめる。
    トンネル・橋の区間は名前が違う（または無い）ことが多いので、端が離れていてもつなぐ。
    上り下りが別の線の道は、反対側の線を拾って戻らないよう、近くを並走する線は足さない。
    named: {way_id: (name, tags, nodes, coords)} → [(name, [way_id...], coords)]"""
    by_name = defaultdict(list)
    for wid, rec in named.items():
        by_name[rec[0]].append(wid)
    out = []
    for name, wids in by_name.items():
        segs = {w: (named[w][3], named[w][1].get('oneway') in ('yes', '1', 'true')) for w in wids}
        unused = set(wids)
        while unused:
            first = max(unused, key=lambda w: length_m(segs[w][0]))
            unused.discard(first)
            line = list(segs[first][0])
            members = [first]
            while True:
                best = None
                for w in unused:
                    c, oneway = segs[w]
                    # 尾につなぐ（向きそのまま／逆向き）・頭につなぐ
                    options = [('tail', c, dist(line[-1], c[0]))]
                    options.append(('head', c, dist(c[-1], line[0])))
                    if not oneway:
                        options.append(('tail', c[::-1], dist(line[-1], c[-1])))
                        options.append(('head', c[::-1], dist(c[0], line[0])))
                    for side, cc, d in options:
                        if d <= CHAIN_GAP_M and (best is None or d < best[0]):
                            best = (d, w, side, cc)
                if best is None:
                    break
                _, w, side, cc = best
                unused.discard(w)
                mid = cc[len(cc) // 2]
                step = max(1, len(line) // 300)
                if min(dist(mid, line[i]) for i in range(0, len(line), step)) < 80:
                    continue  # 反対車線の線（並走）なので足さない
                members.append(w)
                if side == 'tail':
                    line.extend(cc if dist(line[-1], cc[0]) > 1 else cc[1:])
                else:
                    line = (cc if dist(cc[-1], line[0]) > 1 else cc[:-1]) + line
            out.append((name, members, cut_doubling(line)))
    return out


def cut_doubling(line):
    """上り下りが別の線の道で、反対側を戻ってきたときは、戻り始めで切る"""
    if len(line) < 3:
        return line
    cum = [0.0]
    for i in range(1, len(line)):
        cum.append(cum[-1] + dist(line[i - 1], line[i]))
    j0 = 0
    for i in range(1, len(line)):
        while j0 < i and cum[i] - cum[j0] > 2000:
            j0 += 1
        for j in range(0, j0, max(1, j0 // 200)):
            if dist(line[i], line[j]) < 150:
                return line[:i]
    return line


def simplify_ll(pts, tol_m):
    from shapely.geometry import LineString as LS
    if len(pts) < 3:
        return pts
    proj = LocalProjection(pts[len(pts) // 2][0])
    s = LS(proj.to_xy(pts)).simplify(tol_m, preserve_topology=False)
    return proj.to_ll(list(s.coords))


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

    # ---- 湖・島・半島の形（一周コースの候補）----
    loops = []
    for o in osmium.FileProcessor(src).with_areas(
            # 池・川（natural=water だけのもの）は数が多く形を作るのが重いので、湖・ダム湖だけ
            osmium.filter.TagFilter(('water', 'lake'), ('water', 'reservoir'), ('place', 'island'),
                                    ('place', 'peninsula'), ('natural', 'peninsula'))):
        if not o.is_area():
            continue
        tags = dict(o.tags)
        kind = loop_kind(tags)
        if not kind:
            continue
        best = None
        for outer in o.outer_rings():
            ring = [(n.lat, n.lon) for n in outer if n.location.valid()]
            if best is None or len(ring) > len(best):
                best = ring
        made = loop_ring(kind, best or [])
        if not made:
            continue
        ring, km2 = made
        loops.append({'id': f"{kind}/{'w' if o.from_way() else 'r'}{o.orig_id()}",
                      'kind': kind, 'name': tags.get('name:ja') or tags['name'],
                      'ring': ring, 'km2': km2, 'tags': trim(tags)})
    coverage = LoopCoverage(loops)
    print(f'loops: {len(loops)} candidates ({time.time() - t0:.0f}s)', flush=True)

    # ---- 2回目：線（位置つき） ----
    named = {}           # 観光道路の名前の道（形つき）
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
        coverage.add_road(coords, tags.get('highway'))
        name = tags.get('name') or ''
        if tags.get('highway') in ROAD_CLASSES and course_road_name(name):
            named[wid] = (tags.get('name:ja') or name, tags, nodes, coords)
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

    # ---- 名前で選ぶコース ----
    courses = []
    for li, loop in enumerate(loops):
        cov = coverage.ratio(li)
        if cov < LOOP_MIN_COVERAGE:
            continue
        ring = coverage.road_ring(li)
        if len(ring) < 8:
            continue
        t = dict(loop['tags'])
        t.update({'name': loop['name'], 'course': loop['kind'],
                  'km': f"{length_m(ring + ring[:1]) / 1000:.1f}", 'area': f"{loop['km2']:.1f}"})
        courses.append((loop['id'], ring, t))
    chains = chain_named_roads(named)
    for name, members, line in chains:
        if length_m(line) < COURSE_ROAD_MIN_M:
            continue
        first = named[members[0]][1]
        t = {k: v for k, v in trim(first).items() if k in CLOSURE_TAGS or k in (
            'note', 'description', 'oneway', 'surface', 'ref', 'scenic', 'name:en')}
        # 区間のどこかに冬季閉鎖の登録があれば残す
        for w in members[1:]:
            for k, v in trim(named[w][1]).items():
                if (k in CLOSURE_TAGS or k in ('note', 'description')) and k not in t:
                    t[k] = v
        t.update({'name': name, 'course': 'road', 'km': f'{length_m(line) / 1000:.1f}',
                  'curvy': f'{curvature(line):.0f}', 'highway': first.get('highway', '')})
        courses.append((f'road/w{members[0]}', simplify_ll(line, 25), t))
    course_index = []
    for cid, line, t in courses:
        lats = [p[0] for p in line]
        lons = [p[1] for p in line]
        c = ((min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2)
        code = pref_of(*c)
        if code:
            t['prefectureCode'] = code
        cell = cell_of(*c)
        rec = {'type': 'course', 'id': cid, 'center': {'lat': r5(c[0]), 'lon': r5(c[1])},
               'g': [v for p in line for v in (r5(p[0]), r5(p[1]))], 'tags': t}
        tiles[('courses', cell)].append(rec)
        course_index.append([cid, t['course'], t['name'], r5(c[0]), r5(c[1]), float(t['km']), cell,
                      code or ''])
    print(f"courses: {sum(1 for c in courses if c[2]['course'] != 'road')} loops, "
          f"{sum(1 for c in courses if c[2]['course'] == 'road')} roads", flush=True)

    os.makedirs(out_dir, exist_ok=True)
    # 全国のコースの名前の一覧（名前で探すため、1つのファイルにまとめる）
    os.makedirs(os.path.join(out_dir, f'v{FORMAT_VERSION}'), exist_ok=True)
    with gzip.open(os.path.join(out_dir, f'v{FORMAT_VERSION}', 'courses_index.json.gz'), 'wb',
                   compresslevel=9) as f:
        f.write(json.dumps({'courses': course_index}, ensure_ascii=False,
                           separators=(',', ':')).encode('utf-8'))
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
