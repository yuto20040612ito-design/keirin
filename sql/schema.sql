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

-- 競輪場マスタ。バンク諸元は keirin.jp の場データ(robots.txt 許可済)から補完する。
CREATE TABLE IF NOT EXISTS velodromes (
    jyo_cd          VARCHAR PRIMARY KEY,   -- 場コード 2桁 ('21' = 弥彦)
    jyo_name        VARCHAR NOT NULL,
    bank_length_m   DOUBLE,                -- バンク周長 333 / 400 / 500
    straight_m      DOUBLE,                -- みなし直線距離
    bank_angle_deg  DOUBLE,                -- カント(最大カント角)
    prefecture      VARCHAR,
    updated_at      TIMESTAMP
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

-- 締切「前」の最終オッズ(＝実際に買えた価格)。バックテストは必ずこれを使う。
-- secs_to_close >= 0 に限定しているのは、締切後に付いた確定オッズでは買えないため。
-- ここを緩めると、実際には買えない価格で幻の利益が出る。
CREATE OR REPLACE VIEW final_odds AS
SELECT race_id, bet_type, combination, snapshot_at, official_dt, is_official,
       odds_low, odds_high, popularity, secs_to_close
FROM (
    SELECT *,
           ROW_NUMBER() OVER (
               PARTITION BY race_id, bet_type, combination
               ORDER BY snapshot_at DESC
           ) AS rn
    FROM odds_snapshots
    WHERE secs_to_close IS NOT NULL AND secs_to_close >= 0
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
