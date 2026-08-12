"""パース関数のテスト。

ここで守りたいのは主に2点:
  * ライン区切り("0")の解釈を壊さないこと — 競輪で最重要の構造情報なので
  * 締切時刻の日跨ぎ補正を場ごとに評価すること — R番号は場をまたいで重複する
"""

from datetime import datetime

from keirin.collect import JST, NetkeirinError, Race, _fix_midnight_rollover, _parse_hhmm
from keirin.netkeirin import iter_odds_rows, parse_line_forecast


class TestParseLineForecast:
    def test_splits_on_zero(self):
        payload = {"lineForecast": [["5", "1", "7", "0", "2", "4", "3", "0", "6"]]}
        assert parse_line_forecast(payload) == [[5, 1, 7], [2, 4, 3], [6]]

    def test_single_line(self):
        assert parse_line_forecast({"lineForecast": [["1", "2", "3"]]}) == [[1, 2, 3]]

    def test_all_solo(self):
        payload = {"lineForecast": [["1", "0", "2", "0", "3"]]}
        assert parse_line_forecast(payload) == [[1], [2], [3]]

    def test_trailing_and_leading_separators_do_not_create_empty_lines(self):
        payload = {"lineForecast": [["0", "1", "2", "0", "0", "3", "0"]]}
        assert parse_line_forecast(payload) == [[1, 2], [3]]

    def test_empty(self):
        assert parse_line_forecast({}) == []
        assert parse_line_forecast({"lineForecast": []}) == []

    def test_non_numeric_tokens_are_skipped(self):
        payload = {"lineForecast": [["1", "x", "2"]]}
        assert parse_line_forecast(payload) == [[1, 2]]


class TestIterOddsRows:
    def test_parses_bet_types_and_combinations(self):
        payload = {
            "official_dt": "2026-08-11 10:46:00",
            "list_5": [["0102", "3.2", "0", "2"]],
            "list_9": [["010203", "91.9", "0", "26"]],
        }
        rows = sorted(iter_odds_rows(payload))
        assert rows == [
            (5, "0102", 3.2, None, 2),
            (9, "010203", 91.9, None, 26),
        ]

    def test_wide_keeps_upper_bound(self):
        payload = {"list_7": [["0102", "1.6", "2.1", "2"]]}
        assert list(iter_odds_rows(payload)) == [(7, "0102", 1.6, 2.1, 2)]

    def test_zero_odds_becomes_none(self):
        """発売前などオッズ 0 は「値なし」であって 0.0 倍ではない。"""
        payload = {"list_5": [["0102", "0", "0", "0"]]}
        assert list(iter_odds_rows(payload)) == [(5, "0102", None, None, 0)]

    def test_ignores_non_list_keys(self):
        payload = {"official_dt": "2026-08-11 10:46:00", "list_5": []}
        assert list(iter_odds_rows(payload)) == []

    def test_malformed_rows_are_skipped(self):
        payload = {"list_5": [["0102"], ["0103", "2.0", "0", "1"]]}
        assert list(iter_odds_rows(payload)) == [(5, "0103", 2.0, None, 1)]


class TestParseHhmm:
    def test_basic(self):
        assert _parse_hhmm("20260811", "10:40") == datetime(
            2026, 8, 11, 10, 40, tzinfo=JST
        )

    def test_hour_24_rolls_to_next_day(self):
        assert _parse_hhmm("20260811", "24:10") == datetime(
            2026, 8, 12, 0, 10, tzinfo=JST
        )

    def test_invalid(self):
        assert _parse_hhmm("20260811", "") is None
        assert _parse_hhmm("20260811", "abc") is None


def _race(race_id, jyo_cd, race_no, close_hhmm):
    return Race(
        race_id=race_id,
        kaisai_date="20260811",
        jyo_cd=jyo_cd,
        jyo=jyo_cd,
        race_no=race_no,
        race_name="",
        tosu=7,
        close_at=_parse_hhmm("20260811", close_hhmm),
        start_at=None,
    )


