# ふらっとルート スポットデータ

アプリ「ふらっとルート」がコースを作るときに使う、日本のスポット・峠道・休憩場所のデータです。
OpenStreetMap の日本データ（[Geofabrik](https://download.geofabrik.de/asia/japan.html) 配布）から、毎週自動で作り直しています。

データ © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright)（[ODbL](https://opendatacommons.org/licenses/odbl/)）

## 中身

`v1/manifest.json` に作成日とマスの一覧があり、各マスは `v1/{種類}/{緯度マス}_{経度マス}.json.gz` です。
マスは0.5度ごと（`緯度マス = floor(緯度 / 0.5)`）です。

| 種類 | 内容 |
|---|---|
| `roads` | 峠の地点、峠道・名前つきの山岳道路・くねくねした道（道の形つき） |
| `food` | カフェ・食事・スイーツ |
| `spots` | 展望・海・道の駅・温泉・自然・湖・歴史・花・美術館・直売所・牧場 |
| `rests` | 道の駅・コンビニ・SA/PA（休憩場所） |

各ファイルは Overpass API の返事と同じ形（`{"elements": [...]}`）です。道の形だけ
`"g": [緯度, 経度, 緯度, 経度, ...]` に縮めています。各要素には `prefectureCode`（例: `JP-13`）を付けています。

## 作り方

```
pip install -r requirements.txt
python build_tiles.py japan-latest.osm.pbf site
python check_tiles.py site
```

`.github/workflows/build.yml` が毎週月曜 3:00（日本時間）に実行し、`gh-pages` ブランチへ公開します（履歴は残しません）。
