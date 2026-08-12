"""Phase 2-3: モデルを作り、市場との差を測る。

Phase 2 の目的は「良いモデルを作ること」ではなく、
**ベースラインが市場にどれだけ負けるかを数字で知ること**にある。
これを飛ばすと、後の改善が本物なのか過学習なのか判別できなくなる。

Phase 3 ではライン特徴量を足して、その差をどれだけ詰められたかを見る。

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

from .dataset import (
    FEATURE_NAMES,
    RATING_ONLY,
    RaceSample,
    build,
    select,
    time_split,
)

log = logging.getLogger("keirin.baseline")

rng = np.random.default_rng(0)


# ---------------------------------------------------------------------------
# 条件付きロジット
# ---------------------------------------------------------------------------


def _softmax(u: np.ndarray) -> np.ndarray:
    u = u - u.max()
    e = np.exp(u)
    return e / e.sum()


def standardize(train: list[RaceSample], test: list[RaceSample]):
    """train の統計量で標準化する。test の統計量を使うとリークする。"""
    allx = np.vstack([s.x for s in train])
    mu = allx.mean(axis=0)
    sd = allx.std(axis=0)
    sd[sd < 1e-12] = 1.0

    def apply(samples):
        return [
            RaceSample(s.race_id, s.kaisai_date, s.syaban, (s.x - mu) / sd,
                       s.winner_idx, s.p_market)
            for s in samples
        ]

    return apply(train), apply(test)


def fit_conditional_logit(samples: list[RaceSample], l2: float = 1e-2) -> np.ndarray:
    """レース内 softmax の係数を最尤推定する。"""
    k = samples[0].x.shape[1]

    def nll_and_grad(beta: np.ndarray):
        nll = 0.0
        grad = np.zeros(k)
        for s in samples:
            p = _softmax(s.x @ beta)
            nll -= np.log(max(p[s.winner_idx], 1e-300))
            grad -= s.x[s.winner_idx] - p @ s.x
        nll += l2 * beta @ beta
        grad += 2 * l2 * beta
        return nll, grad

    res = minimize(nll_and_grad, np.zeros(k), jac=True, method="L-BFGS-B",
                   options={"maxiter": 1000})
    if not res.success:
        log.warning("optimiser did not converge: %s", res.message)
    return res.x


def predict(samples: list[RaceSample], beta: np.ndarray) -> list[np.ndarray]:
    return [_softmax(s.x @ beta) for s in samples]


# ---------------------------------------------------------------------------
# 評価
# ---------------------------------------------------------------------------


def _losses(samples, probs) -> np.ndarray:
    return np.array([-np.log(max(p[s.winner_idx], 1e-300)) for s, p in zip(samples, probs)])


def log_loss(samples, probs) -> float:
    return float(_losses(samples, probs).mean())


def top1_rate(samples, probs) -> float:
    return float(np.mean([int(np.argmax(p) == s.winner_idx) for s, p in zip(samples, probs)]))


def uniform_probs(samples: list[RaceSample]) -> list[np.ndarray]:
    return [np.full(s.n, 1.0 / s.n) for s in samples]


def bootstrap_diff(samples, a, b, n_boot: int = 2000) -> tuple[float, float, float]:
    """log loss 差 (a - b) の点推定と95%信頼区間。

    点推定だけ見ると必ず自分を騙すので、区間を出してから解釈する。
    """
    d = _losses(samples, a) - _losses(samples, b)
    n = len(d)
    boots = np.array([d[rng.integers(0, n, n)].mean() for _ in range(n_boot)])
    return float(d.mean()), float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))


def fit_and_eval(samples, names, train_frac):
    """特徴量セットを1つ評価する。レース集合は呼び出し側で固定しておくこと。"""
    sub = select(samples, names)
    train, test = time_split(sub, train_frac)
    train, test = standardize(train, test)
    beta = fit_conditional_logit(train)
    probs = predict(test, beta)
    return {
        "names": names,
        "beta": beta,
        "train": train,
        "test": test,
        "probs": probs,
        "ll": log_loss(test, probs),
        "top1": top1_rate(test, probs),
    }


def report(samples, train_frac: float) -> str:
    base = fit_and_eval(samples, RATING_ONLY, train_frac)
    full = fit_and_eval(samples, FEATURE_NAMES, train_frac)

    test = base["test"]
    p_mkt = [s.p_market for s in test]
    p_unif = uniform_probs(test)

    d_base, lo_b, hi_b = bootstrap_diff(test, base["probs"], p_mkt)
    d_full, lo_f, hi_f = bootstrap_diff(test, full["probs"], p_mkt)
    d_gain, lo_g, hi_g = bootstrap_diff(test, full["probs"], base["probs"])

    out = [
        "",
        f"  学習 {len(base['train'])} レース / 検証 {len(test)} レース",
        "",
        f"  {'':22} {'log loss':>9} {'top1':>7}",
        "  " + "-" * 40,
        f"  {'一様分布':18} {log_loss(test, p_unif):9.4f} {top1_rate(test, p_unif):6.1%}",
        f"  {'競走得点のみ':16} {base['ll']:9.4f} {base['top1']:6.1%}",
        f"  {'+ライン・展開':15} {full['ll']:9.4f} {full['top1']:6.1%}",
        f"  {'市場':20} {log_loss(test, p_mkt):9.4f} {top1_rate(test, p_mkt):6.1%}",
        "",
        "  市場との差 (正なら市場に負け):",
        f"    競走得点のみ  {d_base:+.4f}  (95%CI {lo_b:+.4f} 〜 {hi_b:+.4f})",
        f"    +ライン・展開 {d_full:+.4f}  (95%CI {lo_f:+.4f} 〜 {hi_f:+.4f})",
        "",
        f"  ライン特徴量による改善 {-d_gain:+.4f}"
        f"  (95%CI {-hi_g:+.4f} 〜 {-lo_g:+.4f})",
        "",
        "  係数 (標準化後、絶対値順):",
    ]

    order = np.argsort(-np.abs(full["beta"]))
    for i in order:
        out.append(f"    {FEATURE_NAMES[i]:20} {full['beta'][i]:+.4f}")

    out.append("")
    if lo_g > 0:
        out.append("  → ライン特徴量は有意な改善になっていない。")
    elif hi_g < 0:
        out.append("  → ライン特徴量は有意に改善している。")
    else:
        out.append("  → 改善は有意でない。サンプル不足か、効いていないかのどちらか。")

    if lo_f > 0:
        out.append("  → それでもまだ市場には有意に負けている。想定どおり。")
    elif hi_f < 0:
        out.append(
            "  → 市場に有意に勝っている。まずリークを疑うこと。\n"
            "     結果由来の値が特徴量に混ざっていないか、\n"
            "     学習と検証が時系列で分かれているかを確認する。"
        )

    if len(test) < 500:
        out += [
            "",
            f"  ※ 検証が {len(test)} レースしかない。この数字はまだ信用しないこと。",
            "     判断には最低でも数千レース規模のバックフィルが要る。",
        ]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="keirin.baseline", description=__doc__)
    p.add_argument("--db", type=Path, default=Path("data/keirin.duckdb"))
    p.add_argument("--train-frac", type=float, default=0.7)
    p.add_argument(
        "--no-lines", action="store_true",
        help="ライン情報が無いレースも使う (競走得点のみの評価になる)",
    )
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")

    con = duckdb.connect(str(args.db), read_only=True)
    samples = build(con, require_lines=not args.no_lines)
    if len(samples) < 20:
        log.error("only %d usable races -- backfill more data first", len(samples))
        return 1

    train, test = time_split(samples, args.train_frac)
    if not train or not test:
        log.error("time split produced an empty side (need at least 2 race days)")
        return 1

    print(report(samples, args.train_frac))
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
