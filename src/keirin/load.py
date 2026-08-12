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
from .keirinjp import parse_velodrome
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


def bulk_upsert(con, table: str, rows: list[tuple], columns: list[str]) -> int:
    """まとめて INSERT OR REPLACE する。

    executemany は DuckDB では1行ずつの実行になり、主キー付きテーブルに数十万行
    入れると実用にならない(オッズは1レース約700組あるのですぐそうなる)。
    Arrow 経由で一括投入すると桁で速くなる。

    pyarrow が無い環境では executemany に落ちる。収集(Phase 0)は標準ライブラリ
    だけで動く必要があるが、投入は analysis extra を入れる前提なので許容する。
    """
    if not rows:
        return 0
    placeholders = ",".join("?" * len(columns))
    col_list = ",".join(columns)
    try:
        import pyarrow as pa
    except ImportError:
        con.executemany(
            f"INSERT OR REPLACE INTO {table} ({col_list}) VALUES ({placeholders})", rows
        )
        return len(rows)

    try:
        arrays = [pa.array(col) for col in zip(*rows)]
        tbl = pa.Table.from_arrays(arrays, names=columns)
    except (pa.ArrowInvalid, pa.ArrowTypeError) as exc:
        # 型推論に失敗したら遅い経路で確実に入れる。取りこぼすよりは遅いほうがまし。
        log.warning("arrow conversion failed for %s (%s); falling back", table, exc)
        con.executemany(
            f"INSERT OR REPLACE INTO {table} ({col_list}) VALUES ({placeholders})", rows
        )
        return len(rows)

    con.register("_bulk", tbl)
    try:
        # 同一バッチ内に主キー重複があると ON CONFLICT が二重更新でこける。
        # raw は追記のみで同じレースが何度も入るため、ここで先に畳んでおく。
        pk = _PRIMARY_KEYS.get(table)
        src = "_bulk"
        if pk:
            keys = ",".join(pk)
            src = (
                f"(SELECT * EXCLUDE (_rn) FROM (SELECT *, "
                f"ROW_NUMBER() OVER (PARTITION BY {keys} ORDER BY 1) AS _rn "
                f"FROM _bulk) WHERE _rn = 1)"
            )
        con.execute(f"INSERT OR REPLACE INTO {table} ({col_list}) SELECT {col_list} FROM {src}")
    finally:
        con.unregister("_bulk")
    return len(rows)


RACE_COLUMNS = [
    "race_id", "kaisai_date", "jyo_cd", "race_no", "race_name", "jyoken", "grade",
    "tosu", "kyori_m", "laps", "start_at", "close_at", "nichiji", "last_day_flg",
    "tenko", "wind_dir", "wind_speed_ms", "temperature_c", "race_status",
    "is_girls", "is_midnight", "source", "updated_at",
]

ENTRY_COLUMNS = [
    "race_id", "syaban", "wakuban", "player_id", "player_name", "player_kana",
    "prefecture", "age", "graduate_period", "kyu", "han", "rating",
    # ここから下は出走表HTML由来。JSON API には無い。
    "kyakushitsu", "gear_ratio",
    "cnt_s", "cnt_h", "cnt_b",
    "win_nige", "win_makuri", "win_sashi", "win_mark",
    "cnt_1st", "cnt_2nd", "cnt_3rd", "cnt_out",
    "rate_win", "rate_top2", "rate_top3",
    "comment", "honshi_mark",
    "updated_at",
]

LINE_COLUMNS = [
    "race_id", "line_no", "position", "syaban", "line_size", "is_solo",
    "source", "fetched_at",
]

ODDS_COLUMNS = [
    "race_id", "bet_type", "combination", "snapshot_at", "official_dt",
    "is_official", "odds_low", "odds_high", "popularity", "fetched_at",
    "secs_to_close",
]

RESULT_COLUMNS = [
    "race_id", "syaban", "finish_pos", "finish_status", "margin",
    "last_lap_time", "kimarite", "got_s", "got_b", "updated_at",
]

PAYOUT_COLUMNS = [
    "race_id", "bet_type", "combination", "payout_yen", "popularity", "updated_at",
]

VELODROME_COLUMNS = [
    "jyo_cd", "jyo_name", "bank_length_m", "straight_m", "bank_angle_deg",
    "straight_angle_deg", "home_width_m", "back_width_m", "center_width_m",
    "max_agari_sec", "compass_deg", "share_nige", "share_makuri", "share_sashi",
    "updated_at",
]

