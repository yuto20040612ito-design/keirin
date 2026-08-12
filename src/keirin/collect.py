"""Phase 0: オッズ時系列の収集。

このプロジェクトで唯一「後から取り返しがつかない」工程。
レース結果や出走表は後から遡れるが、締切前オッズの履歴はどこにも売っていない。
モデルを書く前に、まずこれを動かし続けること。

使い方:

    # その年の開催カレンダーを取得 (1リクエスト)
    python -m keirin.collect calendar --year 2026

    # ある日のレース一覧(締切時刻つき)を確認
    python -m keirin.collect plan --date 20260811

    # 収集本体。締切前オッズを刻んで貯める
    python -m keirin.collect watch

    # 確定した結果・払戻をバックフィル
    python -m keirin.collect results --date 20260811
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import keirinjp, rawstore
from .manifest import Manifest
from .netkeirin import NetkeirinClient, NetkeirinError, parse_line_forecast

# 日本標準時は年間を通じて UTC+9 で、サマータイムが無い。
# 固定オフセットで完全に正しく、zoneinfo (Python 3.9+) に依存せずに済む。
# 共用レンタルサーバーは Python が古いことがあるので、依存は少ないほどよい。
JST = timezone(timedelta(hours=9))
DEFAULT_DATA_ROOT = Path("data")

# 締切の何秒前にオッズを取るか。締切直前ほど密に刻む。
# 実際に買える価格は締切直前のものなので、そこの解像度が最も重要。
POLL_OFFSETS_SEC = [
    3600, 2700, 1800, 1200, 900, 600, 420, 300, 240, 180, 120, 90, 60, 40, 25, 15,
]

# 締切をまたいだ直後に最終オッズを取りにいく猶予
POST_CLOSE_GRAB_SEC = 20

log = logging.getLogger("keirin.collect")


@dataclass
class Race:
    race_id: str
    kaisai_date: str
    jyo_cd: str
    jyo: str
    race_no: int
    race_name: str
    tosu: int
    close_at: datetime | None
    start_at: datetime | None

    @property
    def is_pollable(self) -> bool:
        return self.close_at is not None


def _parse_hhmm(kaisai_date: str, hhmm: str) -> datetime | None:
    """'20260811' + '10:40' -> JST aware datetime。

    深夜帯(ミッドナイト競輪)は締切が翌日 00:xx になることがあるので、
    レース番号順で時刻が巻き戻ったら日跨ぎとして扱う必要がある。
    ここでは単純にその日の時刻として返し、日跨ぎ補正は呼び出し側で行う。
    """
    if not hhmm or ":" not in hhmm:
        return None
    try:
        hh, mm = (int(x) for x in hhmm.split(":")[:2])
        d = datetime.strptime(kaisai_date, "%Y%m%d").date()
        return datetime(d.year, d.month, d.day, hh % 24, mm, tzinfo=JST) + timedelta(
            days=hh // 24
        )
    except (ValueError, TypeError):
        return None


def _fix_midnight_rollover(races: list[Race]) -> None:
    """R番号順に時刻が巻き戻ったら翌日扱いにする(ミッドナイト競輪対策)。

    必ず場ごとに評価する。R番号は場をまたいで重複するので、
    全場を混ぜて比較すると正常な時刻まで翌日にずらしてしまう。
    """
    by_jyo: dict[str, list[Race]] = {}
    for r in races:
        by_jyo.setdefault(r.jyo_cd, []).append(r)
    for group in by_jyo.values():
        prev: datetime | None = None
        for r in sorted(group, key=lambda x: x.race_no):
            if r.close_at is None:
                continue
            if prev is not None and r.close_at < prev:
                r.close_at += timedelta(days=1)
                if r.start_at is not None:
                    r.start_at += timedelta(days=1)
            prev = r.close_at


def fetch_calendar(client: NetkeirinClient, root: Path, year: int) -> list[dict]:
    payload = client.calendar(year)
    rawstore.append(root, "AplKaisai", f"{year}0101", {"year": str(year)}, payload)
    return payload if isinstance(payload, list) else []


def jyo_for_date(calendar: list[dict], target: str) -> list[tuple[str, str]]:
    """カレンダーから対象日の (jyo_cd, jyo名) を取り出す。"""
    out: list[tuple[str, str]] = []
    for day in calendar:
        if day.get("kaisai_date") != target:
            continue
        for item in day.get("list", []):
            cd, name = item.get("jyo_cd"), item.get("jyo", "")
            if cd:
                out.append((cd, name))
    return out


def fetch_races(
    client: NetkeirinClient, root: Path, kaisai_date: str, jyo_cd: str
) -> list[Race]:
    """指定日の全レースを取得する。

    AplRace は syusai に何を渡してもその日の**全場**を返す(実測で確認済み)。
    したがって場ごとに呼ぶ必要はなく、1日1リクエストで足りる。
    """
    payload = client.races(kaisai_date, jyo_cd)
    rawstore.append(
        root,
        "AplRace",
        kaisai_date,
        {"kaisai_date": kaisai_date, "syusai": jyo_cd},
        payload,
    )
    races: list[Race] = []
    blocks = payload if isinstance(payload, list) else []
    for block in blocks:
        for r in block.get("list", []):
            rid = r.get("race_id")
            if not rid:
                continue
            kd = r.get("kaisai_date", kaisai_date)
            races.append(
                Race(
                    race_id=rid,
                    kaisai_date=kd,
                    jyo_cd=r.get("jyo_cd", jyo_cd),
                    jyo=r.get("jyo", ""),
                    race_no=int(rid[-2:]),
                    race_name=r.get("race_name", ""),
                    tosu=int(r.get("tosu") or 0),
                    close_at=_parse_hhmm(kd, r.get("close", "")),
                    start_at=_parse_hhmm(kd, r.get("start", "")),
                )
            )
    # API が将来1場だけ返すようになっても壊れないよう race_id で重複排除しておく
    unique = {r.race_id: r for r in races}
    races = list(unique.values())
    _fix_midnight_rollover(races)
    return races


def load_day(
    client: NetkeirinClient, root: Path, kaisai_date: str, calendar: list[dict]
) -> list[Race]:
    """指定日の全レースを読む。AplRace は1回で全場返るので1リクエストで済む。"""
    venues = jyo_for_date(calendar, kaisai_date)
    if not venues:
        log.info("%s: no kaisai in calendar", kaisai_date)
        return []
    try:
        races = fetch_races(client, root, kaisai_date, venues[0][0])
    except NetkeirinError as exc:
        log.error("failed to load races for %s: %s", kaisai_date, exc)
        return []
    by_jyo: dict[str, int] = {}
    for r in races:
        by_jyo[r.jyo] = by_jyo.get(r.jyo, 0) + 1
    log.info(
        "%s: %d races / %d venues (%s)",
        kaisai_date,
        len(races),
        len(by_jyo),
        ", ".join(f"{k}{v}R" for k, v in by_jyo.items()),
    )
    return races


# ---------------------------------------------------------------------------
# 収集本体
# ---------------------------------------------------------------------------


class OddsCollector:
    """締切前オッズを刻んで raw 層に貯める。

    重複排除はオッズ本体のハッシュで行う。
    `official_dt` は締切前は空文字で、オッズが公式に確定してから初めて値が入る(実測)。
    そのため official_dt では締切前のスナップショットを区別できない。
    市場が動いていないのに行だけ増やしてもディスクを食うだけなので、内容で比較する。
    """

    def __init__(self, client: NetkeirinClient, root: Path) -> None:
        self.client = client
        self.root = root
        self._last_digest: dict[str, str] = {}
        self._static_done: set[str] = set()

    def collect_static(self, race: Race) -> None:
        """ライン構成と出走選手。締切前に一度だけ取れば足りる。"""
        if race.race_id in self._static_done:
            return
        for name, fn in (
            ("AplNarabiYoso", lambda: self.client.narabi(race.race_id)),
            ("AplRaceHorse", lambda: self.client.entries(race.race_id)),
        ):
            try:
                payload = fn()
            except NetkeirinError as exc:
                log.warning("%s %s failed: %s", race.race_id, name, exc)
                continue
            rawstore.append(
                self.root, name, race.kaisai_date, {"race_id": race.race_id}, payload
            )
            if name == "AplNarabiYoso":
                lines = parse_line_forecast(payload)
                log.info(
                    "%s line: %s",
                    race.race_id,
                    " / ".join("-".join(map(str, ln)) for ln in lines) or "(未確定)",
                )
        self._static_done.add(race.race_id)

    def collect_odds(self, race: Race, now: datetime) -> bool:
        """オッズを1回取得。新しいスナップショットを保存したら True。"""
        try:
            payload = self.client.odds(race.race_id)
        except NetkeirinError as exc:
            log.warning("%s odds failed: %s", race.race_id, exc)
            return False

        digest = hashlib.sha1(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        if self._last_digest.get(race.race_id) == digest:
            return False
        self._last_digest[race.race_id] = digest

        secs_to_close = (
            int((race.close_at - now).total_seconds()) if race.close_at else None
        )
        rawstore.append(
            self.root,
            "AplRaceOdds",
            race.kaisai_date,
            {
                "race_id": race.race_id,
                "close_at": race.close_at.isoformat() if race.close_at else None,
                "secs_to_close": secs_to_close,
                "snapshot_at": now.isoformat(),
            },
            payload,
        )
        log.info(
            "%s odds saved official_dt=%s (T%+ds)",
            race.race_id,
            payload.get("official_dt") or "(未確定)",
            secs_to_close if secs_to_close is not None else 0,
        )
        return True


def _due_times(race: Race) -> list[datetime]:
    """このレースでオッズを取りにいく時刻の一覧。"""
    if race.close_at is None:
        return []
    times = [race.close_at - timedelta(seconds=s) for s in POLL_OFFSETS_SEC]
    times.append(race.close_at + timedelta(seconds=POST_CLOSE_GRAB_SEC))
    return sorted(times)


def watch(
    client: NetkeirinClient,
    root: Path,
    target_date: str | None = None,
    lookahead_sec: int = max(POLL_OFFSETS_SEC),
) -> None:
    """収集ループ。日付が変わったらその日の開催を読み直す。"""
    collector = OddsCollector(client, root)
    calendar_year: int | None = None
    calendar: list[dict] = []
    loaded_date: str | None = None
    races: list[Race] = []
    pending: list[tuple[datetime, Race]] = []

    while True:
        now = datetime.now(JST)
        today = target_date or now.strftime("%Y%m%d")

        if calendar_year != now.year:
            try:
                calendar = fetch_calendar(client, root, now.year)
                calendar_year = now.year
                log.info("calendar loaded: %d kaisai days in %d", len(calendar), now.year)
            except NetkeirinError as exc:
                log.error("calendar fetch failed: %s -- retry in 60s", exc)
                time.sleep(60)
                continue

        if loaded_date != today:
            races = load_day(client, root, today, calendar)
            loaded_date = today
            pending = sorted(
                ((t, r) for r in races for t in _due_times(r)), key=lambda x: x[0]
            )
            # すでに過ぎた予定は捨てる(起動が遅れた場合)
            pending = [(t, r) for t, r in pending if t > now - timedelta(seconds=120)]
            log.info("%s: %d races, %d scheduled fetches", today, len(races), len(pending))
            if not races:
                log.info("no races today; sleeping")

        # 締切が近いレースの静的情報(ライン/出走)を先に押さえる
        for r in races:
            if r.close_at and 0 < (r.close_at - now).total_seconds() <= lookahead_sec:
                collector.collect_static(r)

        if not pending:
            # 次の日付境界まで待つ(最大10分刻みで起き直す)
            time.sleep(min(600, _secs_to_midnight(now)))
            if target_date:
                log.info("target date finished; exiting")
                return
            continue

        due_at, race = pending[0]
        wait = (due_at - datetime.now(JST)).total_seconds()
        if wait > 0:
            time.sleep(min(wait, 600))
            if (due_at - datetime.now(JST)).total_seconds() > 1:
                continue  # まだ早い。ループ先頭で日付変更などを再評価
        pending.pop(0)
        collector.collect_odds(race, datetime.now(JST))


# ---------------------------------------------------------------------------
# cron 方式 (共用レンタルサーバー向け)
# ---------------------------------------------------------------------------
#
# 共用サーバーでは常駐プロセスが禁止・強制終了される。1分ごとに cron で叩き、
# 「前回実行から今までの間に予定時刻を過ぎたレース」だけ取って終了する。
#
# 状態は last_run の時刻ひとつだけ。プロセスが落ちても次の起動で続きから拾える。
# ただし cron が止まればその間の予定は丸ごと飛ぶので、status での確認は必須。


def _state_dir(root: Path) -> Path:
    d = Path(root) / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read_last_run(root: Path, now: datetime) -> datetime:
    path = _state_dir(root) / "last_run.txt"
    if path.exists():
        try:
            return datetime.fromisoformat(path.read_text().strip())
        except ValueError:
            pass
    # 初回は直近2分ぶんだけ見る。過去に遡って一気に取りにいかない。
    return now - timedelta(seconds=120)


def _write_last_run(root: Path, now: datetime) -> None:
    (_state_dir(root) / "last_run.txt").write_text(now.isoformat())


def _cached_schedule(
    client: NetkeirinClient, root: Path, date_str: str, now: datetime,
    max_age_sec: int = 1800,
) -> list[Race]:
    """その日のレース一覧。cron は毎分走るので必ずキャッシュする。

    毎回 API を叩くと1日1440リクエストになり、取りたいオッズの邪魔にしかならない。
    """
    cache = _state_dir(root) / f"schedule_{date_str}.json"
    if cache.exists():
        try:
            doc = json.loads(cache.read_text())
            # 鮮度はファイルの mtime ではなく中身の取得時刻で見る。
            # mtime はコピーやリストアで簡単に変わってしまう。
            fetched = datetime.fromisoformat(doc["fetched_at"])
            raw = doc["races"] if (now - fetched).total_seconds() < max_age_sec else None
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            raw = None
        if raw is not None:
            try:
                return [
                    Race(
                        race_id=r["race_id"], kaisai_date=r["kaisai_date"],
                        jyo_cd=r["jyo_cd"], jyo=r["jyo"], race_no=r["race_no"],
                        race_name=r["race_name"], tosu=r["tosu"],
                        close_at=datetime.fromisoformat(r["close_at"]) if r["close_at"] else None,
                        start_at=datetime.fromisoformat(r["start_at"]) if r["start_at"] else None,
                    )
                    for r in raw
                ]
            except (KeyError, ValueError, TypeError):
                pass  # 壊れていたら取り直す

    try:
        calendar = fetch_calendar(client, root, int(date_str[:4]))
        races = load_day(client, root, date_str, calendar)
    except NetkeirinError as exc:
        log.error("schedule fetch failed: %s", exc)
        return []

    cache.write_text(json.dumps({
        "fetched_at": now.isoformat(),
        "races": [
            {
                "race_id": r.race_id, "kaisai_date": r.kaisai_date, "jyo_cd": r.jyo_cd,
                "jyo": r.jyo, "race_no": r.race_no, "race_name": r.race_name,
                "tosu": r.tosu,
                "close_at": r.close_at.isoformat() if r.close_at else None,
                "start_at": r.start_at.isoformat() if r.start_at else None,
            }
            for r in races
        ],
    }, ensure_ascii=False))
    return races


def run_once(client: NetkeirinClient, root: Path) -> int:
    """cron から1分ごとに呼ばれる想定。予定を過ぎたぶんだけ取って終了する。"""
    now = datetime.now(JST)
    last_run = _read_last_run(root, now)

    # ミッドナイト競輪は締切が翌日 00:xx になる。早朝は前日ぶんも見る。
    dates = [now.strftime("%Y%m%d")]
    if now.hour < 2:
        dates.append((now - timedelta(days=1)).strftime("%Y%m%d"))

    races: list[Race] = []
    for date_str in dates:
        races.extend(_cached_schedule(client, root, date_str, now))
    if not races:
        _write_last_run(root, now)
        log.info("no races scheduled")
        return 0

    collector = OddsCollector(client, root)
    man = Manifest(root)
    # 静的情報は取得済みを索引から復元する(プロセスをまたぐので毎回リセットされる)
    collector._static_done = set(man.done("AplNarabiYoso"))

    n_odds = 0
    for race in races:
        if race.close_at is None:
            continue
        # 前回実行から今までの間に予定時刻を過ぎていれば取る
        if any(last_run < t <= now for t in _due_times(race)):
            if collector.collect_odds(race, now):
                n_odds += 1
        # 締切1時間前を切ったらライン・出走を一度だけ
        secs = (race.close_at - now).total_seconds()
        if 0 < secs <= max(POLL_OFFSETS_SEC) and not man.has("AplNarabiYoso", race.race_id):
            collector.collect_static(race)
            man.mark("AplNarabiYoso", race.race_id)

    _write_last_run(root, now)
    log.info("once: %d races known, %d odds snapshots saved", len(races), n_odds)
    return 0


def _secs_to_midnight(now: datetime) -> float:
    nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
    return max(30.0, (nxt - now).total_seconds())


# ---------------------------------------------------------------------------
# 結果のバックフィル
# ---------------------------------------------------------------------------


def collect_results(
    client: NetkeirinClient, root: Path, kaisai_date: str, calendar: list[dict]
) -> int:
    """確定後の結果ページ HTML を raw に保存する。パースは load 側で行う。"""
    races = load_day(client, root, kaisai_date, calendar)
    saved = 0
    for race in races:
        try:
            html = client.get_html("race/result/", race_id=race.race_id)
        except NetkeirinError as exc:
            log.warning("%s result failed: %s", race.race_id, exc)
            continue
        rawstore.append(
            root, "result_html", kaisai_date, {"race_id": race.race_id}, html
        )
        saved += 1
    log.info("%s: saved %d result pages", kaisai_date, saved)
    return saved


# ---------------------------------------------------------------------------
# Phase 1: 過去データのバックフィル
# ---------------------------------------------------------------------------

# 過去レースで取得できるもの。オッズは確定オッズが1点だけ返る。
# 締切前の推移は遡れないが、確定オッズは市場の最終評価そのものなので
# Phase 2 のベースライン比較には十分使える。
BACKFILL_KINDS = (
    "AplRaceHorse",    # 車番/枠番/級班/競走得点 (JSON)
    "AplNarabiYoso",   # ライン構成 (JSON)
    "AplRaceOdds",     # 確定オッズ (JSON)
    "entry_html",      # 脚質/SHB/決まり手構成/ギヤ倍数 (HTML。JSON API に無い)
    "result_html",     # 着順/決まり手/払戻 (HTML)
)


def _fetch_one(
    client: NetkeirinClient, root: Path, kind: str, race: Race
) -> bool:
    try:
        if kind == "result_html":
            payload = client.get_html("race/result/", race_id=race.race_id)
        elif kind == "entry_html":
            payload = client.get_html("race/entry/", race_id=race.race_id)
        elif kind == "AplRaceHorse":
            payload = client.entries(race.race_id)
        elif kind == "AplNarabiYoso":
            payload = client.narabi(race.race_id)
        elif kind == "AplRaceOdds":
            payload = client.odds(race.race_id)
        else:
            raise ValueError(f"unknown kind: {kind}")
    except NetkeirinError as exc:
        log.warning("%s %s failed: %s", race.race_id, kind, exc)
        return False

    params: dict = {"race_id": race.race_id}
    if kind == "AplRaceOdds":
        params["close_at"] = race.close_at.isoformat() if race.close_at else None
    rawstore.append(root, kind, race.kaisai_date, params, payload)
    return True


def backfill(
    client: NetkeirinClient,
    root: Path,
    date_from: str,
    date_to: str,
    kinds: tuple[str, ...] = BACKFILL_KINDS,
) -> None:
    """期間内の過去データを収集する。中断しても再開できる。

    1レースあたり len(kinds) リクエスト。全場だと1日およそ80レースなので、
    1.5秒間隔・4種で1日分に約8分かかる。1年分なら数十時間の作業になる。
    夜間に流し続ける前提で、いつ止めても続きから再開できるようにしてある。
    """
    man = Manifest(root)
    start = datetime.strptime(date_from, "%Y%m%d").date()
    end = datetime.strptime(date_to, "%Y%m%d").date()
    if start > end:
        raise ValueError("date_from must not be after date_to")

    calendars: dict[int, list[dict]] = {}
    total_done = total_skipped = 0
    day = start
    while day <= end:
        date_str = day.strftime("%Y%m%d")
        day += timedelta(days=1)

        year = int(date_str[:4])
        if year not in calendars:
            try:
                calendars[year] = fetch_calendar(client, root, year)
            except NetkeirinError as exc:
                log.error("calendar %d failed: %s -- skipping year", year, exc)
                calendars[year] = []

        if not jyo_for_date(calendars[year], date_str):
            continue  # 非開催日

        # 完了マーカーは kinds ごとに分ける。日付だけで持つと、--kinds を絞って
        # 収集した日を後から別の kinds で埋め直せなくなる。
        date_key = f"{date_str}|{'+'.join(sorted(kinds))}"
        if man.has("dates", date_key):
            log.info("%s: already complete for %s, skipping", date_str, ",".join(kinds))
            continue

        races = load_day(client, root, date_str, calendars[year])
        if not races:
            continue

        day_done = 0
        for race in races:
            for kind in kinds:
                if man.has(kind, race.race_id):
                    total_skipped += 1
                    continue
                if _fetch_one(client, root, kind, race):
                    man.mark(kind, race.race_id)
                    day_done += 1
                    total_done += 1
        man.mark("dates", date_key)
        log.info(
            "%s: %d races, %d fetched (total %d fetched / %d skipped)",
            date_str,
            len(races),
            day_done,
            total_done,
            total_skipped,
        )


# ---------------------------------------------------------------------------
# 稼働確認
# ---------------------------------------------------------------------------


def _last_fetch(root: Path, api_class: str) -> datetime | None:
    """そのクラスで最後に何かを取れた時刻。

    パーティションは開催日で切ってあるので、日付の新しい順に見ると誤る。
    バックフィルは過去日のパーティションに「今」書き込むため、
    日付順の最後が最新の書き込みとは限らない。
    実際に最後に書かれたファイル(mtime が最大)を見る。
    """
    base = root / "raw" / api_class
    if not base.exists():
        return None
    parts = list(base.glob("dt=*/part.jsonl.gz"))
    if not parts:
        return None
    newest = max(parts, key=lambda p: p.stat().st_mtime)
    last = None
    for rec in rawstore.read(newest):
        ts = rec.get("fetched_at")
        if ts:
            last = ts
    if last is None:
        return None
    try:
        return datetime.fromisoformat(last).astimezone(JST)
    except ValueError:
        return None


def print_status(client: NetkeirinClient, root: Path, days: int) -> int:
    """収集が生きているかを、取れたデータそのものから確認する。

    プロセスの生死ではなくデータの新しさを見る。プロセスが生きていても
    取れていなければ意味がなく、逆に落ちても再起動されていれば問題ない。
    セッションやマシンの再起動で静かに止まるのが一番怖い失敗なので、
    「最後にオッズが取れたのはいつか」を一目で出す。
    """
    now = datetime.now(JST)
    print(f"\n  現在時刻 {now:%Y-%m-%d %H:%M} (JST)\n")

    print("  最後に取得できた時刻:")
    stale = False
    for api_class, label, warn_after_h in (
        ("AplRaceOdds", "オッズ", 24),
        ("AplNarabiYoso", "ライン", 24),
        ("AplRace", "レース一覧", 48),
    ):
        last = _last_fetch(root, api_class)
        if last is None:
            print(f"    {label:12} まだ1件も無い")
            stale = True
            continue
        age_h = (now - last).total_seconds() / 3600
        mark = "  ← 古い" if age_h > warn_after_h else ""
        stale = stale or age_h > warn_after_h
        print(f"    {label:12} {last:%Y-%m-%d %H:%M}  ({age_h:.1f}時間前){mark}")

    print("\n  日別のオッズ収集レース数 (開催日ごと):")
    base = root / "raw" / "AplRaceOdds"
    seen = 0
    if base.exists():
        for part in sorted(base.glob("dt=*/part.jsonl.gz"), reverse=True)[:days]:
            date_str = part.parent.name.split("=", 1)[-1]
            races, snaps = set(), 0
            for rec in rawstore.read(part):
                rid = (rec.get("params") or {}).get("race_id")
                if rid:
                    races.add(rid)
                    snaps += 1
            print(f"    {date_str}  {len(races):4d} レース / {snaps:6d} スナップショット")
            seen += 1
    if not seen:
        print("    まだ無い")

    try:
        cal = fetch_calendar(client, root, now.year)
        today = now.strftime("%Y%m%d")
        venues = jyo_for_date(cal, today)
        print(f"\n  本日 {today} の開催: {len(venues)} 場")
        if venues:
            print("    " + ", ".join(name for _, name in venues))
    except NetkeirinError as exc:
        print(f"\n  開催カレンダーの取得に失敗: {exc}")

    if stale:
        print("\n  → 収集が止まっている可能性がある。watch が動いているか確認すること。")
        print("     systemd なら: systemctl status keirin-collect")
    else:
        print("\n  → 収集は動いているように見える。")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="keirin.collect", description=__doc__)
    p.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    p.add_argument("--min-interval", type=float, default=1.5, help="リクエスト間隔(秒)")
    p.add_argument("--verbose", "-v", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("calendar", help="年間開催カレンダーを取得")
    c.add_argument("--year", type=int, default=date.today().year)

    pl = sub.add_parser("plan", help="指定日のレース一覧と締切時刻を表示")
    pl.add_argument("--date", required=True, help="YYYYMMDD")

    w = sub.add_parser("watch", help="オッズ収集ループ (常駐。VPS/自宅サーバー向け)")
    w.add_argument("--date", help="YYYYMMDD。省略時は当日を追い続ける")

    sub.add_parser(
        "once",
        help="1回だけ収集して終了 (cron 向け。共用レンタルサーバーはこちら)",
    )

    rs = sub.add_parser("results", help="確定結果ページをバックフィル")
    rs.add_argument("--date", required=True, help="YYYYMMDD")

    bf = sub.add_parser("backfill", help="過去データを収集 (Phase 1。中断・再開可)")
    bf.add_argument("--from", dest="date_from", required=True, help="YYYYMMDD")
    bf.add_argument("--to", dest="date_to", required=True, help="YYYYMMDD")
    bf.add_argument(
        "--kinds",
        default=",".join(BACKFILL_KINDS),
        help=f"収集対象をカンマ区切りで指定 (既定: {','.join(BACKFILL_KINDS)})",
    )

    rb = sub.add_parser("rebuild-manifest", help="raw から収集済み索引を作り直す")
    rb.add_argument("--kinds", default=",".join(BACKFILL_KINDS))

    sub.add_parser("velodromes", help="公式からバンク諸元を取得 (年1回で足りる)")

    st = sub.add_parser("status", help="収集が生きているか確認する")
    st.add_argument("--days", type=int, default=7, help="遡って確認する日数")

    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )
    client = NetkeirinClient(min_interval=args.min_interval)
    root = args.data_root

    if args.cmd == "calendar":
        cal = fetch_calendar(client, root, args.year)
        print(f"{args.year}: {len(cal)} kaisai days saved to {root}/raw/AplKaisai/")
        return 0

    if args.cmd == "plan":
        cal = fetch_calendar(client, root, int(args.date[:4]))
        races = load_day(client, root, args.date, cal)
        for r in sorted(races, key=lambda x: (x.close_at or datetime.max.replace(tzinfo=JST))):
            close = r.close_at.strftime("%m/%d %H:%M") if r.close_at else "     ?    "
            print(f"{r.race_id}  {close}  {r.jyo:6} {r.race_no:2}R  {r.tosu}車  {r.race_name}")
        print(f"\n{len(races)} races on {args.date}")
        return 0

    if args.cmd == "once":
        return run_once(client, root)

    if args.cmd == "watch":
        try:
            watch(client, root, target_date=args.date)
        except KeyboardInterrupt:
            log.info("interrupted")
        return 0

    if args.cmd == "results":
        cal = fetch_calendar(client, root, int(args.date[:4]))
        collect_results(client, root, args.date, cal)
        return 0

    if args.cmd == "backfill":
        kinds = tuple(k.strip() for k in args.kinds.split(",") if k.strip())
        unknown = set(kinds) - set(BACKFILL_KINDS)
        if unknown:
            p.error(f"unknown kinds: {', '.join(sorted(unknown))}")
        try:
            backfill(client, root, args.date_from, args.date_to, kinds)
        except KeyboardInterrupt:
            log.info("interrupted -- rerun the same command to resume")
        return 0

    if args.cmd == "status":
        return print_status(client, root, args.days)

    if args.cmd == "velodromes":
        codes = keirinjp.list_velodrome_codes(client)
        log.info("%d velodromes", len(codes))
        saved = 0
        for code in codes:
            try:
                markup = keirinjp.fetch_velodrome(client, code)
            except NetkeirinError as exc:
                log.warning("jocd=%s failed: %s", code, exc)
                continue
            rawstore.append(
                root, "velodrome_html", datetime.now(JST).strftime("%Y%m%d"),
                {"jyo_cd": code}, markup,
            )
            saved += 1
        log.info("saved %d/%d velodromes", saved, len(codes))
        return 0

    if args.cmd == "rebuild-manifest":
        kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
        for kind, n in Manifest(root).rebuild(kinds).items():
            log.info("%-15s %6d keys", kind, n)
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
