"""DuckDB から学習用データセットを組み立てる。

## 市場の勝率

競輪には単勝が無いので、市場の1着確率は直接は手に入らない。
2車単は (1着, 2着) の順序対を全通り覆っているので、2着側で周辺化すれば
1着確率が得られる:

    P(i が1着) = Σ_{j≠i} P(i→j)

各組のインプライド確率は 1/オッズ を全組で正規化したもの。
正規化によって控除率(約25%)が取り除かれ、市場の素の予想確率になる。

## 特徴量

条件付きロジットはレース内 softmax なので、**レース内で一定の特徴量は効かない**
(全員に同じ値を足しても確率が変わらない)。出走数やライン数をそのまま入れても
無意味で、必ず選手ごとに差がつく形にするか、交互作用にする必要がある。

ライン内位置は競輪では脚質の代理変数になる。先頭は先行、番手以降は追込。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

log = logging.getLogger("keirin.dataset")

# 2車単。(1着,2着) の順序対を全通り覆うので周辺化に使える。
EXACTA = 6

FEATURE_NAMES = [
    "rating",             # 競走得点
    "rating_rank",        # レース内順位 (0=最上位 .. 1=最下位)
    "rating_gap_top",     # トップとの得点差
    "is_s_class",         # S級か
    "syaban",             # 車番 (内が有利とされる)
    "line_size",          # 所属ラインの人数
    "line_pos",           # ライン内位置 (1=先頭)
    "is_line_head",       # ライン先頭 ≒ 先行役
    "is_solo",            # 単騎
    "line_rating_mean",   # 自ライン全体の強さ
    "line_head_rating",   # 自ラインの先頭の強さ (番手なら誰の後ろか)
    "head_x_nlines",      # 先行役 × ライン数。先行争いの激しさが先行役に効く分
]

# ライン情報が要らない特徴量。Phase 2 のベースラインはこれだけで作る。
RATING_ONLY = ["rating"]


@dataclass
class RaceSample:
    race_id: str
    kaisai_date: str
    syaban: np.ndarray           # (n,)
    x: np.ndarray                # (n, k) FEATURE_NAMES の順
    winner_idx: int
    p_market: np.ndarray | None  # (n,)

    @property
    def n(self) -> int:
        return len(self.syaban)


MARKET_SQL = f"""
WITH q AS (
    SELECT race_id,
           CAST(substr(combination, 1, 2) AS INTEGER) AS syaban,
           1.0 / odds_low AS inv
    FROM final_odds
    WHERE bet_type = {EXACTA} AND odds_low IS NOT NULL AND odds_low > 0
),
tot AS (SELECT race_id, sum(inv) AS s, count(*) AS n_combos FROM q GROUP BY 1)
SELECT q.race_id, q.syaban,
       sum(q.inv) / max(tot.s) AS p_market,
       max(tot.n_combos)       AS n_combos
FROM q JOIN tot USING (race_id)
GROUP BY 1, 2
"""

BASE_SQL = """
SELECT e.race_id, r.kaisai_date, e.syaban, e.rating, e.kyu,
       CASE WHEN res.finish_pos = 1 THEN 1 ELSE 0 END AS won