_PRIMARY_KEYS = {
    "races": ["race_id"],
    "entries": ["race_id", "syaban"],
    "race_lines": ["race_id", "source", "syaban"],
    "odds_snapshots": ["race_id", "bet_type", "combination", "snapshot_at"],
    "results": ["race_id", "syaban"],
    "payouts": ["race_id", "bet_type", "combination"],
    "velodromes": ["jyo_cd"],
}


def load_velodromes(con, root: Path) -> int:
    """バンク諸元を投入する。静的データなので上書きでよい。"""
    rows = []
    for rec in rawstore.iter_class(root, "velodrome_html"):
        markup = rec.get("payload")
        if not isinstance(markup, str):
            continue
        d = parse_velodrome(markup)
        if not d.get("jyo_cd"):
            d["jyo_cd"] = (rec.get("params") or {}).get("jyo_cd")
        if not d.get("jyo_cd"):
            continue
        d["updated_at"] = _iso(rec.get("fetched_at"))
        rows.append(tuple(d.get(c) for c in VELODROME_COLUMNS))
    if not rows:
        return 0
    bulk_upsert(con, "velodromes", rows, VELODROME_COLUMNS)
    return len(rows)


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
    bulk_upsert(con, "races", rows, RACE_COLUMNS)
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
    """出走表を投入する。

    JSON API(AplRaceHorse)と出走表HTML(entry_html)を突き合わせてから1回で入れる。
    片方ずつ upsert すると、後から入れたほうが相手の列を NULL で潰してしまう。

    JSON からは 車番/枠番/級班/競走得点、
    HTML からは 脚質/SHB/決まり手構成/ギヤ倍数/勝率 を取る。後者は JSON API に無い。
    """
    merged: dict[tuple[str, int], dict] = {}

    for rec in rawstore.iter_class(root, "AplRaceHorse"):
        fetched = _iso(rec.get("fetched_at"))
        for e in rec.get("payload") or []:
            rid = e.get("race_id")
            syaban = _int(e.get("syaban"))
            if not rid or syaban is None:
                continue
            merged[(rid, syaban)] = {
                "race_id": rid,
                "syaban": syaban,
                "wakuban": _int(e.get("wakuban")),
                "player_name": e.get("name"),
                "prefecture": e.get("fuken"),
                "age": _int(e.get("age")),
                "graduate_period": _int(e.get("graduate")),
                "kyu": e.get("kyu"),
                "han": e.get("han"),
                "rating": _float(e.get("rating")),
                "updated_at": fetched,
            }

    for rec in rawstore.iter_class(root, "entry_html"):
        rid = (rec.get("params") or {}).get("race_id")
        markup = rec.get("payload")
        if not rid or not isinstance(markup, str):
            continue
        fetched = _iso(rec.get("fetched_at"))
        for syaban, extra in _parse_entry_html(markup).items():
            row = merged.setdefault((rid, syaban), {
                "race_id": rid, "syaban": syaban, "updated_at": fetched,
            })
            row.update(extra)

    if not merged:
        return 0
    rows = [tuple(r.get(c) for c in ENTRY_COLUMNS) for r in merged.values()]
    bulk_upsert(con, "entries", rows, ENTRY_COLUMNS)
    return len(rows)


# 出走表テーブルのヘッダ名 -> entries の列名。
# 固定インデックスではなくヘッダ照合にしてあるのは、列が増減しても静かに
# ずれた値が入るより、その列が欠けるほうが安全なため。
_ENTRY_HEADERS = {
    "枠": "wakuban",
    "車": "syaban",
    "本紙": "honshi_mark",
    "選手名": "_name",
    "競走得点": "rating",
    "脚質": "kyakushitsu",
    "S": "cnt_s",
    "H": "cnt_h",
    "B": "cnt_b",
    "逃げ": "win_nige",
    "まくり": "win_makuri",
    "差し": "win_sashi",
    "マーク": "win_mark",
    "1着": "cnt_1st",
    "2着": "cnt_2nd",
    "3着": "cnt_3rd",
    "着外": "cnt_out",
    "勝率": "rate_win",
    "2連対率": "rate_top2",
    "3連対率": "rate_top3",
    "ギヤ倍数": "gear_ratio",
    "選手コメント": "comment",
}

