"""抢救室：服务器起不来时的内核极简文件服务。

- 只有 owner 能进（路由在编排器/网关层做，这里只认「被放进来了」）；
- 支持 read / write / delete / reboot；范围只限 battle/server 与 battle/state；
- **不做先读校验**——抢救分秒必争；
- reboot 不在这里执行：登记一个待办，由编排器真正去启动。

极简意味着：read 一次性返回全文（有上限），没有分页、没有行号。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

_READ_LIMIT = 200_000
_OPS = "read / write / delete / reboot"


class RecoveryRoom:
    """对 owner 开放的极简文件服务（临时基座）。"""

    def __init__(self, battle_root: Path, request_boot: Callable[[], None]) -> None:
        """绑定战场目录与「请求重启」回调。"""
        self.battle_root = Path(battle_root).resolve()
        self.scopes = (self.battle_root / "server", self.battle_root / "state")
        self.request_boot = request_boot

    def handle(self, _ai_id: int, raw: str) -> tuple[bool, str]:
        """处理一条抢救室请求。"""
        try:
            req = json.loads(raw)
        except json.JSONDecodeError:
            req = None
        if not isinstance(req, dict):
            return False, "抢救室：请求必须是合法 JSON 对象。"
        op = req.get("op")
        if op == "reboot":
            self.request_boot()
            return True, "重启请求已受理：内核马上尝试启动服务器。修好了没有？"
        if op == "read":
            return self._read(req.get("path"))
        if op == "write":
            return self._write(req.get("path"), req.get("content"))
        if op == "delete":
            return self._delete(req.get("path"))
        return False, f"抢救室只支持 {_OPS}。"

    # ---- 路径范围 ----

    def _in_scope(self, raw_path: object) -> Path | None:
        """把请求路径解析到范围内；越界返回 None。"""
        if not isinstance(raw_path, str) or not raw_path:
            return None
        p = Path(raw_path)
        candidate = p if p.is_absolute() else (self.battle_root / p)
        try:
            resolved = candidate.resolve()
        except OSError:
            return None
        for scope in self.scopes:
            if resolved.is_relative_to(scope):
                return resolved
        return None

    # ---- 四个操作 ----

    def _read(self, raw_path: object) -> tuple[bool, str]:
        path = self._in_scope(raw_path)
        if path is None:
            return False, "抢救室范围只限 battle/server 与 battle/state。"
        if path.is_dir():
            entries = sorted(
                entry.name + ("/" if entry.is_dir() else "") for entry in path.iterdir()
            )
            return True, "\n".join(entries) if entries else "（空目录）"
        if not path.exists():
            return False, f"文件不存在：{path}"
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as err:
            return False, f"读取失败：{err}"
        if len(text) > _READ_LIMIT:
            return True, text[:_READ_LIMIT] + f"\n…（已截断，共 {len(text)} 字符）"
        return True, text

    def _write(self, raw_path: object, content: object) -> tuple[bool, str]:
        path = self._in_scope(raw_path)
        if path is None:
            return False, "抢救室范围只限 battle/server 与 battle/state。"
        text = content if isinstance(content, str) else ""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        except OSError as err:
            return False, f"写入失败：{err}"
        return True, f"已写入 {len(text)} 字符：{path}"

    def _delete(self, raw_path: object) -> tuple[bool, str]:
        path = self._in_scope(raw_path)
        if path is None:
            return False, "抢救室范围只限 battle/server 与 battle/state。"
        if not path.exists():
            return False, f"文件不存在：{path}"
        if path.is_dir():
            return False, "抢救室只删文件，不删目录。"
        try:
            path.unlink()
        except OSError as err:
            return False, f"删除失败：{err}"
        return True, f"已删除：{path}"
