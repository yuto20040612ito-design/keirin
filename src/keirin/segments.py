"""市場が構造的に歪む場所を探す。

自前の特徴量には市場を超える情報が無かった(keirin.baseline のエッジ検定)。
残る道は「市場そのものが特定の場所で歪んでいないか」を探すこと。

## 何を測るか

控除率が一律なら、**どのオッズ帯を全部買っても回収率は約75%になるはず**。
帯によって75%から外れるなら、そこに系統的な歪みがある。

公営競技で繰り返し報告されているのは favorite-longshot bias
（人気薄が過剰に買われ、堅い目が過小評価される）。
これが競輪にもあるなら、堅い帯の回収率が75%より高く出る。

## 多重比較の罠

**これがこの分析でいちばん危険なところ。**

帯を20個作って95%信頼区間で検定すれば、歪みが1つも無くても
期待値1個は「有意」に見える。見つけた歪みを後から理由づけするのは簡単なので、
必ず「いくつ検定したか」を数えて補正する。

ここでは Bonferroni 補正（有意水準を検定数で割る）を併記する。
保守的すぎる補正だが、自分を騙さない方向に間違えるほうがよい。

使い方:
    python -m keirin.segments --db data/keirin.duckdb
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import duckdb
import numpy as np

from .baseline import _pad

log = logging.getLogger("keirin.segments")

rng = np.random.default_rng(0)

EXACTA = 6
TRIFECTA = 9
BET_NAMES = {EXACTA: "2車単", TRIFECTA: "3連単"}

# 控除率25% ⇒ 無選別に買えば回収率はここに収束する
NO_EDGE_ROI = 0.75

# オッズ帯。競輪の2車単は数倍〜数百倍まで開く。
ODDS_BANDS = [
    (1.0, 5.0), (5.0, 10.0), (10.0, 20.0), (20.0, 50.0),
    (50.0, 100.0), (100.0, 500.0), (500.0, 1e9),
]


BETS_SQL = """
SELECT o.race_id,
       o.bet_type,
       o.odds_low                       AS odds,
       coalesce(p.payout_yen, 0)        AS payout,
       r.tosu,
       coalesce(r.is_midnight, FALSE)   AS is_midnight,
       v.bank_length_m
FROM final_odds o
JOIN races r ON r.race_id = o.race_id
LEFT JOIN velodromes v ON v.jyo_cd = r.jyo_cd
LEFT JOIN payouts p
       ON p.race_id = o.race_id AND p.bet_type = o.bet_type
      AND p.combination = o.combination
WHERE o.bet_type = ? AND o.odds_low IS NOT NULL AND o.odds_low > 0
  -- 払戻が存在するレースだけ。未確定レースを混ぜると回収率が下振れする。
  AND EXISTS (SELECT 1 FROM payouts q
              WHERE q.race_id = o.race_id AND q.bet_type = o.bet_type)
