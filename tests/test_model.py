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
from keirin.dataset import (
    FEATURE_NAMES,
    FEATURE_SETS,
    MARKET_SETS,
    OWN_FEATURES,
    RaceSample,
    build,
    select,
    time_split,
)

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


def _add_lines(con, lines):
    """lines は [[車番...], ...]。'0' 区切りを展開した後の形。"""
    for line_no, members in enumerate(lines, start=1):
        for pos, syaban in enumerate(members, start=1):
            con.execute(
                "INSERT INTO race_lines (race_id, line_no, position, syaban,"
                " line_size, is_solo, source, fetched_at)"
                " VALUES ('R1', ?, ?, ?, ?, ?, 'AplNarabiYoso', NULL)",
                [line_no, pos, syaban, len(members), len(members) == 1],
            )


def _make_db(win_probs=(0.5, 0.3, 0.2), winner_syaban=1, lines=([1, 2], [3]),
             velodrome=True):
    con = duckdb.connect(":memory:")
    con.execute(SCHEMA.read_text(encoding="utf-8"))
    n = len(win_probs)
    if velodrome:
        con.execute(
            "INSERT INTO velodromes (jyo_cd, jyo_name, bank_length_m, straight_m,"
            " share_nige, share_makuri, share_sashi)"
            " VALUES ('21', '弥彦競輪場', 400.0, 63.1, 0.19, 0.30, 0.51)"
        )
    con.execute(
        "INSERT INTO races (race_id, kaisai_date, jyo_cd, race_no, tosu) "
        "VALUES ('R1', DATE '2026-08-01', '21', 1, ?)",
        [n],
    )
    for s in range(1, n + 1):
        con.execute(
            "INSERT INTO entries (race_id, syaban, rating, kyakushitsu, gear_ratio,"
            " cnt_s, cnt_b, win_nige, win_makuri, win_sashi, win_mark,"
            " cnt_1st, cnt_2nd, cnt_3rd, cnt_out, rate_win, rate_top3)"
            " VALUES ('R1', ?, ?, ?, 3.92, ?, ?, ?, 0, ?, 0, ?, 2, 2, ?, ?, ?)",
            [
                s, 80.0 + s,
                ["逃", "追", "両"][(s - 1) % 3],
                s,                      # cnt_s
                10 - s,                 # cnt_b
                4 if s == 1 else 0,     # win_nige
                0 if s == 1 else 4,     # win_sashi
                s,                      # cnt_1st
                20 - s,                 # cnt_out  -> starts = s + 2 + 2 + (20-s) = 24
                0.1 * s,                # rate_win
                0.3 * s,                # rate_top3
            ],
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
    if lines:
        _add_lines(con, lines)
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


class TestLineFeatures:
    """ライン内位置は競輪では脚質の代理変数になる(先頭=先行、番手=追込)。"""

    def _x(self, lines, names):
        con = _make_db(lines=lines)
        s = build(con)[0]
        return select([s], names)[0].x

    def test_line_head_is_flagged(self):
        # ライン 1-2 と単騎 3。車番順に並ぶので行は [1, 2, 3]
        x = self._x([[1, 2], [3]], ["is_line_head"])
        assert list(x[:, 0]) == [1.0, 0.0, 1.0]  # 単騎も自ラインの先頭

    def test_solo_is_flagged(self):
        x = self._x([[1, 2], [3]], ["is_solo"])
        assert list(x[:, 0]) == [0.0, 0.0, 1.0]

    def test_line_size_and_position(self):
        x = self._x([[1, 2], [3]], ["line_size", "line_pos"])
        assert list(x[:, 0]) == [2.0, 2.0, 1.0]
        assert list(x[:, 1]) == [1.0, 2.0, 1.0]

    def test_line_head_rating_is_shared_across_the_line(self):
        """番手の選手にとっては「誰の後ろに付くか」が効く。"""
        # rating は _make_db で 81, 82, 83 (車番+80)
        x = self._x([[1, 2], [3]], ["line_head_rating"])
        assert x[0, 0] == x[1, 0] == 81.0   # ライン先頭は車1
        assert x[2, 0] == 83.0              # 単騎は自分自身

    def test_line_rating_mean(self):
        x = self._x([[1, 2], [3]], ["line_rating_mean"])
        assert x[0, 0] == x[1, 0] == pytest.approx(81.5)
        assert x[2, 0] == pytest.approx(83.0)

    def test_head_interaction_counts_lines(self):
        """レース内で一定の値は softmax で消えるので、交互作用にしてある。"""
        x = self._x([[1], [2], [3]], ["head_x_nlines"])
        assert list(x[:, 0]) == [3.0, 3.0, 3.0]

    def test_races_without_line_data_are_dropped_when_required(self):
        """特徴量セットを変えて比較するとき、レース集合が変わってはいけない。"""
        con = _make_db(lines=None)
        assert build(con, require_lines=True) == []
        assert len(build(con, require_lines=False)) == 1


class TestFeatureSelection:
    def test_select_preserves_race_set_and_labels(self):
        con = _make_db()
        full = build(con)
        sub = select(full, ["rating"])
        assert len(sub) == len(full)
        assert sub[0].winner_idx == full[0].winner_idx
        assert sub[0].x.shape[1] == 1

    def test_rating_rank_is_normalised(self):
        con = _make_db()
        x = select(build(con), ["rating_rank"])[0].x[:, 0]
        # rating は 81,82,83 なので車3が最上位
        assert x[2] == pytest.approx(0.0)
        assert x[0] == pytest.approx(1.0)

    def test_gap_to_top(self):
        con = _make_db()
        x = select(build(con), ["rating_gap_top"])[0].x[:, 0]
        assert list(x) == [2.0, 1.0, 0.0]


class TestFormFeatures:
    """出走表HTML由来の特徴量。脚質はライン位置の代理ではなく実物。"""

    def _x(self, names):
        con = _make_db()
        return select(build(con), names)[0].x

    def test_kyakushitsu_dummies(self):
        # _make_db は車1=逃, 車2=追, 車3=両
        x = self._x(["is_nige", "is_ryo"])
        assert list(x[:, 0]) == [1.0, 0.0, 0.0]
        assert list(x[:, 1]) == [0.0, 0.0, 1.0]

    def test_back_count_is_normalised_by_starts(self):
        """生の回数だと『どう勝つ選手か』ではなく『何走したか』を見てしまう。"""
        # cnt_b = 10 - s, starts = 24
        x = self._x(["b_per_start"])
        assert x[0, 0] == pytest.approx(9 / 24)
        assert x[2, 0] == pytest.approx(7 / 24)

    def test_kimarite_is_a_share_not_a_count(self):
        # 車1: win_nige=4, 他0 -> 逃げ構成比 1.0
        # 車2: win_sashi=4, 他0 -> 差し構成比 1.0
        x = self._x(["share_nige", "share_sashi"])
        assert x[0, 0] == pytest.approx(1.0)
        assert x[0, 1] == pytest.approx(0.0)
        assert x[1, 0] == pytest.approx(0.0)
        assert x[1, 1] == pytest.approx(1.0)

    def test_rates_pass_through(self):
        x = self._x(["rate_win", "rate_top3"])
        assert x[0, 0] == pytest.approx(0.1)
        assert x[2, 1] == pytest.approx(0.9)

    def test_races_without_form_data_are_dropped_when_required(self):
        con = _make_db()
        con.execute("UPDATE entries SET kyakushitsu = NULL")
        assert build(con, require_form=True) == []
        assert len(build(con, require_form=False)) == 1

    def test_zero_kimarite_does_not_divide_by_zero(self):
        """まだ勝ったことがない選手でも落ちないこと。"""
        con = _make_db()
        con.execute("UPDATE entries SET win_nige=0, win_makuri=0, win_sashi=0, win_mark=0")
        x = select(build(con), ["share_nige"])[0].x
        assert list(x[:, 0]) == [0.0, 0.0, 0.0]


class TestFeatureSets:
    def test_sets_are_nested_and_cover_all_own_features(self):
        """順に足していく比較なので、前のセットは次のセットに含まれていること。"""
        sets = list(FEATURE_SETS.values())
        for prev, cur in zip(sets, sets[1:]):
            assert set(prev) <= set(cur)
        assert set(sets[-1]) == set(OWN_FEATURES)

    def test_market_feature_is_kept_out_of_the_own_feature_sets(self):
        """市場特徴量を混ぜると「市場を超えたか」の比較にならなくなる。"""
        for names in FEATURE_SETS.values():
            assert "mkt_logit" not in names

    def test_market_sets_add_own_features_on_top_of_the_market(self):
        """エッジの検定は、市場を出発点にして上乗せできるかを見るもの。"""
        base, both = MARKET_SETS["市場のみ"], MARKET_SETS["市場+自前特徴"]
        assert base == ["mkt_logit"]
        assert set(base) < set(both)
        assert set(both) == set(FEATURE_NAMES)

    def test_market_feature_reproduces_the_market(self):
        """log(市場確率) を係数1で softmax に通すと市場そのものに戻ること。"""
        con = _make_db(win_probs=(0.5, 0.3, 0.2))
        s = select(build(con), ["mkt_logit"])[0]
        np.testing.assert_allclose(_softmax(s.x[:, 0]), [0.5, 0.3, 0.2], atol=1e-9)
