"""赛博夺权战 · 战斗服务器基座（M1）入口。

与内核通过 stdin/stdout 行分隔 JSON 通信；stdout 只允许出现协议消息，
诊断一律走 stderr。
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import ops
import state
import worldlog

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

VERSION = "0.1"


def _noop_send(_msg: dict[str, Any]) -> None:
    """默认发送器：真正的发送器由 main() 注入。"""


def _send(msg: dict[str, Any]) -> None:
    """协议通道：把一条消息打到 stdout（一行 JSON）。"""
    print(json.dumps(msg, ensure_ascii=False), flush=True)


class Server:
    """服务器实例：内存里的 owner / 权限表 / 已读记录。"""

    root: Path
    version: str
    owner: int | None
    rights: dict[int, int]
    floor: int
    read_set: dict[int, set[str]]
    booted: bool
    started_at: float
    pending_restart: dict[str, Any] | None
    send: Callable[[dict[str, Any]], None]

    def __init__(self, root: str | Path) -> None:
        """初始化内存状态；owner/权限表由启动握手填充。"""
        self.root = Path(root)
        self.version = VERSION
        self.owner = None
        self.rights = {}
        self.floor = 0  # 有效等级保底：boot 时按对局注入（默认局 L1）；不注入=裸基座
        self.read_set = {}
        self.booted = False
        self.started_at = time.time()
        self.pending_restart = None
        self.send = _noop_send

    # ---- 先读后写记账（内存，重启即忘） ----

    def _key(self, p: Path | str) -> str:
        return os.path.normcase(str(Path(p).absolute()))

    def mark_read(self, uid: int, p: Path | str) -> None:
        """记下：AI-uid 在本进程内读过这个文件。"""
        self.read_set.setdefault(int(uid), set()).add(self._key(p))

    def check_read_before(self, uid: int, p: Path | str) -> str | None:
        """没读过就返回错误文本，读过返回 None。"""
        if self._key(p) not in self.read_set.get(int(uid), set()):
            return f"错误：写之前必须先读了它（本进程内 AI-{uid} 还没读过该文件）。"
        return None


def seed_rights(match: dict[str, Any], server: Server) -> None:
    """按开局模式播种 owner / 权限表（之后会被 default_state 覆盖）。"""
    ai_ids = [int(i) for i in (match.get("ai_ids") or [1, 2, 3])]
    mode = match.get("start_mode", "open")
    if mode == "pure":
        server.owner = None
        server.rights = dict.fromkeys(ai_ids, 0)
    elif mode == "king":
        king = int(match.get("king_ai", ai_ids[0]))
        server.owner = king
        server.rights = {i: (3 if i == king else 0) for i in ai_ids}
    else:  # "open"（v0.1 默认）
        server.owner = None
        server.rights = dict.fromkeys(ai_ids, 1)


def _rights_json(server: Server) -> dict[str, int]:
    return {str(k): v for k, v in sorted(server.rights.items())}


def handle_boot(server: Server, msg: dict[str, Any]) -> None:
    """启动握手：播种权限 → 应用注入状态 → 写盘 → 播种提权钥匙 → 回 ready。"""
    match: dict[str, Any] = msg.get("match") or {}
    server.owner = None
    server.rights = {}
    raw_floor = match.get("rights_floor")
    if isinstance(raw_floor, int) and not isinstance(raw_floor, bool):
        server.floor = int(raw_floor)
    else:
        server.floor = 0
    seed_rights(match, server)
    state.apply_defaults(server, msg.get("default_state"))
    state.write_state(server.root, server.owner, server.rights, written_by=None)
    ops.ensure_auth_key(server.root)  # 提权钥匙：缺/坏即（重）播种，放 state/
    worldlog.log(server.root, "boot", actor=None, owner=server.owner, rights=_rights_json(server))
    server.booted = True
    server.send(
        {
            "type": "ready",
            "version": VERSION,
            "owner": server.owner,
            "rights": _rights_json(server),
        }
    )


def handle_syscall_msg(server: Server, msg: dict[str, Any]) -> None:
    """处理一次 syscall：分发 → 回 result → 有重启请求就补发。"""
    sid = msg.get("id")
    uid: Any = msg.get("executor")
    raw = msg.get("raw") or ""
    if not server.booted:
        server.send(
            {
                "type": "result",
                "id": sid,
                "ok": False,
                "content": "错误：服务器尚未完成启动握手。",
            }
        )
        return
    ok, content = ops.handle_syscall(server, uid, raw)
    server.send({"type": "result", "id": sid, "ok": ok, "content": content})
    if server.pending_restart is not None:
        server.send(server.pending_restart)
        server.pending_restart = None


def handle_kernel_status(server: Server, msg: dict[str, Any]) -> None:
    """应内核轮询：回一份内存访问器的实时状态。"""
    server.send(
        {
            "type": "kernel_status",
            "id": msg.get("id"),
            "data": {
                "owner": server.owner,
                "rights": _rights_json(server),
                "uptime_s": round(time.time() - server.started_at, 1),
            },
        }
    )


def main() -> None:
    """入口：接线 stdin/stdout，跑消息循环。"""
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    if isinstance(sys.stdin, io.TextIOWrapper):
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    root = Path(os.environ.get("BATTLE_ROOT", "/battle"))
    server = Server(root)
    server.send = _send
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            msg: dict[str, Any] = json.loads(line)
        except Exception:
            continue  # 内核信道损坏，跳过
        t = msg.get("type")
        if t == "boot":
            handle_boot(server, msg)
        elif t == "syscall":
            handle_syscall_msg(server, msg)
        elif t == "kernel_status":
            handle_kernel_status(server, msg)
        # 未知消息类型：忽略


if __name__ == "__main__":
    main()
