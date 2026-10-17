"""对局账本：runs/<id>/ledger.jsonl，一行一事件，**永不回滚**。

每条事件带 t（墙钟时间）与 rel（对局相对秒数，便于回放）。事件类型至少在：
spawn / boot_ok / boot_fail / syscall / owner_sample / owner_change / score /
crash / restart / rollback / recovery_enter / recovery_commit / recovery_exit /
full_reset / end。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any


class Ledger:
    """追加式 JSONL 账本。"""

    def __init__(self, path: Path, clock: Callable[[], float] | None = None) -> None:
        """绑定账本文件（父目录自动创建），以 clock 为对局时钟计 rel。"""
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock if clock is not None else time.monotonic
        self._t0 = self._clock()

    def log(self, event: str, **fields: object) -> None:
        """追加一条事件。"""
        row: dict[str, Any] = {
            "t": time.strftime("%Y-%m-%d %H:%M:%S"),
            "rel": round(self._clock() - self._t0, 2),
            "event": event,
            **fields,
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def read_all(self) -> list[dict[str, Any]]:
        """读回全部事件（测试 / 回放用）。"""
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def events(self, *names: str) -> list[dict[str, Any]]:
        """按事件名过滤账本。"""
        wanted = set(names)
        return [row for row in self.read_all() if row.get("event") in wanted]