class TestMidnightRollover:
    def test_does_not_shift_normal_schedule(self):
        """異なる場のR番号が重複していても、正常な時刻をずらしてはいけない。"""
        races = [
            _race("202608113501", "35", 1, "08:25"),
            _race("202608112101", "21", 1, "10:40"),
            _race("202608114809", "48", 9, "23:25"),
        ]
        _fix_midnight_rollover(races)
        assert races[0].close_at == datetime(2026, 8, 11, 8, 25, tzinfo=JST)
        assert races[1].close_at == datetime(2026, 8, 11, 10, 40, tzinfo=JST)
        assert races[2].close_at == datetime(2026, 8, 11, 23, 25, tzinfo=JST)

    def test_shifts_when_time_goes_backwards_within_same_venue(self):
        races = [
            _race("202608114808", "48", 8, "23:40"),
            _race("202608114809", "48", 9, "00:05"),
        ]
        _fix_midnight_rollover(races)
        assert races[0].close_at == datetime(2026, 8, 11, 23, 40, tzinfo=JST)
        assert races[1].close_at == datetime(2026, 8, 12, 0, 5, tzinfo=JST)

    def test_missing_close_time_is_tolerated(self):
        races = [_race("202608114801", "48", 1, ""), _race("202608114802", "48", 2, "10:00")]
        _fix_midnight_rollover(races)
        assert races[0].close_at is None
        assert races[1].close_at == datetime(2026, 8, 11, 10, 0, tzinfo=JST)


# ---------------------------------------------------------------------------
# 結果ページ HTML のパース
# ---------------------------------------------------------------------------

from keirin.load import _parse_payout_table, _parse_result_table  # noqa: E402


PAYOUT_HTML = """
<table class="Payout_Detail_Table">
<tr><th>２車複</th><td>1-5</td><td>240円</td><td>1人気</td></tr>
<tr><th>２車単</th><td>5&gt;1</td><td>750円</td><td>3人気</td></tr>
<tr><th>ワイド</th><td>1-5</td><td>140円</td><td>1人気</td></tr>
<tr><td>2-5</td><td>200円</td><td>3人気</td></tr>
<tr><th>３連複</th><td>1-2-5</td><td>240円</td><td>1人気</td></tr>
<tr><th>３連単</th><td>5&gt;1&gt;2</td><td>2,280円</td><td>7人気</td></tr>
</table>
"""


class TestPayoutParsing:
    def setup_method(self):
        self.rows = _parse_payout_table(PAYOUT_HTML, "R1", None)
        self.by_type = {(r[1], r[2]): r for r in self.rows}

    def test_ordered_bets_survive_html_entity_escaping(self):
        """'5&gt;1' を復元しないと2車単・3連単の払戻が丸ごと落ちる(実際に踏んだバグ)。"""
        assert (6, "0501") in self.by_type
        assert (9, "050102") in self.by_type

    def test_combination_is_zero_padded_to_match_odds_format(self):
        """払戻の組番は odds_snapshots.combination と同形式でなければ JOIN できない。"""
        assert self.by_type[(5, "0105")][2] == "0105"
        assert self.by_type[(8, "010205")][2] == "010205"

    def test_payout_amount_strips_comma(self):
        assert self.by_type[(9, "050102")][3] == 2280

    def test_popularity(self):
        assert self.by_type[(9, "050102")][4] == 7

    def test_bet_type_carries_over_to_continuation_rows(self):
        """ワイドは1レース3組で、2組目以降は賭式名のセルを持たない。"""
        assert (7, "0105") in self.by_type
        assert (7, "0205") in self.by_type


RESULT_HTML = """
<table class="RaceCard_Table ResultRefund">
<tr><th>着</th><th>枠番</th><th>車番</th><th>選手名</th><th>着差</th><th>上り</th><th>決</th><th>SB</th></tr>
<tr><td>1着</td><td>5</td><td>5</td><td>森崎英登</td><td></td><td>11.8</td><td>逃</td><td>B</td></tr>
<tr><td>2着</td><td>1</td><td>1</td><td>渡邊健</td><td>2車身</td><td>11.9</td><td>ク</td><td>S</td></tr>
<tr><td>棄</td><td>6</td><td>7</td><td>吉村文隆</td><td>－ (落車棄権)</td><td></td><td></td><td></td></tr>
</table>
"""


