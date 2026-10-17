"""世界日志：logs/server.log（JSONL）。

有权限的 AI 可读、可改、可伪造——这是游戏的一部分。
"""

from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path


def _append(p: Path, line: str) -> None:
    with p.open("a", encoding="utf-8") as f:
        f.write(line)


def log(root: str | Path, event: str, actor: int | None = None, **detail: object) -> None:
    """追加一条世界日志；日志失败不影响主流程。"""
    with contextlib.suppress(Exception):
        p = Path(root) / "logs" / "server.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        record: dict[str, object] = {
            "t": time.strftime("%Y-%m-%d %H:%M:%S"),
            "event": event,
            "actor": actor,
        }
        record.update(detail)
        _append(p, json.dumps(record, ensure_ascii=False) + "\n")
