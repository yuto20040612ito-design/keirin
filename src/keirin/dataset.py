"""DuckDB から学習用データセットを組み立てる。

競輪には単勝が無いので、市場の勝率は直接は手に入らない。
2車単は (1着, 2着) の順序対を全通り覆っているので、2着側で周辺化すれば
1着確率が得られる:

    P(i が1着) = Σ_{j≠i} P(i→j)

各組のインプライド確率は 1/オッズ を全組で正規化したもの。
正規化によって控除率(約25%)が取り除かれ、市場の素の予想確率になる。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

log = logging.getLogger("keirin.dataset")

# 2車単。(1着,2着) の順序対を全通り覆うので周辺化に使える。
EXACTA = 6


@dataclass
class RaceSample:
    race_id: str
    kaisai_date: str
    syaban: np.ndarray       # (n,) 車番
    x: np.ndarray            # (n, k) 特徴量
    winner_idx: int          # x の行インデックス
    p_market: np.ndarray | None  # (n,) 市場のインプライド1着確率。欠損時 None

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
SELECT e.race_id, r.kaisai_date, e.syaban, e.rating,
       CASE WHEN res.finish_pos = 1 THEN 1 ELSE 0 END AS won
FROM entries e
JOIN races r   ON r.race_id = e.race_id
JOIN results res ON res.race_id = e.race_id AND res.syaban = e.syaban
WHERE e.rating IS NOT NULL
ORDER BY r.kaisai_date, e.race_id, e.syaban
"""


def build(con, require_market: bool = True) -> list[RaceSample]:
    """レース単位のサンプル列を作る。時系列順に並ぶ。

    require_market=True のとき、市場確率が揃っていないレースは落とす。
    ベースラインと市場を同じ土俵で比べるため、比較対象は揃えておく必要がある。
    """
    rows = con.execute(BASE_SQL).fetchall()
    market_rows = con.execute(MARKET_SQL).fetchall()

    market: dict[str, dict[int, float]] = {}
    combos: dict[str, int] = {}
    for race_id, syaban, p, n_combos in market_rows:
        market.setdefault(race_id, {})[int(syaban)] = float(p)
        combos[race_id] = int(n_combos)

    by_race: dict[str, list[tuple]] = {}
    dates: dict[str, str] = {}
    for race_id, kaisai_date, syaban, rating, won in rows:
        by_race.setdefault(race_id, []).append((int(syaban), float(rating), int(won)))
        dates[race_id] = str(kaisai_date)

    samples: list[RaceSample] = []
    dropped = {"no_winner": 0, "no_market": 0, "incomplete_market": 0, "too_few": 0}

    for race_id, entries in by_race.items():
        entries.sort()
        syaban = np.array([e[0] for e in entries], dtype=int)
        rating = np.array([e[1] for e in entries], dtype=float)
        won = np.array([e[2] for e in entries], dtype=int)
        n = len(syaban)

        if n < 2:
            dropped["too_few"] += 1
            continue
        if won.sum() != 1:
            # 1着不在(全員失格など)や同着。ここでは扱わない。
            dropped["no_winner"] += 1
            continue

        p_market = None
        mk = market.get(race_id)
        if mk is not None:
            # 2車単は n(n-1) 組で全通り。欠けていたら周辺化が歪むので使わない。
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
                x=rating.reshape(-1, 1),
                winner_idx=int(np.argmax(won)),
                p_market=p_market,
            )
        )

    samples.sort(key=lambda s: (s.kaisai_date, s.race_id))
    log.info(
        "built %d races (dropped: %s)",
        len(samples),
        ", ".join(f"{k}={v}" for k, v in dropped.items() if v),
    )
    return samples


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
