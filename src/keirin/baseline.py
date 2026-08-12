"""Phase 2: ベースラインを作り、市場に負けることを確認する。

この工程の目的は「良いモデルを作ること」ではない。
**ベースラインが市場にどれだけ負けるかを数字で知ること**にある。
これを飛ばすと、後の改善が本物なのか過学習なのか判別できなくなる。

モデルは条件付きロジット。競輪はレース内で1人だけが勝つので、
独立な二値分類は理論的に誤り(レース内の確率合計が1にならない)。

    P(i が1着) = exp(x_i·β) / Σ_j exp(x_j·β)

使い方:
    python -m keirin.baseline --db data/keirin.duckdb
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import duckdb
import numpy as np
from scipy.optimize import minimize

from .dataset import RaceSample, build, time_split

log = logging.getLogger("keirin.baseline")

rng = np.random.default_rng(0)


# ---------------------------------------------------------------------------
# 条件付きロジット
# ---------------------------------------------------------------------------


def _softmax(u: np.ndarray) -> np.ndarray:
    u = u - u.max()
    e = np.exp(u)
    return e / e.sum()


def fit_conditional_logit(
    samples: list[RaceSample], l2: float = 1e-3
) -> np.ndarray:
    """レース内 softmax の係数を最尤推定する。"""
    k = samples[0].x.shape[1]

    def nll_and_grad(beta: np.ndarray):
        nll = 0.0
        grad = np.zeros(k)
        for s in samples:
            u = s.x @ beta
            p = _softmax(u)
            nll -= np.log(max(p[s.winner_idx], 1e-300))
            grad -= s.x[s.winner_idx] - p @ s.x
        nll += l2 * beta @ beta
        grad += 2 * l2 * beta
        return nll, grad

    res = minimize(
        nll_and_grad, np.zeros(k), jac=True, method="L-BFGS-B",
        options={"maxiter": 500},
    )
    if not res.success:
        log.warning("optimiser did not converge: %s", res.message)
    return res.x


def predict(samples: list[RaceSample], beta: np.ndarray) -> list[np.ndarray]:
    return [_softmax(s.x @ beta) for s in samples]


# ---------------------------------------------------------------------------
# 評価
# ---------------------------------------------------------------------------


def log_loss(samples: list[RaceSample], probs: list[np.ndarray]) -> float:
    """レースごとの -log p(実際の1着)。低いほど良い。"""
    return float(
        np.mean([-np.log(max(p[s.winner_idx], 1e-300)) for s, p in zip(samples, probs)])
    )


def top1_rate(samples: list[RaceSample], probs: list[np.ndarray]) -> float:
    return float(np.mean([int(np.argmax(p) == s.winner_idx) for s, p in zip(samples, probs)]))


def uniform_probs(samples: list[RaceSample]) -> list[np.ndarray]:
    return [np.full(s.n, 1.0 / s.n) for s in samples]


def bootstrap_diff(
    samples: list[RaceSample],
    a: list[np.ndarray],
    b: list[np.ndarray],
    n_boot: int = 2000,
) -> tuple[float, float, float]:
    """log loss 差 (a - b) の点推定と95%信頼区間。

    回収率も log loss も分散が大きい。点推定だけ見ると必ず自分を騙すので、
    区間を出してから解釈する。
    """
    la = np.array([-np.log(max(p[s.winner_idx], 1e-300)) for s, p in zip(samples, a)])
    lb = np.array([-np.log(max(p[s.winner_idx], 1e-300)) for s, p in zip(samples, b)])
    d = la - lb
    n = len(d)
    boots = np.array([d[rng.integers(0, n, n)].mean() for _ in range(n_boot)])
    return float(d.mean()), float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))


def evaluate(train: list[RaceSample], test: list[RaceSample]) -> dict:
    beta = fit_conditional_logit(train)
    p_base = predict(test, beta)
    p_unif = uniform_probs(test)
    p_mkt = [s.p_market for s in test]

    diff, lo, hi = bootstrap_diff(test, p_base, p_mkt)
    return {
        "beta": beta,
        "n_train": len(train),
        "n_test": len(test),
        "ll_uniform": log_loss(test, p_unif),
        "ll_baseline": log_loss(test, p_base),
        "ll_market": log_loss(test, p_mkt),
        "top1_uniform": top1_rate(test, p_unif),
        "top1_baseline": top1_rate(test, p_base),
        "top1_market": top1_rate(test, p_mkt),
        "diff": diff,
        "diff_lo": lo,
        "diff_hi": hi,
    }


def report(r: dict) -> str:
    lines = [
        "",
        f"  学習 {r['n_train']} レース / 検証 {r['n_test']} レース",
        f"  競走得点の係数 β = {r['beta'][0]:+.4f}",
        "",
        "  " + f"{'':10} {'log loss':>10} {'top1':>8}",
        "  " + "-" * 30,
        f"  {'一様':10} {r['ll_uniform']:10.4f} {r['top1_uniform']:7.1%}",
        f"  {'ベースライン':6} {r['ll_baseline']:10.4f} {r['top1_baseline']:7.1%}",
        f"  {'市場':10} {r['ll_market']:10.4f} {r['top1_market']:7.1%}",
        "",
        f"  ベースライン − 市場 = {r['diff']:+.4f}"
        f"  (95%CI {r['diff_lo']:+.4f} 〜 {r['diff_hi']:+.4f})",
        "",
    ]

    if r["diff_lo"] > 0:
        lines += [
            "  → ベースラインは市場に有意に負けている。想定どおり。",
            "     ここが出発点。特徴量を足してこの差を詰めていく。",
        ]
    elif r["diff_hi"] < 0:
        lines += [
            "  → ベースラインが市場に有意に勝っている。",
            "     競走得点だけで市場超えは考えにくいので、まずリークを疑うこと。",
            "     結果由来の値が特徴量に混ざっていないか、",
            "     学習と検証が時系列で分かれているかを確認する。",
        ]
    else:
        lines += [
            "  → 有意差なし。サンプル不足で判定できていない可能性が高い。",
            "     レース数を増やしてから再評価すること。",
        ]

    if r["n_test"] < 500:
        lines += [
            "",
            f"  ※ 検証が {r['n_test']} レースしかない。この数字はまだ信用しないこと。",
            "     判断には最低でも数千レース規模のバックフィルが要る。",
        ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="keirin.baseline", description=__doc__)
    p.add_argument("--db", type=Path, default=Path("data/keirin.duckdb"))
    p.add_argument("--train-frac", type=float, default=0.7)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")

    con = duckdb.connect(str(args.db), read_only=True)
    samples = build(con)
    if len(samples) < 20:
        log.error("only %d usable races -- backfill more data first", len(samples))
        return 1

    train, test = time_split(samples, args.train_frac)
    if not train or not test:
        log.error("time split produced an empty side (need at least 2 race days)")
        return 1

    print(report(evaluate(train, test)))
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
