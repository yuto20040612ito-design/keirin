"""Phase 4: キャリブレーションと期待値ベースのバックテスト。

モデルの log loss がいくら良くても、それだけでは儲からない。
儲かるかどうかを決めるのは**賭け方**であり、そのために必要なのは
「順位を当てる力」ではなく「確率が正しいこと」。

    EV = p × オッズ − 1 > 閾値 のときだけ買う

p が過大評価されていると、EVが正に見えるだけの馬券を買い続けて破滅する。
だから確率の較正(キャリブレーション)を先に済ませ、閾値も保守的に取る。

## 3分割する理由

    train  … モデルの学習
    calib  … 確率の較正
    test   … 評価

学習に使ったデータで較正すると、モデルが既に当てにいっているぶん
「よく較正されている」ように見えてしまう。必ず別のデータで較正する。
分割はすべて開催日の境界で、時系列順に切る。

## 車券の確率をどう作るか

競輪の賭式は組み合わせなので、1着確率から順序対の確率を作る必要がある。
Harville 近似を使う:

    P(i→j)   = p_i × p_j/(1−p_i)
    P(i→j→k) = p_i × p_j/(1−p_i) × p_k/(1−p_i−p_j)

これは「1着が抜けた後も残りの相対的な強さは変わらない」という仮定。
実際には2着以降で人気薄が過小評価される既知のバイアスがあるので、
ここは後で実測ベースの補正に差し替える余地がある。

使い方:
    python -m keirin.backtest --db data/keirin.duckdb
"""

from __future__ import annotations

import argparse
import logging
from itertools import permutations
from pathlib import Path

import duckdb
import numpy as np

from .baseline import fit_conditional_logit, predict, standardize
from .dataset import FEATURE_NAMES, build, select

log = logging.getLogger("keirin.backtest")

rng = np.random.default_rng(0)

EXACTA = 6      # 2車単
TRIFECTA = 9    # 3連単

BET_NAMES = {EXACTA: "2車単", TRIFECTA: "3連単"}


# ---------------------------------------------------------------------------
# キャリブレーション
# ---------------------------------------------------------------------------


def fit_calibration(probs: list[np.ndarray], samples) -> float:
    """べき変換 p^a を1パラメータで当てはめる。

    a > 1 なら「モデルは自信過剰」で確率を鋭くしすぎ、a < 1 なら自信不足。
    レース内で再正規化するので、順位は変わらず確率の鋭さだけが変わる。

    isotonic のような自由度の高い較正はレース内正規化と相性が悪く、
    サンプルが少ないと暴れる。まずは単調な1パラメータで足りる。
    """
    grid = np.linspace(0.3, 2.5, 89)
    best_a, best_ll = 1.0, np.inf
    for a in grid:
        ll = 0.0
        for p, s in zip(probs, samples):
            q = p**a
            q = q / q.sum()
            ll -= np.log(max(q[s.winner_idx], 1e-300))
        if ll < best_ll:
            best_a, best_ll = float(a), ll
    return best_a


def apply_calibration(probs: list[np.ndarray], a: float) -> list[np.ndarray]:
    out = []
    for p in probs:
        q = p**a
        out.append(q / q.sum())
    return out


def reliability(probs: list[np.ndarray], samples, n_bins: int = 8) -> list[tuple]:
    """予測確率帯ごとの実際の勝率。ここがずれていると期待値計算が全部狂う。"""
    flat_p, flat_y = [], []
    for p, s in zip(probs, samples):
        for i, pi in enumerate(p):
            flat_p.append(pi)
            flat_y.append(1 if i == s.winner_idx else 0)
    flat_p = np.array(flat_p)
    flat_y = np.array(flat_y)
    edges = np.quantile(flat_p, np.linspace(0, 1, n_bins + 1))
    out = []
    for lo, hi in zip(edges, edges[1:]):
        m = (flat_p >= lo) & (flat_p <= hi)
        if m.sum() >= 20:
            out.append((float(flat_p[m].mean()), float(flat_y[m].mean()), int(m.sum())))
    return out


# ---------------------------------------------------------------------------
# 車券の確率 (Harville)
# ---------------------------------------------------------------------------


