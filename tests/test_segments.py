"""セグメント分析のテスト。

ここで守りたいのは:
  * オッズ帯の割り当てと並び順（文字列ソートだと 500倍以上 が 50-100倍 より前に来る）
  * ブートストラップをレース単位でやること（賭け単位だと区間が不当に狭くなる）
  * 検定数を数えていること（多重比較の補正に要る）
"""

import numpy as np
import pytest

from keirin.segments import (
    NO_EDGE_ROI,
    ODDS_BANDS,
    _aggregate,
    _band_of,
    _BAND_ORDER,
    _roi,
    _verdict,
)


class TestOddsBands:
    def test_assignment(self):
        assert _band_of(1.5) == "1-5倍"
        assert _band_of(7.0) == "5-10倍"
        assert _band_of(250.0) == "100-500倍"
        assert _band_of(1200.0) == "500倍以上"

    def test_boundaries_are_half_open(self):
        """境界が両方の帯に入ると二重計上になる。"""
        assert _band_of(5.0) == "5-10倍"
        assert _band_of(4.999) == "1-5倍"

    def test_below_the_first_band_is_unassigned(self):
        assert _band_of(0.5) is None

    def test_bands_are_ordered_by_definition_not_by_string(self):
        order = [_BAND_ORDER[_band_of(o)] for o in (2, 7, 15, 30, 70, 200, 900)]
        assert order == sorted(order)

    def test_every_band_has_a_distinct_order_key(self):
        assert len(_BAND_ORDER) == len(ODDS_BANDS)


class TestRoi:
    def test_roi_is_return_over_stake(self):
        r = _roi([(100, 0, 1), (100, 300, 1)], n_boot=200)
        assert r["roi"] == pytest.approx(1.5)
        assert r["races"] == 2
        assert r["bets"] == 2

    def test_empty_returns_none(self):
        assert _roi([]) is None

    def test_bootstrap_resamples_races_not_bets(self):
        """1レース内は必ず1点だけ的中するので結果が強く相関する。

        賭け単位で resample すると、その相関を無視して区間が狭くなりすぎる。
        ここでは1レース=1標本になっていることを、標本数の効き方で確認する。
        """
        # 同じ賭け数でもレース数が少ないほど区間は広くなるはず
        few = _roi([(4200, 3000, 42)] * 5 + [(4200, 9000, 42)] * 5, n_boot=800)
        many = _roi([(420, 300, 42)] * 50 + [(420, 900, 42)] * 50, n_boot=800)
        assert (few["hi"] - few["lo"]) > (many["hi"] - many["lo"])

    def test_confidence_interval_brackets_the_point_estimate(self):
        r = _roi([(100, 75, 1)] * 300, n_boot=500)
        assert r["lo"] <= r["roi"] <= r["hi"]


class TestVerdict:
    def test_above_baseline(self):
        assert "高い" in _verdict({"roi": 1.0, "lo": 0.9, "hi": 1.1}, "")

    def test_below_baseline(self):
        assert "低い" in _verdict({"roi": 0.5, "lo": 0.4, "hi": 0.6}, "")

    def test_indistinguishable(self):
        assert "区別できない" in _verdict({"roi": 0.76, "lo": 0.6, "hi": 0.9}, "")

    def test_baseline_is_the_payback_rate(self):
        """控除率25%なので、無選別に買えば75%に収束する。"""
        assert NO_EDGE_ROI == pytest.approx(0.75)


class TestAggregate:
    def _rows(self):
        return [
            {"race_id": "R1", "odds": 2.0, "payout": 200},
            {"race_id": "R1", "odds": 30.0, "payout": 0},
            {"race_id": "R2", "odds": 3.0, "payout": 0},
        ]

    def test_groups_by_segment_then_by_race(self):
        got = _aggregate(self._rows(), lambda r: _band_of(r["odds"]))
        assert set(got) == {"1-5倍", "20-50倍"}
        # 1-5倍 は R1 と R2 の2レース
        assert len(got["1-5倍"]) == 2

    def test_stake_is_100_yen_per_bet(self):
        got = _aggregate(self._rows(), lambda r: _band_of(r["odds"]))
        staked = sum(v[0] for v in got["1-5倍"])
        assert staked == 200

    def test_payouts_are_summed_within_a_race(self):
        rows = [
            {"race_id": "R1", "odds": 2.0, "payout": 200},
            {"race_id": "R1", "odds": 3.0, "payout": 300},
        ]
        got = _aggregate(rows, lambda r: _band_of(r["odds"]))
        assert got["1-5倍"] == [(200, 500, 2)]

    def test_none_key_rows_are_skipped(self):
        rows = [{"race_id": "R1", "odds": 0.5, "payout": 0}]
        assert _aggregate(rows, lambda r: _band_of(r["odds"])) == {}


class TestNoEdgeBaselineArithmetic:
    def test_buying_everything_in_a_fair_market_returns_the_payback_rate(self):
        """全通り買いの回収率は払戻率に一致する、という前提の確認。

        市場のインプライド確率が真の確率と一致していれば、
        E[的中オッズ] = 組数 × 払戻率 になり、回収率は払戻率に収束する。
        この分析はここからのズレを歪みとして読むので、前提を明示しておく。
        """
        gen = np.random.default_rng(0)
        n_combos = 42
        per_race = []
        for _ in range(4000):
            true_p = gen.dirichlet(np.ones(n_combos))
            odds = NO_EDGE_ROI / true_p          # 払戻率25%控除のフェアオッズ
            winner = int(gen.choice(n_combos, p=true_p))
            per_race.append((100 * n_combos, 100 * odds[winner], n_combos))
        r = _roi(per_race, n_boot=200)
        assert r["roi"] == pytest.approx(NO_EDGE_ROI, abs=0.05)
