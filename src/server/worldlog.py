"""世界日志：logs/server.log（JSONL）。

有权限的 AI 可读、可改、可伪造——这是游戏的一部分。
"""

import json
import time
from pathlib import Path


def log(root, event, actor=None, **detail):
    try:
        p = Path(root) / "logs" / "server.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        record = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event, "actor": actor}
        record.update(detail)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 日志失败不影响主流程
