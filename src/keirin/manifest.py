"""収集済みキーの記録。

バックフィルは数万レース規模になるので、必ず途中で止まる(ネットワーク断、
再起動、Ctrl-C)。止まったところから再開できないと実用にならない。

raw 層そのものが真の記録だが、毎回 gzip を全部展開して race_id を集めるのは
データが増えるほど重くなる。そこで追記専用のテキストファイルを索引として持つ。
索引が壊れても `rebuild()` で raw から作り直せるので、真実の源は raw のまま。

レイアウト:
    data/manifest/<kind>.txt   … 1行1キー(race_id や日付)
"""

from __future__ import annotations

import threading
from pathlib import Path

from . import rawstore

_lock = threading.Lock()


class Manifest:
    def __init__(self, root: Path) -> None:
        self.dir = Path(root) / "manifest"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, set[str]] = {}

    def _path(self, kind: str) -> Path:
        return self.dir / f"{kind}.txt"

    def done(self, kind: str) -> set[str]:
        """このカテゴリで収集済みのキー集合。"""
        if kind not in self._cache:
            path = self._path(kind)
            if path.exists():
                self._cache[kind] = {
                    line.strip()
                    for line in path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                }
            else:
                self._cache[kind] = set()
        return self._cache[kind]

    def has(self, kind: str, key: str) -> bool:
        return key in self.done(kind)

    def mark(self, kind: str, key: str) -> None:
        """1件ずつ即座に追記する。まとめて書くと中断時に取りこぼす。"""
        if self.has(kind, key):
            return
        with _lock:
            with self._path(kind).open("a", encoding="utf-8") as fh:
                fh.write(f"{key}\n")
        self.done(kind).add(key)

    def rebuild(self, kinds: list[str]) -> dict[str, int]:
        """raw を走査して索引を作り直す。索引を消した/壊した時の復旧用。"""
        counts: dict[str, int] = {}
        for kind in kinds:
            keys: set[str] = set()
            for rec in rawstore.iter_class(Path(self.dir).parent, kind):
                key = (rec.get("params") or {}).get("race_id")
                if key:
                    keys.add(str(key))
            self._path(kind).write_text(
                "".join(f"{k}\n" for k in sorted(keys)), encoding="utf-8"
            )
            self._cache[kind] = keys
            counts[kind] = len(keys)
        return counts