def exacta_probs(p: np.ndarray, syaban: np.ndarray) -> dict[str, float]:
    out = {}
    for i, j in permutations(range(len(p)), 2):
        denom = 1.0 - p[i]
        if denom <= 1e-12:
            continue
        out[f"{syaban[i]:02d}{syaban[j]:02d}"] = p[i] * p[j] / denom
    return out


def trifecta_probs(p: np.ndarray, syaban: np.ndarray) -> dict[str, float]:
    out = {}
    for i, j, k in permutations(range(len(p)), 3):
        d1 = 1.0 - p[i]
        d2 = 1.0 - p[i] - p[j]
        if d1 <= 1e-12 or d2 <= 1e-12:
            continue
        out[f"{syaban[i]:02d}{syaban[j]:02d}{syaban[k]:02d}"] = (
            p[i] * (p[j] / d1) * (p[k] / d2)
        )
    return out


# ---------------------------------------------------------------------------
# バックテスト
# ---------------------------------------------------------------------------


ODDS_SQL = """
SELECT race_id, bet_type, combination, odds_low
FROM final_odds
WHERE bet_type IN (?, ?) AND odds_low IS NOT NULL AND odds_low > 0
"""

PAYOUT_SQL = """
SELECT race_id, bet_type, combination, payout_yen
FROM payouts WHERE bet_type IN (?, ?)
"""


def load_market(con):
    odds: dict[tuple[str, int], dict[str, float]] = {}
    for race_id, bet_type, combo, o in con.execute(
        ODDS_SQL, [EXACTA, TRIFECTA]
    ).fetchall():
        odds.setdefault((race_id, int(bet_type)), {})[combo] = float(o)

    payouts: dict[tuple[str, int], dict[str, int]] = {}
    for race_id, bet_type, combo, yen in con.execute(
        PAYOUT_SQL, [EXACTA, TRIFECTA]
    ).fetchall():
        payouts.setdefault((race_id, int(bet_type)), {})[combo] = int(yen)
    return odds, payouts


def simulate(samples, probs, odds, payouts, bet_type: int, threshold: float):
    """EVが閾値を超える組だけ100円ずつ買う。

    100円均等なのは解釈しやすいから。ケリーは資金曲線の話であって、
    エッジが有るか無いかの判定には均等買いのほうが素直に出る。
    """
    per_race = []
    for s, p in zip(samples, probs):
        key = (s.race_id, bet_type)
        market = odds.get(key)
        if not market:
            continue
        model = (exacta_probs if bet_type == EXACTA else trifecta_probs)(p, s.syaban)
        paid = payouts.get(key, {})

        staked = returned = 0
        n_bets = 0
        for combo, o in market.items():
            q = model.get(combo)
            if q is None:
                continue
            if q * o - 1.0 <= threshold:
                continue
            staked += 100
            returned += paid.get(combo, 0)
            n_bets += 1
        if staked:
            per_race.append((staked, returned, n_bets))
    return per_race


def roi_with_ci(per_race, n_boot: int = 2000):
    if not per_race:
        return None
    arr = np.array(per_race, dtype=float)
    staked, returned = arr[:, 0], arr[:, 1]
    roi = returned.sum() / staked.sum()
    n = len(arr)
    boots = []
    for _ in range(n_boot):
        idx = rng.integers(0, n, n)
        s = staked[idx].sum()
        if s > 0:
            boots.append(returned[idx].sum() / s)
    lo, hi = np.percentile(boots, [2.5, 97.5])
    return {
        "roi": float(roi),
        "lo": float(lo),
        "hi": float(hi),
        "races": n,
        "bets": int(arr[:, 2].sum()),
        "staked": int(staked.sum()),
        "returned": int(returned.sum()),
    }