class TestResultParsing:
    def setup_method(self):
        self.rows = {r[1]: r for r in _parse_result_table(RESULT_HTML, "R1", None)}

    def test_finish_positions(self):
        assert self.rows[5][2] == 1
        assert self.rows[1][2] == 2

    def test_abnormal_finish_is_kept_with_null_position(self):
        """落車・失格の履歴は展開予測に効くので捨てない(実際に取りこぼしたバグ)。"""
        assert 7 in self.rows, "落車棄権の選手が丸ごと落ちている"
        assert self.rows[7][2] is None
        assert self.rows[7][3] == "落車棄権"

    def test_header_row_is_ignored(self):
        assert len(self.rows) == 3

    def test_kimarite_and_sb(self):
        assert self.rows[5][6] == "逃"
        assert self.rows[5][8] is True   # got_b
        assert self.rows[1][7] is True   # got_s

    def test_last_lap_time(self):
        assert self.rows[5][5] == 11.8
        assert self.rows[7][5] is None


# ---------------------------------------------------------------------------
# 収集済み索引 (バックフィルの再開に必要)
# ---------------------------------------------------------------------------

from keirin.manifest import Manifest  # noqa: E402


class TestManifest:
    def test_mark_and_has(self, tmp_path):
        m = Manifest(tmp_path)
        assert not m.has("AplRaceOdds", "R1")
        m.mark("AplRaceOdds", "R1")
        assert m.has("AplRaceOdds", "R1")

    def test_survives_process_restart(self, tmp_path):
        """中断して再実行したとき、続きから再開できなければ意味がない。"""
        Manifest(tmp_path).mark("AplRaceOdds", "R1")
        assert Manifest(tmp_path).has("AplRaceOdds", "R1")

    def test_kinds_are_independent(self, tmp_path):
        m = Manifest(tmp_path)
        m.mark("AplRaceOdds", "R1")
        assert not m.has("result_html", "R1")

    def test_marking_twice_does_not_duplicate(self, tmp_path):
        m = Manifest(tmp_path)
        m.mark("AplRaceOdds", "R1")
        m.mark("AplRaceOdds", "R1")
        assert (tmp_path / "manifest" / "AplRaceOdds.txt").read_text().count("R1") == 1


class TestBackfillDateMarker:
    """--kinds を絞った日を、後から別の kinds で埋め直せること。

    完了マーカーを日付だけで持つと、結果だけ集めた日に後から出走表を足せなくなる
    (実際にこれで出走表を取りこぼした)。
    """

    def test_date_marker_is_scoped_to_kinds(self, tmp_path):
        m = Manifest(tmp_path)
        odds_only = "20260810|" + "+".join(sorted(["AplRaceOdds"]))
        with_entries = "20260810|" + "+".join(sorted(["AplRaceOdds", "AplRaceHorse"]))
        m.mark("dates", odds_only)
        assert m.has("dates", odds_only)
        assert not m.has("dates", with_entries)

    def test_kind_order_does_not_change_the_marker(self, tmp_path):
        m = Manifest(tmp_path)
        a = "20260810|" + "+".join(sorted(["AplRaceOdds", "result_html"]))
        b = "20260810|" + "+".join(sorted(["result_html", "AplRaceOdds"]))
        m.mark("dates", a)
        assert m.has("dates", b)


# ---------------------------------------------------------------------------
# cron 方式 (共用レンタルサーバー向け)
# ---------------------------------------------------------------------------

from datetime import timedelta  # noqa: E402

from keirin.collect import (  # noqa: E402
    POLL_OFFSETS_SEC,
    _cached_schedule,
    _due_times,
    _read_last_run,
    _write_last_run,
)


class TestJst:
    def test_jst_is_a_fixed_nine_hour_offset(self):
        """日本標準時はサマータイムが無いので固定オフセットで完全に正しい。

        zoneinfo (Python 3.9+) に依存しないことで、Python が古い共用サーバーでも動く。
        """
        assert JST.utcoffset(None) == timedelta(hours=9)

    def test_summer_and_winter_have_the_same_offset(self):
        jan = datetime(2026, 1, 15, 12, 0, tzinfo=JST)
        aug = datetime(2026, 8, 15, 12, 0, tzinfo=JST)
        assert jan.utcoffset() == aug.utcoffset()


