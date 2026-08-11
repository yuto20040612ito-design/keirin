"""Raw 層 -> DuckDB。

Raw は不変・追記のみ。このモジュールは何度実行しても同じ結果になる(冪等)。
パース仕様を変えたくなったら、DB を捨てて raw から作り直せばよい。

使い方:
    python -m keirin.load --data-root data --db data/keirin.duckdb
"""

from __future__ import annotations

import argparse
import html
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

import duckdb

from . import rawstore
from .netkeirin import iter_odds_rows, parse_line_forecast

log = logging.getLogger("keirin.load")

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "sql" / "schema.sql"


def _int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _float(v):
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _ts(v: str | None):
    """'2026-08-11 10:46:00' -> datetime。空文字や不正値は None。"""
    if not v:
        return None
    try:
        return datetime.strptime(str(v).strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _iso(v: str | None):
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v))
    except ValueError:
        return None


def init_db(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(SCHEMA_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# races
# ---------------------------------------------------------------------------


def load_races(con, root: Path) -> int:
    rows = []
    for rec in rawstore.iter_class(root, "AplRace"):
        for block in rec.get("payload") or []:
            for r in block.get("list", []):
                rid = r.get("race_id")
                if not rid:
                    continue
                kd = r.get("kaisai_date") or rid[:8]
                jyoken = r.get("jyoken") or ""
                name = r.get("race_name") or ""
                rows.append(
                    (
                        rid,
                        datetime.strptime(kd, "%Y%m%d").date(),
                        r.get("jyo_cd") or rid[8:10],
                        _int(rid[-2:]),
                        name,
                        jyoken,
                        None,  # grade: 開催情報から後で補完
                        _int(r.get("tosu")),
                        _int(r.get("kyori")),
                        None,  # laps
                        _hhmm_to_ts(kd, r.get("start")),
                        _hhmm_to_ts(kd, r.get("close")),
                        _int(r.get("nichiji")),
                        str(r.get("last_day_flg")) == "1",
                        r.get("tenko") or None,
                        None,
                        None,
                        None,
                        r.get("race_status") or None,
                        "ガールズ" in name or "Ｌ級" in jyoken,
                        None,  # is_midnight: 締切時刻から後段で判定
                        "netkeirin",
                        _iso(rec.get("fetched_at")),
                    )
                )
    if not rows:
        return 0
    con.executemany(
        """INSERT OR REPLACE INTO races VALUES
           (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    # ミッドナイトは締切が概ね 20:30 以降。時刻から判定する。
    con.execute(
        "UPDATE races SET is_midnight = (close_at IS NOT NULL AND hour(close_at) >= 20)"
    )
    return len(rows)


def _hhmm_to_ts(kaisai_date: str, hhmm: str | None):
    if not hhmm or ":" not in str(hhmm):
        return None
    try:
        hh, mm = (int(x) for x in str(hhmm).split(":")[:2])
        d = datetime.strptime(kaisai_date, "%Y%m%d")
        return d + timedelta(hours=hh, minutes=mm)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# entries / lines / odds
# ---------------------------------------------------------------------------


def load_entries(con, root: Path) -> int:
    rows = []
    for rec in rawstore.iter_class(root, "AplRaceHorse"):
        fetched = _iso(rec.get("fetched_at"))
        for e in rec.get("payload") or []:
            rid = e.get("race_id")
            syaban = _int(e.get("syaban"))
            if not rid or syaban is None:
                continue
            rows.append(
                (
                    rid,
                    syaban,
                    _int(e.get("wakuban")),
                    None,  # player_id: HTML 側から補完
                    e.get("name"),
                    None,
                    e.get("fuken"),
                    _int(e.get("age")),
                    _int(e.get("graduate")),
                    e.get("kyu"),
                    e.get("han"),
                    _float(e.get("rating")),
                    fetched,
                )
            )
    if not rows:
        return 0
    con.executemany(
        """INSERT OR REPLACE INTO entries
           (race_id, syaban, wakuban, player_id, player_name, player_kana,
            prefecture, age, graduate_period, kyu, han, rating, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    return len(rows)


def load_lines(con, root: Path) -> int:
    rows = []
    for api_class, source in (
        ("AplNarabiYoso", "AplNarabiYoso"),
        ("AplNarabiYoso2", "AplNarabiYoso2"),
    ):
        for rec in rawstore.iter_class(root, api_class):
            rid = (rec.get("params") or {}).get("race_id")
            if not rid:
                continue
            fetched = _iso(rec.get("fetched_at"))
            for line_no, line in enumerate(
                parse_line_forecast(rec.get("payload") or {}), start=1
            ):
                size = len(line)
                for pos, syaban in enumerate(line, start=1):
                    rows.append(
                        (rid, line_no, pos, syaban, size, size == 1, source, fetched)
                    )
    if not rows:
        return 0
    con.executemany(
        "INSERT OR REPLACE INTO race_lines VALUES (?,?,?,?,?,?,?,?)", rows
    )
    return len(rows)


def load_odds(con, root: Path) -> int:
    """オッズ時系列を投入する。

    official_dt は締切前は空なので、スナップショットの時刻は
    snapshot_at (収集時の時刻) を優先し、無ければ fetched_at で代用する。
    """
    rows = []
    for rec in rawstore.iter_class(root, "AplRaceOdds"):
        params = rec.get("params") or {}
        rid = params.get("race_id")
        payload = rec.get("payload") or {}
        if not rid or not isinstance(payload, dict):
            continue

        fetched = _iso(rec.get("fetched_at"))
        official = _ts(payload.get("official_dt"))
        snapshot = _iso(params.get("snapshot_at")) or fetched
        if snapshot is None:
            continue

        close_at = _iso(params.get("close_at"))
        if close_at is not None:
            secs = int((close_at - _align_tz(snapshot, close_at)).total_seconds())
        else:
            secs = _int(params.get("secs_to_close"))

        snapshot_naive = snapshot.replace(tzinfo=None)
        fetched_naive = fetched.replace(tzinfo=None) if fetched else snapshot_naive
        for bet_type, combo, low, high, pop in iter_odds_rows(payload):
            rows.append(
                (
                    rid,
                    bet_type,
                    combo,
                    snapshot_naive,
                    official,
                    official is not None,
                    low,
                    high if bet_type == 7 else None,
                    pop,
                    fetched_naive,
                    secs,
                )
            )
    if not rows:
        return 0
    con.executemany(
        "INSERT OR REPLACE INTO odds_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows
    )
    return len(rows)


def _align_tz(dt: datetime, ref: datetime) -> datetime:
    """dt を ref と同じ tz-aware / naive の状態に揃える。"""
    if ref.tzinfo is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=ref.tzinfo)
    if ref.tzinfo is None and dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


# ---------------------------------------------------------------------------
# 結果 HTML のパース
# ---------------------------------------------------------------------------

_TAG = re.compile(r"<[^>]+>")
_KIMARITE = {"逃", "捲", "差", "マ", "ク"}


def _cells(markup: str) -> list[list[str]]:
    """テーブルの行 -> セル文字列のリスト。

    HTML エンティティの復元は必須。2車単/3連単の組番は '5&gt;1' のように
    '>' がエスケープされて入っており、復元しないと順序付き賭式の払戻が丸ごと落ちる。
    """
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", markup, re.S):
        cells = []
        for td in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S):
            txt = _TAG.sub(" ", td)
            txt = html.unescape(txt)
            txt = txt.replace("\xa0", " ")
            txt = re.sub(r"\s+", " ", txt).strip()
            cells.append(txt)
        if cells:
            out.append(cells)
    return out


def load_results(con, root: Path) -> tuple[int, int]:
    """結果ページ HTML から results と payouts を作る。"""
    res_rows, pay_rows = [], []
    for rec in rawstore.iter_class(root, "result_html"):
        rid = (rec.get("params") or {}).get("race_id")
        html = rec.get("payload")
        if not rid or not isinstance(html, str):
            continue
        fetched = _iso(rec.get("fetched_at"))
        body = re.sub(r"<script.*?</script>|<style.*?</style>", "", html, flags=re.S)

        for tbl in re.findall(r"<table[^>]*>.*?</table>", body, re.S):
            if "ResultRefund" in tbl:
                res_rows.extend(_parse_result_table(tbl, rid, fetched))
            elif "Payout_Detail_Table" in tbl:
                pay_rows.extend(_parse_payout_table(tbl, rid, fetched))

    if res_rows:
        con.executemany(
            "INSERT OR REPLACE INTO results VALUES (?,?,?,?,?,?,?,?,?,?)", res_rows
        )
    if pay_rows:
        con.executemany(
            "INSERT OR REPLACE INTO payouts VALUES (?,?,?,?,?,?)", pay_rows
        )
    return len(res_rows), len(pay_rows)


# 着順欄が数字でない場合のマーカー。落車・失格の履歴は展開予測に効くので必ず残す。
_ABNORMAL = {"棄": "棄権", "失": "失格", "落": "落車", "欠": "欠車", "故": "故障"}


def _parse_result_table(tbl: str, race_id: str, fetched) -> list[tuple]:
    rows = []
    for cells in _cells(tbl):
        head = cells[0]
        m = re.fullmatch(r"(\d+)着", head)
        if m:
            pos, status = int(m.group(1)), "正常"
        elif head in _ABNORMAL:
            # 落車・失格・欠車。着順は付かないが行としては残す。
            pos, status = None, _ABNORMAL[head]
        else:
            continue

        nums = [c for c in cells if re.fullmatch(r"\d{1,2}", c)]
        if len(nums) < 2:
            continue
        syaban = int(nums[1])

        detail = next((c for c in cells if "(" in c or "（" in c), None)
        if detail:
            inner = re.search(r"[(（]([^)）]+)[)）]", detail)
            if inner:
                status = inner.group(1)

        last_lap = None
        for c in cells:
            if re.fullmatch(r"\d{1,2}\.\d", c):
                last_lap = float(c)
                break
        kimarite = next((c for c in cells if c in _KIMARITE), None)
        margin = next(
            (c for c in cells if re.search(r"(車身|車輪|タイヤ|大差|同着)", c)), None
        )
        got_s = any(c in ("S", "SB") for c in cells)
        got_b = any(c in ("B", "SB") for c in cells)
        rows.append(
            (race_id, syaban, pos, status, margin, last_lap, kimarite, got_s, got_b, fetched)
        )
    return rows


_BET_NAME_TO_CODE = {
    "２車複": 5, "2車複": 5,
    "２車単": 6, "2車単": 6,
    "ワイド": 7,
    "３連複": 8, "3連複": 8,
    "３連単": 9, "3連単": 9,
}


def _parse_payout_table(tbl: str, race_id: str, fetched) -> list[tuple]:
    rows = []
    current = None
    for cells in _cells(tbl):
        for i, c in enumerate(cells):
            if c in _BET_NAME_TO_CODE:
                current = _BET_NAME_TO_CODE[c]
                cells = cells[i + 1 :]
                break
        if current is None:
            continue
        combo = next((c for c in cells if re.fullmatch(r"\d+([->]\d+)*", c)), None)
        yen = next((c for c in cells if re.fullmatch(r"[\d,]+円", c)), None)
        pop = next((c for c in cells if re.fullmatch(r"\d+人気", c)), None)
        if not combo or not yen:
            continue
        parts = re.split(r"[->]", combo)
        norm = "".join(f"{int(p):02d}" for p in parts if p)
        rows.append(
            (
                race_id,
                current,
                norm,
                int(yen.replace(",", "").replace("円", "")),
                int(pop.replace("人気", "")) if pop else None,
                fetched,
            )
        )
    return rows


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="keirin.load", description=__doc__)
    p.add_argument("--data-root", type=Path, default=Path("data"))
    p.add_argument("--db", type=Path, default=Path("data/keirin.duckdb"))
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")

    args.db.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(args.db))
    init_db(con)

    load_races(con, args.data_root)
    load_entries(con, args.data_root)
    load_lines(con, args.data_root)
    load_odds(con, args.data_root)
    load_results(con, args.data_root)

    # 投入試行数ではなく実際のテーブル件数を出す(raw には同じレースが何度も入るため)
    for table in (
        "races",
        "entries",
        "race_lines",
        "odds_snapshots",
        "results",
        "payouts",
    ):
        n = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        log.info("%-15s %8d rows", table, n)
    snaps = con.execute(
        "SELECT count(DISTINCT (race_id, snapshot_at)) FROM odds_snapshots"
    ).fetchone()[0]
    log.info("%-15s %8d snapshots", "(odds)", snaps)
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