_INT_FIELDS = {
    "cnt_s", "cnt_h", "cnt_b", "win_nige", "win_makuri", "win_sashi", "win_mark",
    "cnt_1st", "cnt_2nd", "cnt_3rd", "cnt_out", "wakuban", "syaban",
}
_PCT_FIELDS = {"rate_win", "rate_top2", "rate_top3"}
_FLOAT_FIELDS = {"rating", "gear_ratio"}


def _norm_header(s: str) -> str:
    """'マ｜ク' や '本 紙' のような表記ゆれを吸収する。"""
    return re.sub(r"[\s｜|]", "", s).replace("マーク", "マーク")


def _pct(v: str):
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", v or "")
    return float(m.group(1)) / 100.0 if m else None


def _parse_entry_html(markup: str) -> dict[int, dict]:
    """出走表HTMLから車番ごとの追加情報を取り出す。"""
    body = re.sub(r"<script.*?</script>|<style.*?</style>", "", markup, flags=re.S)
    out: dict[int, dict] = {}

    for tbl in re.findall(r"<table[^>]*>.*?</table>", body, re.S):
        if "RaceCard_Table" not in tbl:
            continue
        rows = _rows_with_html(tbl)
        if not rows:
            continue

        header = [_norm_header(c) for c in rows[0][0]]
        # 'マ｜ク' は正規化すると 'マク' になるので合わせておく
        colmap = {}
        for i, h in enumerate(header):
            key = {"マク": "マーク"}.get(h, h)
            if key in _ENTRY_HEADERS:
                colmap[_ENTRY_HEADERS[key]] = i
        if "syaban" not in colmap or "kyakushitsu" not in colmap:
            continue  # 出走表本体ではない

        for cells, tr_html in rows[1:]:
            syaban = _int(cells[colmap["syaban"]]) if colmap["syaban"] < len(cells) else None
            if syaban is None:
                continue
            rec: dict = {}
            for field, i in colmap.items():
                if field == "syaban" or i >= len(cells):
                    continue
                raw = cells[i]
                if field == "_name":
                    parts = raw.split()
                    if parts:
                        rec["player_kana"] = parts[0]
                elif field == "kyakushitsu":
                    # '3 追' のように段階の数字が前に付く。記号だけ取る。
                    m = re.search(r"[逃追両]", raw)
                    rec["kyakushitsu"] = m.group(0) if m else None
                elif field in _PCT_FIELDS:
                    rec[field] = _pct(raw)
                elif field in _INT_FIELDS:
                    rec[field] = _int(raw)
                elif field in _FLOAT_FIELDS:
                    rec[field] = _float(raw)
                else:
                    rec[field] = raw or None

            pid = re.search(r"db/profile/\?id=(\d+)", tr_html)
            if pid:
                rec["player_id"] = pid.group(1)
            out[syaban] = rec
        if out:
            break
    return out


def _rows_with_html(markup: str) -> list[tuple[list[str], str]]:
    """(セル文字列, その行の生HTML) の組。生HTMLは選手IDの抽出に要る。"""
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", markup, re.S):
        cells = []
        for td in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S):
            txt = _TAG.sub(" ", td)
            txt = html.unescape(txt).replace("\xa0", " ")
            cells.append(re.sub(r"\s+", " ", txt).strip())
        if cells:
            out.append((cells, tr))
    return out


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
    bulk_upsert(con, "race_lines", rows, LINE_COLUMNS)
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
        # 確定オッズは official_dt の時点の値。バックフィルでは取得時刻が
        # レースの何ヶ月も後になるので、それを snapshot_at にしてはいけない。
        snapshot = official or _iso(params.get("snapshot_at")) or fetched
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
    bulk_upsert(con, "odds_snapshots", rows, ODDS_COLUMNS)
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
        bulk_upsert(con, "results", res_rows, RESULT_COLUMNS)
    if pay_rows:
        bulk_upsert(con, "payouts", pay_rows, PAYOUT_COLUMNS)
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

    load_velodromes(con, args.data_root)
    load_races(con, args.data_root)
    load_entries(con, args.data_root)
    load_lines(con, args.data_root)
    load_odds(con, args.data_root)
    load_results(con, args.data_root)

    # 投入試行数ではなく実際のテーブル件数を出す(raw には同じレースが何度も入るため)
    for table in (
        "velodromes",
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
