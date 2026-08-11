# データソース調査結果

実地調査日: 2026-08-11。すべて実際にリクエストして確認済み。

## 1. robots.txt の調査結果（重要）

### keirin.jp（公式・JKA）— レースデータは収集不可

```
User-agent: *
Disallow:/
Allow:/pc/top
Allow:/pc/raceschedule
Allow:/pc/racerprofile
Allow:/pc/racerrecordranking
Allow:/pc/jyobunseki
Allow:/pc/dividendranking
...（以下ホワイトリスト形式）
```

**`Disallow:/` に対するホワイトリスト方式**で、許可リストに
**出走表・レース結果・オッズのページが含まれていない**。
つまり公式サイトからのレースデータ収集は robots.txt で明示的に拒否されている。

一方で以下は許可されているので、**選手マスタ系の一次情報は公式から取れる**：

| 用途 | パス |
|---|---|
| 選手プロフィール | `/pc/racerprofile`, `/sp/racerprofile` |
| 選手通算成績 | `/sp/racertotalrecord` |
| 選手直近成績 | `/sp/racerracentrecord` |
| 選手成績ランキング | `/pc/racerrecordranking` |
| 開催日程 | `/pc/raceschedule`, `/pc/graderaceschedule` |
| **競輪場データ（バンク特性）** | `/pc/jyobunseki`, `/pc/jyoguide` |
| 払戻ランキング | `/pc/dividendranking` |

→ **方針: バンク諸元と選手マスタは公式、レース単位のデータは netkeirin。**

### winticket.jp — レースページは収集不可

```
Disallow: /keirin/cups/
```

レース情報は `/keirin/cups/` 配下にあるため、実質的に全面禁止。**使わない。**

### oddspark.com — クロール自体は禁止されていない

`Disallow` の記述はなく、`Noindex:`（非標準ディレクティブ、検索エンジン向け）のみ。
`/keirin/Odds.do`, `/keirin/RaceKekka.do`, `/keirin/RaceList.do` が存在。
→ **netkeirin が落ちたときのフォールバック候補**として保持。常用はしない。

### keirin.netkeiba.com（netkeirin）— 全面許可

```
User-agent: *
Disallow:
```

`Disallow` が空 = 全パス許可。**これをメインの収集元とする。**

---

## 2. netkeirin の内部 JSON API（メイン収集経路）

HTMLスクレイピングではなく、サイト自身が使っている JSON API を叩ける。
HTMLパースより桁違いに安定かつ軽量。

- **エンドポイント**: `https://keirin.netkeiba.com/api/race/`
- **メソッド**: `POST`（`output=json` のとき JS 実装が POST を使う）
- **共通パラメータ**: `class=<クラス名>`, `method=get`, `compress=0`, `input=UTF-8`, `output=json`
  - `compress=1` にすると payload が base64+zlib で返る。`compress=0` で生JSONになる。

### レスポンス形状

```json
{"status":"OK","reason":"","data":{
   "<prefix><key>": <payload>,
   "<prefix><key>_last_dt": ""
}}
```

`_last_dt` で終わるキーを除いた1件が本体。`status` が `NG` のときは `reason` にエラー理由。

### 利用可能なクラス

| class | 必須パラメータ | 返るもの |
|---|---|---|
| `AplKaisai` | `year`, `syusai` | その年の**全開催カレンダー**（日付 × 場）。1リクエストで年間分 |
| `AplRace` | `kaisai_date`, `syusai`(=jyo_cd) | その日その場の**全レース**（race_id, 発走/**締切時刻**, 距離, 車立, 天候, 賭式別発売状態） |
| `AplRaceHorse` | `race_id` | 出走選手（車番/枠番/氏名/級班/府県/期/年齢/**競走得点**） |
| `AplRaceOdds` | `race_id` | **全賭式のオッズ** + `official_dt`（オッズ公式時刻） |
| `AplNarabiYoso` | `race_id` | **並び予想＝ライン構成** |
| `AplNarabiYoso2` | `race_id` | 並び予想（別ソース） |

### race_id の形式

```
202608112101
└──┬─┘└┬┘└┬┘└┬┘
 YYYY MM DD  │  │
        場コード(2桁) │
              R番号(2桁)
```

`YYYYMMDD` + `jyo_cd`(2桁) + `race_no`(2桁) の12桁。

### AplNarabiYoso のレスポンス（ライン）

```json
{"lineForecast":[["5","1","7","0","2","4","3","0","6"]]}
```

**`"0"` がライン区切り**。上例は：

- ライン1: 5 → 1 → 7（先頭 / 番手 / 3番手）
- ライン2: 2 → 4 → 3
- 単騎: 6

競輪で最も重要かつ最も入手困難な「ライン構成」が構造化データで取れる。
ルールベース推定を自作する必要がない。**この発見が収集設計上いちばん大きい。**

### AplRaceOdds のレスポンス（オッズ）

```json
{"official_dt":"2026-08-11 10:46:00",
 "list_5":[["0102","3.2","0","2"], ...],
 "list_7":[["0102","1.6","2.1","2"], ...],
 "list_9":[["010203","91.9","0","26"], ...]}
```

配列の各要素は `[組番, オッズ下限, オッズ上限, 人気]`。
ワイド以外は上限が `"0"`（＝単一オッズ）。組番は車番2桁ゼロ埋めの連結。

**賭式コード対応表**（7車立てレースの件数から確定）:

| key | 件数(7車) | 件数(9車) | 賭式 |
|---|---|---|---|
| `list_5` | 21 = C(7,2) | 36 | 2車複 |
| `list_6` | 42 = 7P2 | 72 | 2車単 |
| `list_7` | 21 = C(7,2) | 36 | ワイド（下限・上限の2値） |
| `list_8` | 35 = C(7,3) | 84 | 3連複 |
| `list_9` | 210 = 7P3 | 504 | 3連単 |

`official_dt` があるおかげで、**同じオッズを重複保存せずに済み、かつ時系列が正確に復元できる**。
スナップショットは `official_dt` が変わったときだけ書けばよい。

---

## 3. HTML から取る補完情報

JSON API に含まれない項目は HTML パースで補う（レース確定後に1回だけ取ればよいので低頻度）。

| URL | 取れるもの |
|---|---|
| `/race/entry/?race_id=` | **脚質**(逃/追/両), S/H/B回数, **決まり手内訳**(逃げ/まくり/差し/マーク), 着度数, 勝率/連対率, **ギヤ倍数**, 選手コメント, 本紙予想印 |
| `/race/result/?race_id=` | 着順, 着差, **上りタイム**, **決まり手**, S/B, 全賭式の払戻金・人気 |
| `/race/data/?race_id=` | データ分析 |
| `/race/player_comment/?race_id=` | 選手コメント |
| `/race/match_list/?race_id=` | 対戦表 |

いずれもサーバーレンダリングされており JS 実行は不要。

---

## 4. 収集マナー（実装済みの制約）

- リクエスト間隔は**最低 1.5 秒**（`NetkeirinClient` がグローバルに強制）
- 失敗時は指数バックオフでリトライ、諦めたら次に進む（収集を止めない）
- User-Agent に連絡先を入れられるよう設定可能
- 同じ `official_dt` のオッズは再保存しない（無駄なI/Oとリクエストを減らす）

ブロックされて収集が止まることが最大の損失なので、速度より継続性を優先する設計にしている。
