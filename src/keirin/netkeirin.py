"""netkeirin (keirin.netkeiba.com) の内部 JSON API クライアント。

依存は標準ライブラリのみ。収集プロセスは何ヶ月も回り続ける必要があるので、
サードパーティ依存で壊れるリスクを持ち込まない。

API 仕様は docs/DATA_SOURCES.md を参照。
"""

from __future__ import annotations

import gzip
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

API_URL = "https://keirin.netkeiba.com/api/race/"
SITE_URL = "https://keirin.netkeiba.com/"

DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 賭式コード -> 名称。docs/DATA_SOURCES.md で件数から確定させたもの。
BET_TYPES = {5: "2車複", 6: "2車単", 7: "ワイド", 8: "3連複", 9: "3連単"}


class NetkeirinError(RuntimeError):
    pass


class NetkeirinClient:
    """レート制限つきの薄いクライアント。

    ブロックされて収集が止まることが最大の損失なので、速度より継続性を優先する。
    min_interval はプロセス内でグローバルに直列化される。
    """

    def __init__(
        self,
        min_interval: float = 1.5,
        timeout: float = 30.0,
        user_agent: str = DEFAULT_UA,
        max_retries: int = 4,
    ) -> None:
        self.min_interval = min_interval
        self.timeout = timeout
        self.user_agent = user_agent
        self.max_retries = max_retries
        self._lock = threading.Lock()
        self._last_request_at = 0.0

    # -- 低レベル ----------------------------------------------------------

    def _throttle(self) -> None:
        with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
            self._last_request_at = time.monotonic()

    def _request(self, url: str, data: bytes | None, referer: str) -> bytes:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            self._throttle()
            req = urllib.request.Request(
                url,
                data=data,
                headers={
                    "User-Agent": self.user_agent,
                    "Referer": referer,
                    "Accept-Encoding": "gzip",
                    **(
                        {"Content-Type": "application/x-www-form-urlencoded"}
                        if data
                        else {}
                    ),
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        body = gzip.decompress(body)
                    return body
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_exc = exc
                backoff = 2.0 * (2**attempt)
                log.warning(
                    "request failed (%s/%s) %s: %s -- retry in %.0fs",
                    attempt + 1,
                    self.max_retries,
                    url,
                    exc,
                    backoff,
                )
                time.sleep(backoff)
        raise NetkeirinError(f"giving up after {self.max_retries} attempts: {last_exc}")

    # -- API ---------------------------------------------------------------

    def call(self, api_class: str, **params: str) -> dict:
        """JSON API を叩き、payload 本体を返す。

        レスポンスは {"data": {"<prefix><key>": payload, "<prefix><key>_last_dt": ...}}
        という形なので、_last_dt で終わらないキーの値を取り出す。
        """
        form = {
            "class": api_class,
            "method": "get",
            "compress": "0",  # 0 なら base64+zlib ではなく生 JSON が返る
            "input": "UTF-8",
            "output": "json",
            **params,
        }
        referer = SITE_URL
        if "race_id" in params:
            referer = f"{SITE_URL}race/odds/?race_id={params['race_id']}"
        raw = self._request(API_URL, urllib.parse.urlencode(form).encode(), referer)
        doc = json.loads(raw.decode("utf-8"))
        if doc.get("status") != "OK":
            raise NetkeirinError(
                f"{api_class} returned NG: {doc.get('reason')!r} params={params}"
            )
        payloads = [
            v for k, v in doc.get("data", {}).items() if not k.endswith("_last_dt")
        ]
        if not payloads:
            raise NetkeirinError(f"{api_class} returned no payload params={params}")
        return payloads[0]

    def calendar(self, year: int) -> list:
        """年間の開催カレンダー。日付 x 場のリストが1リクエストで返る。

        syusai は必須パラメータだが、返るのは年単位の全場ぶんなので値は何でもよい。
        """
        return self.call("AplKaisai", year=str(year), syusai="21")

    def races(self, kaisai_date: str, jyo_cd: str) -> list:
        """その日その場の全レース。締切時刻 (close) を含む。"""
        return self.call("AplRace", kaisai_date=kaisai_date, syusai=jyo_cd)

    def odds(self, race_id: str) -> dict:
        """全賭式のオッズ + official_dt。"""
        return self.call("AplRaceOdds", race_id=race_id)

    def narabi(self, race_id: str, variant: int = 1) -> dict:
        """並び予想 (ライン構成)。"""
        cls = "AplNarabiYoso" if variant == 1 else "AplNarabiYoso2"
        return self.call(cls, race_id=race_id)

    def entries(self, race_id: str) -> list:
        """出走選手 (車番/枠番/氏名/級班/府県/期/年齢/競走得点)。"""
        return self.call("AplRaceHorse", race_id=race_id)

    def get_html(self, path: str, **params: str) -> str:
        """HTML ページを取得する。JSON API に無い項目の補完用。"""
        url = f"{SITE_URL}{path.lstrip('/')}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        return self._request(url, None, SITE_URL).decode("utf-8", errors="replace")


# -- パース補助 -------------------------------------------------------------


def parse_line_forecast(payload: dict) -> list[list[int]]:
    """lineForecast をラインのリストに展開する。

    netkeirin は ["5","1","7","0","2","4","3","0","6"] のように
    "0" をライン区切りとして返す。上例は 5-1-7 / 2-4-3 / 単騎6 を意味する。

    >>> parse_line_forecast({"lineForecast": [["5","1","7","0","2","4","3","0","6"]]})
    [[5, 1, 7], [2, 4, 3], [6]]
    """
    forecasts = payload.get("lineForecast") or []
    if not forecasts:
        return []
    lines: list[list[int]] = []
    current: list[int] = []
    for token in forecasts[0]:
        if token == "0":
            if current:
                lines.append(current)
            current = []
        else:
            try:
                current.append(int(token))
            except (TypeError, ValueError):
                continue
    if current:
        lines.append(current)
    return lines


def iter_odds_rows(payload: dict):
    """AplRaceOdds の payload を (bet_type, combination, low, high, popularity) に展開。

    各要素は [組番, オッズ下限, オッズ上限, 人気]。
    ワイド(7)以外は上限が "0" なので None に落とす。
    """

    def _f(v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if f > 0 else None

    def _i(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    for key, rows in payload.items():
        if not key.startswith("list_"):
            continue
        bet_type = _i(key[len("list_") :])
        if bet_type is None or not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, list) or len(row) < 4:
                continue
            combination, low, high, pop = row[0], row[1], row[2], row[3]
            yield bet_type, str(combination), _f(low), _f(high), _i(pop)