FROM entries e
JOIN races r     ON r.race_id = e.race_id
JOIN results res ON res.race_id = e.race_id AND res.syaban = e.syaban
WHERE e.rating IS NOT NULL
ORDER BY r.kaisai_date, e.race_id, e.syaban
"""

LINE_SQL = """
SELECT race_id, syaban, line_no, position, line_size, is_solo
FROM race_lines
WHERE source = 'AplNarabiYoso'
"""


def _fetch_market(con):
    market: dict[str, dict[int, float]] = {}
    combos: dict[str, int] = {}
    for race_id, syaban, p, n_combos in con.execute(MARKET_SQL).fetchall():
        market.setdefault(race_id, {})[int(syaban)] = float(p)
        combos[race_id] = int(n_combos)
    return market, combos


def _fetch_lines(con):
    lines: dict[str, dict[int, tuple]] = {}
    try:
        rows = con.execute(LINE_SQL).fetchall()
    except Exception:  # テーブルが無い/空でも動くようにしておく
        return lines
    for race_id, syaban, line_no, position, line_size, is_solo in rows:
        lines.setdefault(race_id, {})[int(syaban)] = (
            int(line_no), int(position), int(line_size), bool(is_solo)
        )
    return lines


def _line_features(syaban, rating, line_map) -> np.ndarray | None:
    """ライン由来の特徴量。全員ぶん揃っていなければ None。"""
    if not line_map or any(s not in line_map for s in syaban):
        return None

    line_no = np.array([line_map[s][0] for s in syaban])
    line_pos = np.array([line_map[s][1] for s in syaban], dtype=float)
    line_size = np.array([line_map[s][2] for s in syaban], dtype=float)
    is_solo = np.array([1.0 if line_map[s][3] else 0.0 for s in syaban])
    is_head = (line_pos == 1).astype(float)
    n_lines = float(len(set(line_no.tolist())))

    line_rating_mean = np.zeros(len(syaban))
    line_head_rating = np.zeros(len(syaban))
    for ln in set(line_no.tolist()):
        mask = line_no == ln
        line_rating_mean[mask] = rating[mask].mean()
        head = mask & (line_pos == 1)
        line_head_rating[mask] = rating[head][0] if head.any() else rating[mask].max()

    return np.column_stack([
        line_size, line_pos, is_head, is_solo,
        line_rating_mean, line_head_rating, is_head * n_lines,
    ])


def build(con, require_market: bool = True, require_lines: bool = True) -> list[RaceSample]:
    """レース単位のサンプル列を作る。時系列順に並ぶ。

    require_lines=True のとき、ライン情報が揃わないレースは落とす。
    特徴量セットを変えて比較するときに、比較対象のレースが変わってしまうと
    改善したのかレースが変わっただけなのか判別できなくなるため。
    """
    market, combos = _fetch_market(con)
    lines = _fetch_lines(con)

    by_race: dict[str, list[tuple]] = {}
    dates: dict[str, str] = {}
    for race_id, kaisai_date, syaban, rating, kyu, won in con.execute(BASE_SQL).fetchall():
        by_race.setdefault(race_id, []).append(
            (int(syaban), float(rating), str(kyu or ""), int(won))
        )
        dates[race_id] = str(kaisai_date)

    samples: list[RaceSample] = []
    dropped = {
        "no_winner": 0, "no_market": 0, "incomplete_market": 0,
        "no_lines": 0, "too_few": 0,
    }

    for race_id, entries in by_race.items():
        entries.sort()
        syaban = np.array([e[0] for e in entries], dtype=int)
        rating = np.array([e[1] for e in entries], dtype=float)
        is_s = np.array([1.0 if e[2].strip() in ("Ｓ", "S") else 0.0 for e in entries])
        won = np.array([e[3] for e in entries], dtype=int)
        n = len(syaban)

        if n < 2:
            dropped["too_few"] += 1
            continue
        if won.sum() != 1:
            dropped["no_winner"] += 1
            continue

        # 順位は 0(最上位)〜1(最下位) に正規化する。出走数が 7/9 で混ざるため。
        order = np.argsort(-rating)
        rank = np.empty(n, dtype=float)
        rank[order] = np.arange(n) / max(n - 1, 1)
        gap_top = rating.max() - rating

        line_x = _line_features(syaban, rating, lines.get(race_id))
        if line_x is None:
            if require_lines:
                dropped["no_lines"] += 1
                continue
            line_x = np.zeros((n, 7))

        x = np.column_stack([rating, rank, gap_top, is_s, syaban.astype(float), line_x])
        assert x.shape[1] == len(FEATURE_NAMES), (x.shape, len(FEATURE_NAMES))

        p_market = None
        mk = market.get(race_id)
        if mk is not None:
            if combos.get(race_id) != n * (n - 1):
                dropped["incomplete_market"] += 1
            elif all(s in mk for s in syaban):
                p_market = np.array([mk[s] for s in syaban], dtype=float)
                p_market = p_market / p_market.sum()
            else:
                dropped["incomplete_market"] += 1

        if require_market and p_market is None:
            if mk is None:
                dropped["no_market"] += 1
            continue

        samples.append(
            RaceSample(
                race_id=race_id,
                kaisai_date=dates[race_id],
                syaban=syaban,
                x=x,
                winner_idx=int(np.argmax(won)),
                p_market=p_market,
            )
        )

    samples.sort(key=lambda s: (s.kaisai_date, s.race_id))
    log.info(
        "built %d races (dropped: %s)",
        len(samples),
        ", ".join(f"{k}={v}" for k, v in dropped.items() if v) or "none",
    )
    return samples


def select(samples: list[RaceSample], names: list[str]) -> list[RaceSample]:
    """特徴量を名前で絞った複製を返す。レース集合は変えない。"""
    idx = [FEATURE_NAMES.index(nm) for nm in names]
    return [
        RaceSample(s.race_id, s.kaisai_date, s.syaban, s.x[:, idx], s.winner_idx, s.p_market)
        for s in samples
    ]


def time_split(samples: list[RaceSample], train_frac: float = 0.7):
    """時系列split。ランダムsplitは即リークするので使わない。

    同一開催日が train と test にまたがらないよう、日付の境界で切る。
    """
    if not samples:
        return [], []
    dates = sorted({s.kaisai_date for s in samples})
    cut_idx = max(1, int(len(dates) * train_frac))
    cut = dates[min(cut_idx, len(dates) - 1)]
    train = [s for s in samples if s.kaisai_date < cut]
    test = [s for s in samples if s.kaisai_date >= cut]
    return train, test
