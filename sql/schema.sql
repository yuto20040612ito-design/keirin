-- 競輪データベース スキーマ (DuckDB)
--
-- 設計方針:
--   * Raw層(data/raw/*.jsonl.gz)は不変・追記のみ。ここは Raw から再構築可能な派生物。
--   * 締切前に確定する情報(entries/lines/odds)と締切後に確定する情報(results/payouts)を
--     テーブルとして分離する。混ぜるとリークする。
--   * odds_snapshots は official_dt を主キーに含めることで、重複なく時系列が積み上がる。

-- ---------------------------------------------------------------------------
-- マスタ
-- ---------------------------------------------------------------------------

-- 競輪場マスタ。バンク諸元は keirin.jp の場データ(robots.txt 許可済)から取る。
-- 静的な情報なので収集は年1回で足りる。
CREATE TABLE IF NOT EXISTS velodromes (
    jyo_cd              VARCHAR PRIMARY KEY,  -- 場コード 2桁 ('21' = 弥彦)
    jyo_name            VARCHAR,
    bank_length_m       DOUBLE,   -- バンク周長 333 / 400 / 500
    straight_m          DOUBLE,   -- みなし直線距離。長いほど差しが決まりやすい
    bank_angle_deg      DOUBLE,   -- センター部カント
    straight_angle_deg  DOUBLE,   -- 直線部カント
    home_width_m        DOUBLE,
    back_width_m        DOUBLE,
    center_width_m      DOUBLE,
    max_agari_sec       DOUBLE,   -- 当該バンクの最高上がりタイム
    compass_deg         DOUBLE,   -- バンクの方位。風向と組めば向かい風/追い風が出る
    -- 当該バンクの1着決まり手構成 (逃げ/捲り/差しの3種で合計1)。
    -- 「そのバンクでどう決まりやすいか」がそのまま入っている。
    share_nige          DOUBLE,
    share_makuri        DOUBLE,
    share_sashi         DOUBLE,
    updated_at          TIMESTAMP
);

-- 選手マスタ。属性は時点で変わるため、変わるものは entries 側にも持つ。
CREATE TABLE IF NOT EXISTS players (
    player_id       VARCHAR PRIMARY KEY,   -- netkeirin の選手ID (db/profile/?id=)
    name            VARCHAR NOT NULL,
    name_kana       VARCHAR,
    prefecture      VARCHAR,               -- 府県
    graduate_period INTEGER,               -- 期別
    birth_date      DATE,
    updated_at      TIMESTAMP
);

-- ---------------------------------------------------------------------------
-- レース
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS races (
    race_id         VARCHAR PRIMARY KEY,   -- YYYYMMDD + jyo_cd(2) + race_no(2)
    kaisai_date     DATE NOT NULL,
    jyo_cd          VARCHAR NOT NULL,
    race_no         INTEGER NOT NULL,
    race_name       VARCHAR,
    jyoken          VARCHAR,               -- 条件 (A級 / S級 など)
    grade           VARCHAR,               -- G1/G2/G3/FI/FII など
    tosu            INTEGER,               -- 車立数 (7 / 9 など)
    kyori_m         INTEGER,               -- 距離(m)
    laps            INTEGER,               -- 周回数
    start_at        TIMESTAMP,             -- 発走時刻
    close_at        TIMESTAMP,             -- 投票締切時刻 ★オッズ収集の基準
    nichiji         INTEGER,               -- 開催日次 (初日=1)
    last_day_flg    BOOLEAN,
    tenko           VARCHAR,               -- 天候
    wind_dir        VARCHAR,               -- 風向
    wind_speed_ms   DOUBLE,                -- 風速
    temperature_c   DOUBLE,
    race_status     VARCHAR,
    is_girls        BOOLEAN,               -- ガールズケイリン
    is_midnight     BOOLEAN,               -- ミッドナイト競輪
    source          VARCHAR,               -- 取得元 ('netkeirin')
    updated_at      TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_races_date ON races (kaisai_date);
CREATE INDEX IF NOT EXISTS idx_races_close ON races (close_at);

-- ---------------------------------------------------------------------------
-- 出走表 (締切前に確定する情報のみ)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS entries (
    race_id         VARCHAR NOT NULL,
    syaban          INTEGER NOT NULL,      -- 車番
    wakuban         INTEGER,               -- 枠番
    player_id       VARCHAR,
    player_name     VARCHAR,
    player_kana     VARCHAR,
    prefecture      VARCHAR,
    age             INTEGER,
    graduate_period INTEGER,
    kyu             VARCHAR,               -- 級 (S / A)
    han             VARCHAR,               -- 班 (S / 1 / 2 / 3)
    rating          DOUBLE,                -- 競走得点 ★最重要の単体指標
    kyakushitsu     VARCHAR,               -- 脚質 (逃 / 追 / 両)
    gear_ratio      DOUBLE,                -- ギヤ倍数
    -- 直近成績 (出走表掲載値。集計期間はソース依存なので生値として保持)
    cnt_s           INTEGER,               -- S回数(先頭誘導後ろを取った回数)
    cnt_h           INTEGER,               -- H回数(ホーム先頭通過)
    cnt_b           INTEGER,               -- B回数(バック先頭通過)
    win_nige        INTEGER,               -- 決まり手内訳: 逃げ
    win_makuri      INTEGER,               -- 決まり手内訳: まくり
    win_sashi       INTEGER,               -- 決まり手内訳: 差し
    win_mark        INTEGER,               -- 決まり手内訳: マーク
    cnt_1st         INTEGER,
    cnt_2nd         INTEGER,
    cnt_3rd         INTEGER,
    cnt_out         INTEGER,               -- 着外
    rate_win        DOUBLE,                -- 勝率
    rate_top2       DOUBLE,                -- 2連対率
    rate_top3       DOUBLE,                -- 3連対率
    comment         VARCHAR,               -- 選手コメント
    honshi_mark     VARCHAR,               -- 本紙予想印
    updated_at      TIMESTAMP,
    PRIMARY KEY (race_id, syaban)
);

-- ライン構成。netkeirin の lineForecast を '0' 区切りで展開したもの。
-- 競輪で最も重要な構造情報。
CREATE TABLE IF NOT EXISTS race_lines (
    race_id         VARCHAR NOT NULL,
    line_no         INTEGER NOT NULL,      -- ライン番号 (1始まり)
    position        INTEGER NOT NULL,      -- ライン内位置 (1=先頭/先行, 2=番手, 3=3番手...)
    syaban          INTEGER NOT NULL,      -- 車番
    line_size       INTEGER NOT NULL,      -- そのラインの人数 (1なら単騎)
    is_solo         BOOLEAN NOT NULL,      -- 単騎フラグ
    source          VARCHAR,               -- 'AplNarabiYoso' / 'AplNarabiYoso2'
    fetched_at      TIMESTAMP,
    PRIMARY KEY (race_id, source, syaban)
);

-- ---------------------------------------------------------------------------
-- オッズ時系列 ★再取得不可能な最重要データ
-- ---------------------------------------------------------------------------

-- 賭式コード: 5=2車複, 6=2車単, 7=ワイド, 8=3連複, 9=3連単
CREATE TABLE IF NOT EXISTS bet_types (
    bet_type        INTEGER PRIMARY KEY,
    name            VARCHAR NOT NULL,
    is_ordered      BOOLEAN NOT NULL,      -- 着順を問うか
    n_picks         INTEGER NOT NULL
);

INSERT INTO bet_types (bet_type, name, is_ordered, n_picks) VALUES
    (5, '2車複',  FALSE, 2),
    (6, '2車単',  TRUE,  2),
    (7, 'ワイド', FALSE, 2),
    (8, '3連複',  FALSE, 3),
    (9, '3連単',  TRUE,  3)
ON CONFLICT (bet_type) DO NOTHING;

-- 注意: netkeirin の official_dt は「オッズが公式に確定した時刻」で、
-- **締切前は空文字**（実測で確認）。したがって締切前のスナップショットは
-- official_dt では区別できない。時系列の主キーには snapshot_at を使う。
CREATE TABLE IF NOT EXISTS odds_snapshots (
    race_id         VARCHAR NOT NULL,
    bet_type        INTEGER NOT NULL,
    combination     VARCHAR NOT NULL,      -- 車番2桁ゼロ埋め連結 ('0102', '010203')
    snapshot_at     TIMESTAMP NOT NULL,    -- ★このスナップショットの時刻(主キーの一部)
    official_dt     TIMESTAMP,             -- 公式確定時刻。締切前は NULL
    is_official     BOOLEAN NOT NULL,      -- official_dt が入っているか(＝確定オッズか)
    odds_low        DOUBLE,                -- ワイド以外はこれが確定オッズ
    odds_high       DOUBLE,                -- ワイドのみ上限。それ以外は NULL
    popularity      INTEGER,               -- 人気順
    fetched_at      TIMESTAMP NOT NULL,    -- こちらが取得した時刻
    secs_to_close   INTEGER,               -- 締切まで何秒か。負なら締切後
    PRIMARY KEY (race_id, bet_type, combination, snapshot_at)
);

CREATE INDEX IF NOT EXISTS idx_odds_race ON odds_snapshots (race_id, bet_type);
CREATE INDEX IF NOT EXISTS idx_odds_close ON odds_snapshots (race_id, secs_to_close);

-- 「判断に使うオッズ」と「精算に使うオッズ」は別物。混同すると ROI を誤る。
--
--   decision_odds … 締切前に自分が実際に見られた最後のオッズ。
--                   モデルの市場特徴量・購入判断はこちらを使う。
--                   締切後のスナップショットを混ぜると未来を見たことになる。
--
--   final_odds    … 確定オッズ。パリミュチュエルでは払戻が最終プールで決まるため、
--                   いつ買っても受け取る価格はこれ。回収率の計算はこちらを使う。
--
-- 判断は decision_odds、精算は final_odds。これを逆にすると幻の利益が出る。

CREATE OR REPLACE VIEW decision_odds AS
SELECT race_id, bet_type, combination, snapshot_at, odds_low, odds_high,
       popularity, secs_to_close
FROM (
    SELECT *,
           ROW_NUMBER() OVER (
               PARTITION BY race_id, bet_type, combination
               ORDER BY snapshot_at DESC
           ) AS rn
    FROM odds_snapshots
    WHERE secs_to_close IS NOT NULL AND secs_to_close >= 0
) WHERE rn = 1;

-- 確定オッズを優先し、無ければ最後のスナップショットで代用する。
CREATE OR REPLACE VIEW final_odds AS
SELECT race_id, bet_type, combination, snapshot_at, official_dt, is_official,
       odds_low, odds_high, popularity, secs_to_close
FROM (
    SELECT *,
           ROW_NUMBER() OVER (
               PARTITION BY race_id, bet_type, combination
               ORDER BY is_official DESC, snapshot_at DESC
           ) AS rn
    FROM odds_snapshots
) WHERE rn = 1;

-- ---------------------------------------------------------------------------
-- 結果 (締切後に確定。特徴量に混ぜないこと)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS results (
    race_id         VARCHAR NOT NULL,
    syaban          INTEGER NOT NULL,
    finish_pos      INTEGER,               -- 着順。失格/欠車は NULL
    finish_status   VARCHAR,               -- 正常 / 失格 / 落車 / 欠車 など
    margin          VARCHAR,               -- 着差 ('2車身', '1/4車輪')
    last_lap_time   DOUBLE,                -- 上りタイム
    kimarite        VARCHAR,               -- 決まり手 (逃/捲/差/マ)
    got_s           BOOLEAN,               -- S を取ったか
    got_b           BOOLEAN,               -- B を取ったか
    updated_at      TIMESTAMP,
    PRIMARY KEY (race_id, syaban)
);

CREATE TABLE IF NOT EXISTS payouts (
    race_id         VARCHAR NOT NULL,
    bet_type        INTEGER NOT NULL,
    combination     VARCHAR NOT NULL,
    payout_yen      INTEGER NOT NULL,      -- 100円あたり払戻
    popularity      INTEGER,
    updated_at      TIMESTAMP,
    PRIMARY KEY (race_id, bet_type, combination)
);

-- ---------------------------------------------------------------------------
-- 収集メタ (何をいつ取ったか。欠損の追跡用)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS fetch_log (
    fetched_at      TIMESTAMP NOT NULL,
    api_class       VARCHAR NOT NULL,
    race_id         VARCHAR,
    ok              BOOLEAN NOT NULL,
    note            VARCHAR
);
