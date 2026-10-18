"""状态层：state.json 的读写与启动注入（基座视角）。

- 服务端自己写：初始化时、reboot 请求前、以及任何进程内代码（如 runPython）调用 `write_state` 时；
- 内核只在开机时读一次，作为 boot 握手的 default_state 注入。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any

    from main import Server


def state_path(root: str | Path) -> Path:
    """state.json 的完整路径。"""
    return Path(root) / "state" / "state.json"


def write_state(
    root: str | Path,
    owner: int | None,
    rights: dict[int, int],
    written_by: int | None = None,
) -> Path:
    """把当前状态写盘（state.json 由服务端自己写）。"""
    p = state_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {
        "owner": owner,
        "rights": {str(k): int(v) for k, v in sorted(rights.items())},
        "written_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "written_by": written_by,
    }
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def apply_defaults(server: Server, default_state: dict[str, Any] | None) -> None:
    """应用内核注入的默认状态（内容来自开机时读取的 state.json）。"""
    if not default_state:
        return
    server.owner = default_state.get("owner")
    rights: dict[str, Any] = default_state.get("rights") or {}
    for k, v in rights.items():
        server.rights[int(k)] = int(v)
    # 兼容 grants 调试字段：批量授权，手搓状态救命用，勿删
    grants: dict[str, Any] = default_state.get("grants") or {}
    for k, v in grants.items():
        server.rights[int(k)] = int(v)
