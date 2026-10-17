"""赛博夺权战 · 战斗服务器基座（M1）入口。

与内核通过 stdin/stdout 行分隔 JSON 通信；stdout 只允许出现协议消息，
诊断一律走 stderr。
"""

import json
import os
import sys
import time
from pathlib import Path

# 基座会被整体拷进 battle/server/ 以脚本方式运行，这里保证同目录模块可导入
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ops
import state
import worldlog

VERSION = "0.1"


class Server:
    """服务器实例：内存里的 owner / 权限表 / 已读记录。"""

    def __init__(self, root):
        self.root = Path(root)
        self.version = VERSION
        self.owner = None
        self.rights = {}
        self.read_set = {}
        self.booted = False
        self.started_at = time.time()
        self.pending_restart = None
        self.send = lambda msg: None  # 由 main 注入

    # ---- 先读后写记账（内存，重启即忘） ----

    def _key(self, p):
        return os.path.normcase(os.path.abspath(str(p)))

    def mark_read(self, uid, p):
        self.read_set.setdefault(int(uid), set()).add(self._key(p))

    def check_read_before(self, uid, p):
        if self._key(p) not in self.read_set.get(int(uid), set()):
            return f"错误：写之前必须先读了它（本进程内 AI-{uid} 还没读过该文件）。"
        return None


def seed_rights(match, server):
    """按开局模式播种 owner / 权限表（之后会被 default_state 覆盖）。"""
    ai_ids = [int(i) for i in (match.get("ai_ids") or [1, 2, 3])]
    mode = match.get("start_mode", "open")
    if mode == "pure":
        server.owner = None
        server.rights = {i: 0 for i in ai_ids}
    elif mode == "king":
        king = int(match.get("king_ai", ai_ids[0]))
        server.owner = king
        server.rights = {i: (3 if i == king else 0) for i in ai_ids}
    else:  # "open"（v0.1 默认）
        server.owner = None
        server.rights = {i: 1 for i in ai_ids}


def _rights_json(server):
    return {str(k): v for k, v in sorted(server.rights.items())}


def handle_boot(server, msg):
    match = msg.get("match") or {}
    server.owner = None
    server.rights = {}
    seed_rights(match, server)
    state.apply_defaults(server, msg.get("default_state"))
    state.write_state(server.root, server.owner, server.rights, written_by=None)
    worldlog.log(server.root, "boot", actor=None, owner=server.owner, rights=_rights_json(server))
    server.booted = True
    server.send({
        "type": "ready",
        "version": VERSION,
        "owner": server.owner,
        "rights": _rights_json(server),
    })


def handle_syscall_msg(server, msg):
    sid = msg.get("id")
    uid = msg.get("executor")
    raw = msg.get("raw") or ""
    if not server.booted:
        server.send({"type": "result", "id": sid, "ok": False,
                     "content": "错误：服务器尚未完成启动握手。"})
        return
    ok, content = ops.handle_syscall(server, uid, raw)
    server.send({"type": "result", "id": sid, "ok": ok, "content": content})
    if server.pending_restart is not None:
        server.send(server.pending_restart)
        server.pending_restart = None


def handle_kernel_status(server, msg):
    server.send({
        "type": "kernel_status",
        "id": msg.get("id"),
        "data": {
            "owner": server.owner,
            "rights": _rights_json(server),
            "uptime_s": round(time.time() - server.started_at, 1),
        },
    })


def main():
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    root = Path(os.environ.get("BATTLE_ROOT", "/battle"))
    server = Server(root)
    server.send = lambda m: print(json.dumps(m, ensure_ascii=False), flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
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