"""


def _roi(per_race: list[tuple], n_boot: int = 2000):
    """レース単位でブートストラップする。

    1レース内では必ず1点だけ的中するので、同一レースの結果は強く相関している。
    賭け単位で resample すると信頼区間が不当に狭くなる。
    """
    if not per_race:
        return None
    arr = np.array(per_race, dtype=float)
    staked, returned = arr[:, 0], arr[:, 1]
    if staked.sum() <= 0:
        return None
    n = len(arr)
    boots = []
    for _ in range(n_boot):
        idx = rng.integers(0, n, n)
        s = staked[idx].sum()
        if s > 0:
            boots.append(returned[idx].sum() / s)
    lo, hi = np.percentile(boots, [2.5, 97.5])
    return {
        "roi": float(returned.sum() / staked.sum()),
        "lo": float(lo),
        "hi": float(hi),
        "races": n,
        "bets": int(arr[:, 2].sum()),
    }


def _aggregate(rows, key_fn):
    """(セグメント -> レース -> (投資, 回収, 点数)) に畳む。"""
    acc: dict = {}
    for row in rows:
        key = key_fn(row)
        if key is None:
            continue
        race_acc = acc.setdefault(key, {})
        cur = race_acc.get(row["race_id"], [0, 0, 0])
        cur[0] += 100
        cur[1] += row["payout"]
        cur[2] += 1
        race_acc[row["race_id"]] = cur
    return {k: [tuple(v) for v in races.values()] for k, races in acc.items()}


def _fetch(con, bet_type: int) -> list[dict]:
    cur = con.execute(BETS_SQL, [bet_type])
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _band_label(lo: float, hi: float) -> str:
    return f"{lo:g}-{hi:g}倍" if hi < 1e9 else f"{lo:g}倍以上"


# 帯は定義順に並べる。文字列ソートだと "500倍以上" が "50-100倍" より前に来る。
_BAND_ORDER = {_band_label(lo, hi): i for i, (lo, hi) in enumerate(ODDS_BANDS)}


def _band_of(odds: float) -> str | None:
    for lo, hi in ODDS_BANDS:
        if lo <= odds < hi:
            return _band_label(lo, hi)
    return None


def _verdict(r: dict, alpha_note: str) -> str:
    """信頼区間が 75% を含むかどうかで判定する。"""
    if r["lo"] > NO_EDGE_ROI:
        return f"75%より高い{alpha_note}"
    if r["hi"] < NO_EDGE_ROI:
        return f"75%より低い{alpha_note}"
    return "75%と区別できない"


def analyse(con, bet_type: int) -> tuple[list[str], int]:
    """1賭式ぶんの分析。出力行と、実施した検定の数を返す。"""
    rows = _fetch(con, bet_type)
    if not rows:
        return [f"  {BET_NAMES[bet_type]}: データなし"], 0

    out = ["", f"  ### {BET_NAMES[bet_type]}", ""]
    n_tests = 0

    segments = [
        ("オッズ帯", lambda r: _band_of(r["odds"])),
        ("車立", lambda r: f"{int(r['tosu'])}車立" if r["tosu"] else None),
        ("時間帯", lambda r: "ミッドナイト" if r["is_midnight"] else "昼間"),
        (
            "バンク周長",
            lambda r: f"{int(r['bank_length_m'])}バンク" if r["bank_length_m"] else None,
        ),
    ]

    for title, key_fn in segments:
        groups = _aggregate(rows, key_fn)
        if not groups:
            continue
        out.append(f"  [{title}]")
        out.append(
            f"    {_pad('', 14)} {'R数':>6} {'点数':>8} {'回収率':>8} {'95%CI':>17}  判定"
        )
        for key in sorted(groups, key=lambda k: (_BAND_ORDER.get(k, 99), k)):
            r = _roi(groups[key])
            if r is None or r["races"] < 20:
                continue
            n_tests += 1
            out.append(
                f"    {_pad(str(key), 14)} {r['races']:6d} {r['bets']:8d}"
                f" {r['roi']:8.1%}  {r['lo']:6.1%}〜{r['hi']:6.1%}  {_verdict(r, '')}"
            )
        out.append("")
    return out, n_tests


def report(con) -> str:
    out = [
        "",
        "  市場が歪んでいる場所を探す",
        "  " + "=" * 60,
        "",
        f"  控除率が一律なら、どの帯を全部買っても回収率は約{NO_EDGE_ROI:.0%}になるはず。",
        "  そこから外れる帯があれば、系統的な歪みの候補。",
    ]
    total_tests = 0
    for bet_type in (EXACTA, TRIFECTA):
        lines, n = analyse(con, bet_type)
        out += lines
        total_tests += n

    out += [
        "  " + "=" * 60,
        f"  実施した検定: {total_tests} 件",
        "",
        "  多重比較の補正:",
        f"    95%信頼区間を {total_tests} 回引けば、歪みが1つも無くても",
        f"    期待値 {total_tests * 0.05:.1f} 件は「有意」に見える。",
        "    上の判定を額面どおり受け取ってはいけない。",
        "",
        "    Bonferroni 補正なら有意水準は 0.05 / "
        f"{total_tests} = {0.05 / max(total_tests, 1):.4f}、",
        f"    つまり {100 * (1 - 0.05 / max(total_tests, 1)):.2f}% 信頼区間で見る必要がある。",
        "    上に出しているのは 95% 区間なので、この基準では**どれも有意ではない**",
        "    と考えておくのが安全。",
        "",
        "  歪みを見つけたと思ったら:",
        "    1. その帯だけを別期間のデータで再検証する（追試）",
        "    2. なぜ歪むのか、事前に説明がつくかを考える",
        "       （後から理由をつけるのは簡単なので、順序が重要）",
        "    3. 自分の投票でオッズが下がる分を差し引いても残るか確認する",
        "",
        "  この3つを通らない「発見」は、賭ける根拠にはならない。",
    ]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="keirin.segments", description=__doc__)
    p.add_argument("--db", type=Path, default=Path("data/keirin.duckdb"))
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")

    con = duckdb.connect(str(args.db), read_only=True)
    print(report(con))
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