class TestLastRunState:
    def test_first_run_only_looks_back_briefly(self, tmp_path):
        """初回に過去を全部遡って一気に取りにいかないこと。"""
        now = datetime(2026, 8, 13, 12, 0, tzinfo=JST)
        got = _read_last_run(tmp_path, now)
        assert timedelta(seconds=0) < (now - got) <= timedelta(minutes=5)

    def test_round_trips(self, tmp_path):
        now = datetime(2026, 8, 13, 12, 0, tzinfo=JST)
        _write_last_run(tmp_path, now)
        assert _read_last_run(tmp_path, now + timedelta(minutes=5)) == now

    def test_corrupt_state_falls_back_instead_of_crashing(self, tmp_path):
        (tmp_path / "state").mkdir()
        (tmp_path / "state" / "last_run.txt").write_text("not a timestamp")
        now = datetime(2026, 8, 13, 12, 0, tzinfo=JST)
        assert _read_last_run(tmp_path, now) < now


class TestDueWindow:
    """cron は毎分走る。前回実行から今までに予定時刻を過ぎたものだけ取る。"""

    def _race(self, close_hhmm="12:00"):
        return _race_for_cron(close_hhmm)

    def test_scheduled_times_are_before_the_deadline(self):
        race = self._race()
        times = _due_times(race)
        # 最後の1つは締切直後の確定オッズ狙い
        assert sum(1 for t in times if t < race.close_at) == len(POLL_OFFSETS_SEC)
        assert max(times) > race.close_at

    def test_a_minute_window_catches_the_scheduled_time(self):
        race = self._race()
        target = race.close_at - timedelta(seconds=300)   # 5分前は予定に入っている
        prev = target - timedelta(seconds=30)
        now = target + timedelta(seconds=30)
        assert any(prev < t <= now for t in _due_times(race))

    def test_a_window_with_nothing_scheduled_fires_nothing(self):
        race = self._race()
        prev = race.close_at - timedelta(hours=5)
        now = prev + timedelta(seconds=60)
        assert not any(prev < t <= now for t in _due_times(race))

    def test_no_close_time_means_nothing_scheduled(self):
        assert _due_times(_race_for_cron("")) == []


def _race_for_cron(close_hhmm):
    return Race(
        race_id="202608134801", kaisai_date="20260813", jyo_cd="48", jyo="四日市",
        race_no=1, race_name="", tosu=7,
        close_at=_parse_hhmm("20260813", close_hhmm), start_at=None,
    )


class TestScheduleCache:
    """cron は毎分走るので、レース一覧を毎回取りにいってはいけない。"""

    def test_cache_is_used_without_touching_the_network(self, tmp_path):
        now = datetime(2026, 8, 13, 12, 0, tzinfo=JST)
        state = tmp_path / "state"
        state.mkdir()
        (state / "schedule_20260813.json").write_text(
            '{"fetched_at":"2026-08-13T11:55:00+09:00","races":'
            '[{"race_id":"202608134801","kaisai_date":"20260813","jyo_cd":"48",'
            '"jyo":"四日市","race_no":1,"race_name":"","tosu":7,'
            '"close_at":"2026-08-13T12:30:00+09:00","start_at":null}]}',
            encoding="utf-8",
        )

        class Boom:
            def __getattr__(self, name):
                raise AssertionError("キャッシュがあるのに通信した")

        races = _cached_schedule(Boom(), tmp_path, "20260813", now)
        assert len(races) == 1
        assert races[0].close_at == datetime(2026, 8, 13, 12, 30, tzinfo=JST)

    def test_stale_cache_is_refetched(self, tmp_path):
        """開催情報は当日でも更新される(欠車など)ので、古いまま使い続けない。"""
        now = datetime(2026, 8, 13, 12, 0, tzinfo=JST)
        state = tmp_path / "state"
        state.mkdir()
        (state / "schedule_20260813.json").write_text(
            '{"fetched_at":"2026-08-13T02:00:00+09:00","races":[]}', encoding="utf-8"
        )

        class Failing:
            def calendar(self, year):
                raise NetkeirinError("offline")

        # 10時間前のキャッシュは使わず取り直しにいく(通信に失敗するので空が返る)
        assert _cached_schedule(Failing(), tmp_path, "20260813", now) == []

    def test_corrupt_cache_is_refetched_not_crashed(self, tmp_path):
        now = datetime(2026, 8, 13, 12, 0, tzinfo=JST)
        state = tmp_path / "state"
        state.mkdir()
        (state / "schedule_20260813.json").write_text("{ broken", encoding="utf-8")

        class Failing:
            def calendar(self, year):
                raise NetkeirinError("offline")

        assert _cached_schedule(Failing(), tmp_path, "20260813", now) == []
