"""期待値バックテストのテスト。

ここで守りたいのは:
  * 車券確率が確率として成立していること — 合計が1にならないと期待値が全部狂う
  * 較正が恒等変換を恒等と判定できること — 過剰に補正すると確率が壊れる
  * 回収率の計算と分割にリークが無いこと
"""

from itertools import permutations

import numpy as np
import pytest

from keirin.backtest import (
    apply_calibration,
    exacta_probs,
    fit_calibration,
    roi_with_ci,
    simulate,
    three_way_split,
    trifecta_probs,
)
from keirin.dataset import RaceSample


def _sample(n=4, winner_idx=0, date="20260801", race_id="R1"):
    return RaceSample(
        race_id=race_id,
        kaisai_date=date,
        syaban=np.arange(1, n + 1),
        x=np.zeros((n, 1)),
        winner_idx=winner_idx,
        p_market=None,
    )


class TestHarville:
    def test_exacta_probabilities_sum_to_one(self):
        """全順序対を覆っているので合計は1。ここがずれると期待値が狂う。"""
        p = np.array([0.5, 0.3, 0.15, 0.05])
        got = exacta_probs(p, np.arange(1, 5))
        assert len(got) == 4 * 3
        assert sum(got.values()) == pytest.approx(1.0)

    def test_trifecta_probabilities_sum_to_one(self):
        p = np.array([0.5, 0.3, 0.15, 0.05])
        got = trifecta_probs(p, np.arange(1, 5))
        assert len(got) == 4 * 3 * 2
        assert sum(got.values()) == pytest.approx(1.0)

    def test_exacta_matches_hand_calculation(self):
        p = np.array([0.5, 0.3, 0.2])
        got = exacta_probs(p, np.arange(1, 4))
        assert got["0102"] == pytest.approx(0.5 * 0.3 / 0.5)
        assert got["0201"] == pytest.approx(0.3 * 0.5 / 0.7)

    def test_marginalising_the_exacta_recovers_the_win_probability(self):
        """dataset 側が市場に対してやっているのと逆向きの操作。整合すること。"""
        p = np.array([0.5, 0.3, 0.2])
        got = exacta_probs(p, np.arange(1, 4))
        for i, want in enumerate(p, start=1):
            marginal = sum(v for k, v in got.items() if k[:2] == f"{i:02d}")
            assert marginal == pytest.approx(want)

    def test_combination_format_matches_the_odds_tables(self):
        """車番2桁ゼロ埋め連結。ずれると JOIN が全部外れて静かに0点になる。"""
        got = exacta_probs(np.array([0.6, 0.4]), np.array([7, 10]))
        assert set(got) == {"0710", "1007"}


class TestCalibration:
    def _make(self, n_races=400, seed=0):
        gen = np.random.default_rng(seed)
        probs, samples = [], []
        for i in range(n_races):
            p = gen.dirichlet(np.ones(7) * 2)
            winner = int(gen.choice(7, p=p))
            probs.append(p)
            samples.append(_sample(7, winner, race_id=f"R{i}"))
        return probs, samples

    def test_already_calibrated_probabilities_give_a_near_one(self):
        probs, samples = self._make()
        assert fit_calibration(probs, samples) == pytest.approx(1.0, abs=0.25)

    def test_overconfident_model_is_pulled_back(self):
        """自信過剰な確率には a < 1 が当たり、鋭さが緩められること。"""
        probs, samples = self._make()
        sharp = []
        for p in probs:
            q = p**2.0
            sharp.append(q / q.sum())
        assert fit_calibration(sharp, samples) < 1.0

    def test_calibration_preserves_ranking(self):
        """較正は確率の鋭さを変えるだけで、順位を入れ替えてはいけない。"""
        p = [np.array([0.5, 0.3, 0.2])]
        for a in (0.5, 1.5):
            q = apply_calibration(p, a)[0]
            assert list(np.argsort(-q)) == [0, 1, 2]
            assert q.sum() == pytest.approx(1.0)


class TestRoi:
    def test_roi_is_return_over_stake(self):
        r = roi_with_ci([(100, 0, 1), (100, 300, 1)], n_boot=200)
        assert r["roi"] == pytest.approx(1.5)
        assert r["staked"] == 200
        assert r["returned"] == 300

    def test_no_bets_returns_none(self):
        assert roi_with_ci([]) is None

    def test_confidence_interval_brackets_the_point_estimate(self):
        r = roi_with_ci([(100, 150, 1)] * 200, n_boot=500)
        assert r["lo"] <= r["roi"] <= r["hi"]


class TestSimulate:
    def _fixture(self):
        s = _sample(3, winner_idx=0)
        p = [np.array([0.5, 0.3, 0.2])]
        # 0102 の真の確率は 0.3。オッズ 10 倍なら EV = 2.0
        odds = {("R1", 6): {"0102": 10.0, "0103": 1.0, "0201": 1.0}}
        payouts = {("R1", 6): {"0102": 1000}}
        return [s], p, odds, payouts

    def test_only_bets_above_the_threshold(self):
        samples, p, odds, payouts = self._fixture()
        staked, returned, n_bets = simulate(samples, p, odds, payouts, 6, 1.0)[0]
        assert n_bets == 1          # 0102 のみ EV > 1.0
        assert staked == 100
        assert returned == 1000

    def test_raising_the_threshold_never_adds_bets(self):
        samples, p, odds, payouts = self._fixture()
        counts = [
            simulate(samples, p, odds, payouts, 6, th)[0][2]
            for th in (0.0, 1.0)
        ]
        assert counts[0] >= counts[1]

    def test_losing_bets_return_nothing(self):
        samples, p, odds, payouts = self._fixture()
        payouts[("R1", 6)] = {}  # 的中なし
        staked, returned, _ = simulate(samples, p, odds, payouts, 6, 1.0)[0]
        assert staked == 100
        assert returned == 0

    def test_races_without_odds_are_skipped(self):
        samples, p, _, payouts = self._fixture()
        assert simulate(samples, p, {}, payouts, 6, 0.0) == []


class TestThreeWaySplit:
    def test_splits_are_chronological_and_disjoint(self):
        samples = [
            _sample(date=d, race_id=f"{d}-{i}")
            for d in (f"202608{n:02d}" for n in range(1, 11))
            for i in range(2)
        ]
        train, calib, test = three_way_split(samples)
        assert train and calib and test
        dt = {s.kaisai_date for s in train}
        dc = {s.kaisai_date for s in calib}
        dte = {s.kaisai_date for s in test}
        assert not (dt & dc) and not (dc & dte) and not (dt & dte)
        assert max(dt) < min(dc) < max(dc) < min(dte)

    def test_calibration_set_is_not_the_training_set(self):
        """学習に使ったデータで較正すると、較正済みに見えてしまう。"""
        samples = [
            _sample(date=d, race_id=f"{d}-{i}")
            for d in ("20260801", "20260802", "20260803", "20260804", "20260805")
            for i in range(2)
        ]
        train, calib, _ = three_way_split(samples)
        assert {s.race_id for s in train}.isdisjoint({s.race_id for s in calib})
