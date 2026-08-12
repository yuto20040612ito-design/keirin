"""公式 keirin.jp からバンク諸元を取る。

keirin.jp は robots.txt が `Disallow:/` のホワイトリスト方式で、出走表・結果・
オッズは許可されていない。一方 `/pc/jyoguide` は明示的に許可されており、
競輪場の静的なデータ（バンク周長・みなし直線・カント・方位）が載っている。
レース単位のデータは netkeirin、場の諸元は公式、と使い分ける。

43場ぶんの静的データなので、収集は年に1回で足りる。

## パースについて

バンク諸元はページ内に「エスケープされたJSON文字列」として埋まっており、
外側のJSONをパースしても取り出せない（`jyoInfo` が空文字に見える）。
そのため必要なキーだけを正規表現で拾う。構造化パースより脆いが、
対象が固定キーの数値だけなので実用上は問題ない。値が取れなければ None にする。
"""

from __future__ import annotations

import html
import logging
import re

from .netkeirin import NetkeirinClient

log = logging.getLogger(__name__)

BASE = "https://www.keirin.jp"
GUIDE_PATH = "/pc/jyoguide"

# 1着の決まり手は逃げ・捲り・差しの3種。マークは2着以降にしか付かないので
# ここには含めない(含めると構成比の合計が1を超える)。
_TECHNIQ = {"逃げ": "share_nige", "捲り": "share_makuri", "差し": "share_sashi"}


def fetch_velodrome(client: NetkeirinClient, jyo_cd: str) -> str:
    """1場ぶんの案内ページHTMLを取る。レート制限は client が持っている。"""
    url = f"{BASE}{GUIDE_PATH}?&jocd={jyo_cd}"
    return client._request(url, None, BASE).decode("utf-8", errors="replace")


def list_velodrome_codes(client: NetkeirinClient) -> list[str]:
    """場コード一覧。一覧ページのリンクから拾う。"""
    raw = client._request(f"{BASE}/pc/jyolist", None, BASE).decode(
        "utf-8", errors="replace"
    )
    codes = set(re.findall(r"jyoguide\?(?:&amp;|&)?jocd=(\d+)", raw))
    return sorted(codes)


def _num(pattern: str, text: str) -> float | None:
    m = re.search(pattern, text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except (TypeError, ValueError):
        return None


def _dms_to_deg(raw: str | None) -> float | None:
    """'32&deg;24&prime;17&Prime;' -> 32.405 度。"""
    if not raw:
        return None
    s = html.unescape(raw.replace("\\u0026", "&"))
    m = re.search(r"(\d+)[°º]\s*(?:(\d+)[′']\s*)?(?:(\d+)[″\"]\s*)?", s)
    if not m:
        return None
    d, mi, se = m.group(1), m.group(2) or 0, m.group(3) or 0
    return float(d) + float(mi) / 60 + float(se) / 3600


def parse_velodrome(markup: str) -> dict:
    """案内ページからバンク諸元を取り出す。取れなかった項目は None。"""
    t = markup.replace("\\u0026", "&")
    out: dict = {
        "jyo_cd": None,
        "jyo_name": None,
        "bank_length_m": None,
        "straight_m": None,
        "bank_angle_deg": None,
        "straight_angle_deg": None,
        "home_width_m": None,
        "back_width_m": None,
        "center_width_m": None,
        "max_agari_sec": None,
        "compass_deg": None,
        "share_nige": None,
        "share_makuri": None,
        "share_sashi": None,
    }

    m = re.search(r'"jyoCode"\s*:\s*"(\d+)"', t)
    if m:
        out["jyo_cd"] = m.group(1)
    m = re.search(r'"jyoName"\s*:\s*"([^"]+)"', t)
    if m:
        out["jyo_name"] = m.group(1)

    # 周長は数値では持っておらず、バンク図の画像名 bank400.gif に入っている
    out["bank_length_m"] = _num(r'syutyoImg"\s*:\s*"[^"]*bank(\d{3})', t)
    # バンクの方位も同様に compass90.gif から
    out["compass_deg"] = _num(r'houiImg"\s*:\s*"[^"]*compass(\d+)', t)

    out["straight_m"] = _num(r'"tyokusen"\s*:\s*"([\d.]+)m', t)
    out["home_width_m"] = _num(r'"homeHukuin"\s*:\s*"([\d.]+)m', t)
    out["back_width_m"] = _num(r'"backHukuin"\s*:\s*"([\d.]+)m', t)
    out["center_width_m"] = _num(r'"centerHukuin"\s*:\s*"([\d.]+)m', t)
    out["max_agari_sec"] = _num(r'"maxAgari"\s*:\s*"([\d.]+)', t)

    m = re.search(r'"kant"\s*:\s*"([^"]+)"', t)
    out["bank_angle_deg"] = _dms_to_deg(m.group(1) if m else None)
    m = re.search(r'"tkant"\s*:\s*"([^"]+)"', t)
    out["straight_angle_deg"] = _dms_to_deg(m.group(1) if m else None)

    # 当該バンクの決まり手構成。展開の起きやすさがそのまま出る。
    # ページには1着グラフと2着グラフが並んでおり、同じキー名で2組現れる。
    # 欲しいのは1着なので、先に出たほうだけを採る(後勝ちにすると2着の値になる)。
    for name, pct in re.findall(
        r'"iconName"\s*:\s*"([^"]+)"\s*,\s*"percentName"\s*:\s*"(\d+)%"', t
    ):
        col = _TECHNIQ.get(name)
        if col and out[col] is None:
            out[col] = int(pct) / 100.0

    return out
