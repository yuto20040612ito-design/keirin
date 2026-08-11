"""Raw 層への追記保存。

方針:
  * 取得した生レスポンスをそのまま残す。パース仕様は必ず後で変えたくなる。
  * 追記のみ。既存行は絶対に書き換えない。
  * gzip は複数メンバの連結を許すので、追記モードでそのまま足していける。

レイアウト:
    data/raw/<api_class>/dt=<YYYYMMDD>/part.jsonl.gz

各行:
    {"fetched_at": ISO8601, "api_class": str, "params": {...}, "payload": <生JSON>}
"""

from __future__ import annotations

import gzip
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

_lock = threading.Lock()


def raw_path(root: Path, api_class: str, date_str: str) -> Path:
    return Path(root) / "raw" / api_class / f"dt={date_str}" / "part.jsonl.gz"


def append(
    root: Path,
    api_class: str,
    date_str: str,
    params: dict,
    payload,
    fetched_at: datetime | None = None,
) -> Path:
    """1レコードを raw 層に追記し、書き込んだファイルパスを返す。"""
    path = raw_path(root, api_class, date_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "fetched_at": (fetched_at or datetime.now(timezone.utc)).isoformat(),
        "api_class": api_class,
        "params": params,
        "payload": payload,
    }
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    with _lock:
        with gzip.open(path, "at", encoding="utf-8") as fh:
            fh.write(line)
    return path


def read(path: Path):
    """raw ファイルを1行ずつ読む。壊れた行は飛ばして収集を止めない。"""
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def iter_class(root: Path, api_class: str):
    """あるクラスの raw を全期間ぶん読む。"""
    base = Path(root) / "raw" / api_class
    if not base.exists():
        return
    for part in sorted(base.glob("dt=*/part.jsonl.gz")):
        yield from read(part)