def three_way_split(samples, train_frac=0.6, calib_frac=0.2):
    """開催日の境界で train / calib / test に切る。"""
    dates = sorted({s.kaisai_date for s in samples})
    i1 = max(1, int(len(dates) * train_frac))
    i2 = max(i1 + 1, int(len(dates) * (train_frac + calib_frac)))
    c1 = dates[min(i1, len(dates) - 1)]
    c2 = dates[min(i2, len(dates) - 1)]
    train = [s for s in samples if s.kaisai_date < c1]
    calib = [s for s in samples if c1 <= s.kaisai_date < c2]
    test = [s for s in samples if s.kaisai_date >= c2]
    return train, calib, test


def run(con, thresholds, train_frac, calib_frac) -> str:
    samples = build(con)
    train, calib, test = three_way_split(samples, train_frac, calib_frac)
    if not (train and calib and test):
        return "  データが足りない。最低3開催日ぶんは要る。"

    train = select(train, FEATURE_NAMES)
    calib = select(calib, FEATURE_NAMES)
    test = select(test, FEATURE_NAMES)

    train_s, rest = standardize(train, calib + test)
    calib_s, test_s = rest[: len(calib)], rest[len(calib):]

    beta = fit_conditional_logit(train_s)
    a = fit_calibration(predict(calib_s, beta), calib_s)
    p_test = apply_calibration(predict(test_s, beta), a)

    odds, payouts = load_market(con)

    out = [
        "",
        f"  学習 {len(train)} / 較正 {len(calib)} / 検証 {len(test)} レース",
        f"  較正パラメータ a = {a:.3f}"
        + ("  (自信過剰ぎみ)" if a > 1.05 else "  (自信不足ぎみ)" if a < 0.95 else "  (ほぼ較正済み)"),
        "",
        "  較正の確認 (予測確率 vs 実際の勝率):",
        f"    {'予測':>8} {'実際':>8} {'件数':>7}",
    ]
    for pred, actual, n in reliability(p_test, test_s):
        out.append(f"    {pred:8.3f} {actual:8.3f} {n:7d}")

    out += ["", "  期待値ベースの購入 (100円均等):"]
    for bet_type in (EXACTA, TRIFECTA):
        out.append(f"    {BET_NAMES[bet_type]}")
        out.append(
            f"      {'EV閾値':>7} {'対象R':>6} {'点数':>7} {'投資':>9} {'回収':>9}"
            f" {'回収率':>8} {'95%CI':>18}"
        )
        for th in thresholds:
            r = roi_with_ci(simulate(test_s, p_test, odds, payouts, bet_type, th))
            if r is None:
                out.append(f"      {th:7.2f}  (該当なし)")
                continue
            out.append(
                f"      {th:7.2f} {r['races']:6d} {r['bets']:7d} {r['staked']:9d}"
                f" {r['returned']:9d} {r['roi']:8.1%}"
                f"  {r['lo']:6.1%} 〜 {r['hi']:6.1%}"
            )

    out += [
        "",
        "  読み方と、この数字の限界:",
        "  * 控除率25%なので、無選別に買えば回収率は約75%に収束する。",
        "    それを超えていない閾値帯は、単に負けているだけ。",
        "  * 95%CIの下限が100%を超えていなければ、エッジがあるとは言えない。",
        "  * バックフィルには締切前オッズが無いため、確定オッズで購入判断している。",
        "    実際には締切前の価格しか見られないので、この回収率は楽観側に偏る。",
        "    watch で締切前オッズが貯まったら decision_odds に切り替えること。",
        "  * パリミュチュエルなので自分の投票でオッズは下がる。ここでは未考慮。",
        "    薄い市場(人気薄の目、ミッドナイト)ほど影響が大きい。",
        "  * Harville 近似は2着以降で歪むことが知られている。",
    ]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="keirin.backtest", description=__doc__)
    p.add_argument("--db", type=Path, default=Path("data/keirin.duckdb"))
    p.add_argument("--train-frac", type=float, default=0.6)
    p.add_argument("--calib-frac", type=float, default=0.2)
    p.add_argument(
        "--thresholds", default="0.0,0.1,0.2,0.3,0.5",
        help="EV閾値をカンマ区切りで",
    )
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")

    thresholds = [float(t) for t in args.thresholds.split(",") if t.strip()]
    con = duckdb.connect(str(args.db), read_only=True)
    print(run(con, thresholds, args.train_frac, args.calib_frac))
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
