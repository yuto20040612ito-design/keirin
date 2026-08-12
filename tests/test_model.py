"""データセット構築とベースラインモデルのテスト。

ここで守りたいのは:
  * 2車単からの1着確率の周辺化 — 誤ると市場との比較が丸ごと無意味になる
  * 控除率が正規化で消えること — 消さないと市場を不当に弱く見積もる
  * 時系列split が開催日をまたがないこと — またぐと即リークする
"""

from pathlib import Path

import duckdb
import numpy as np
import pytest

from keirin.baseline import (
    _softmax,
    fit_conditional_logit,
    log_loss,
    top1_rate,
    uniform_probs,
)
from keirin.dataset import RaceSample, build, time_split

SCHEMA = Path(__file__).resolve().parents[1] / "sql" / "schema.sql"

TAKEOUT = 0.75  # 払戻率。合成オッズにも現実と同じ控除率を入れておく


def _exacta_probs(win_probs):
    """1着確率から (i→j) の順序対確率を作る (Harville近似)。"""
    out = {}
    for i, pi in enumerate(win_probs, start=1):
        for j, pj in enumerate(win_probs, start=1):
            if i == j:
                continue
            out[(i, j)] = pi * pj / (1 - pi)
    return out


def _make_db(win_probs=(0.5, 0.3, 0.2), winner_syaban=1):
    con = duckdb.connect(":memory:")
    con.execute(SCHEMA.read_text(encoding="utf-8"))
    n = len(win_probs)
    con.execute(
        "INSERT INTO races (race_id, kaisai_date, jyo_cd, race_no, tosu) "
        "VALUES ('R1', DATE '2026-08-01', '21', 1, ?)",
        [n],
    )
    for s in range(1, n + 1):
        con.execute(
            "INSERT INTO entries (race_id, syaban, rating) VALUES ('R1', ?, ?)",
            [s, 80.0 + s],
        )
        con.execute(
            "INSERT INTO results (race_id, syaban, finish_pos) VALUES ('R1', ?, ?)",
            [s, 1 if s == winner_syaban else s + 1],
        )
    for (i, j), p in _exacta_probs(win_probs).items():
        con.execute(
            "INSERT INTO odds_snapshots (race_id, bet_type, combination, snapshot_at,"
            " is_official, odds_low, fetched_at, secs_to_close)"
            " VALUES ('R1', 6, ?, TIMESTAMP '2026-08-01 10:00:00', TRUE, ?,"
            " TIMESTAMP '2026-08-01 10:00:00', 0)",
            [f"{i:02d}{j:02d}", TAKEOUT / p],
        )
    return con


class TestMarketMarginalisation:
    def test_recovers_win_probabilities_from_exacta_odds(self):
        """競輪に単勝は無いので、2車単を2着側で周辺化して1着確率を作る。"""
        con = _make_db(win_probs=(0.5, 0.3, 0.2))
        samples = build(con)
        assert len(samples) == 1
        np.testing.assert_allclose(samples[0].p_market, [0.5, 0.3, 0.2], atol=1e-9)

    def test_takeout_is_removed_by_normalisation(self):
        """正規化しないと控除率のぶん市場を弱く見積もってしまう。"""
        con = _make_db()
        p = build(con)[0].p_market
        assert p.sum() == pytest.approx(1.0)

    def test_incomplete_exacta_set_is_rejected(self):
        """組が欠けたまま周辺化すると確率が歪む。使わないのが正しい。"""
        con = _make_db()
        con.execute("DELETE FROM odds_snapshots WHERE combination = '0203'")
        assert build(con, require_market=True) == []

    def test_winner_index_points_at_the_actual_winner(self):
        con = _make_db(winner_syaban=3)
        s = build(con)[0]
        assert s.syaban[s.winner_idx] == 3

    def test_race_without_a_winner_is_dropped(self):
        """全員失格などで1着が居ないレースは学習にも評価にも使えない。"""
        con = _make_db()
        con.execute("UPDATE results SET finish_pos = NULL")
        assert build(con) == []


def _sample(ratings, winner_idx, p_market=None, date="20260801", race_id="R"):
    x = np.asarray(ratings, dtype=float).reshape(-1, 1)
    return RaceSample(
        race_id=race_id,
        kaisai_date=date,
        syaban=np.arange(1, len(ratings) + 1),
        x=x,
        winner_idx=winner_idx,
        p_market=p_market,
    )


class TestConditionalLogit:
    def test_probabilities_sum_to_one_within_a_race(self):
        """レース内で1人だけが勝つ。独立な二値分類ではこれが崩れる。"""
        p = _softmax(np.array([1.0, 2.0, 3.0]))
        assert p.sum() == pytest.approx(1.0)

    def test_recovers_a_known_coefficient(self):
        """合成データで真の β を復元できること。"""
        true_beta = 0.8
        gen = np.random.default_rng(1)
        samples = []
        for i in range(4000):
            ratings = gen.normal(0, 1, 7)
            p = np.exp(true_beta * ratings)
            p /= p.sum()
            winner = int(gen.choice(len(ratings), p=p))
            samples.append(_sample(ratings, winner, race_id=f"R{i}"))
        beta = fit_conditional_logit(samples)
        assert beta[0] == pytest.approx(true_beta, abs=0.1)

    def test_higher_rating_gets_higher_probability(self):
        samples = [_sample([1.0, 2.0, 3.0], 2, race_id=f"R{i}") for i in range(50)]
        beta = fit_conditional_logit(samples)
        p = _softmax(samples[0].x @ beta)
        assert p[2] > p[1] > p[0]


class TestMetrics:
    def test_log_loss_of_a_confident_correct_prediction_is_near_zero(self):
        s = _sample([1.0, 2.0], 1)
        assert log_loss([s], [np.array([0.001, 0.999])]) == pytest.approx(0.0, abs=2e-3)

    def test_uniform_log_loss_equals_log_n(self):
        s = _sample([1.0] * 7, 0)
        assert log_loss([s], uniform_probs([s])) == pytest.approx(np.log(7))

    def test_top1_rate(self):
        a = _sample([1.0, 2.0], 1)
        b = _sample([1.0, 2.0], 0)
        probs = [np.array([0.2, 0.8]), np.array([0.2, 0.8])]
        assert top1_rate([a, b], probs) == pytest.approx(0.5)


class TestTimeSplit:
    def test_train_and_test_never_share_a_race_day(self):
        """同一開催日がまたがると、同じ選手・同じ条件が両側に入ってリークする。"""
        samples = [
            _sample([1.0, 2.0], 0, date=d, race_id=f"{d}-{i}")
            for d in ("20260801", "20260802", "20260803", "20260804")
            for i in range(3)
        ]
        train, test = time_split(samples, 0.7)
        assert train and test
        assert not ({s.kaisai_date for s in train} & {s.kaisai_date for s in test})

    def test_test_is_strictly_later_than_train(self):
        samples = [
            _sample([1.0, 2.0], 0, date=d, race_id=f"{d}-{i}")
            for d in ("20260801", "20260802", "20260803")
            for i in range(2)
        ]
        train, test = time_split(samples, 0.7)
        assert max(s.kaisai_date for s in train) < min(s.kaisai_date for s in test)
