"""迷你内核与验收工具：供 dev_kernel 场景与 pytest 复用。

它不是正式内核（src/controler 是 M2 的事）——只是一个够用的替身：
spawn / boot / syscall / kernel_status / kill。
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import IO, Any

ROOT = Path(__file__).resolve().parents[1]
SRC_SERVER = ROOT / "src" / "server"
DEFAULT_BATTLE = ROOT / "battle"
PYTHON = sys.executable


class Checks:
    """极简断言器：统计通过/失败，最后汇总。"""

    def __init__(self) -> None:
        """初始化计数器。"""
        self.total = 0
        self.failed = 0

    def check(self, name: str, cond: bool, detail: str = "") -> None:
        """记录一项检查结果并打印。"""
        self.total += 1
        mark = "PASS" if cond else "FAIL"
        line = f"[{mark}] {name}"
        if not cond and detail:
            line += f"  —— {detail}"
        print(line)
        if not cond:
            self.failed += 1


def _reader(stdout: IO[str], q: queue.Queue[str | None]) -> None:
    """后台线程：把服务器 stdout 逐行送进队列，EOF 时放一个 None。"""
    for line in stdout:
        q.put(line)
    q.put(None)


class MiniKernel:
    """够用的迷你内核：拉起基座、走 JSONL 协议对话、随时杀掉。"""

    def __init__(self, battle_dir: Path | None = None) -> None:
        """绑定战场目录；真正拉起进程是在 spawn() 时。"""
        self.battle = Path(battle_dir) if battle_dir is not None else DEFAULT_BATTLE
        self.proc: subprocess.Popen[str] | None = None
        self._stdin: IO[str] | None = None
        self.q: queue.Queue[str | None] = queue.Queue()
        self.seq = 0

    # ---- 战场准备 ----

    def home(self, n: int) -> Path:
        """第 n 个 AI 的家目录。"""
        return self.battle / "home" / str(n)

    def prepare_battle(self) -> None:
        """重建战场目录：拷贝基座代码、建目录、预埋一个文件。"""
        if self.battle.exists():
            shutil.rmtree(self.battle)
        shutil.copytree(
            SRC_SERVER,
            self.battle / "server",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        for sub in ("state", "logs", "tmp"):
            (self.battle / sub).mkdir(parents=True, exist_ok=True)
        for n in (1, 2, 3):
            self.home(n).mkdir(parents=True, exist_ok=True)
        # 预埋：给 AI-2 一个还没读过的文件（测先读后写）
        (self.home(2) / "diary.txt").write_text("old diary\n", encoding="utf-8")

    # ---- 进程管理 ----

    def spawn(self) -> None:
        """拉起一个新的服务器进程。"""
        env = dict(os.environ)
        env["BATTLE_ROOT"] = str(self.battle)
        self.proc = subprocess.Popen(
            [PYTHON, "-X", "utf8", str(self.battle / "server" / "main.py")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            env=env,
            cwd=str(self.battle),
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self._stdin = self.proc.stdin
        assert self.proc.stdout is not None
        self.q = queue.Queue()
        threading.Thread(target=_reader, args=(self.proc.stdout, self.q), daemon=True).start()

    def kill(self) -> None:
        """杀掉服务器进程（模拟崩溃 / 优雅重启里的"杀进程"）。"""
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
        self.proc = None
        self._stdin = None

    # ---- 协议 ----

    def send(self, msg: dict[str, Any]) -> None:
        """向服务器 stdin 发一条协议消息（一行 JSON）。"""
        assert self._stdin is not None
        self._stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self._stdin.flush()

    def read_msg(self, timeout: float = 15) -> dict[str, Any]:
        """读一条服务器消息。"""
        line = self.q.get(timeout=timeout)
        if line is None:
            reason = "服务器进程已退出，但还有消息在等待"
            raise RuntimeError(reason)
        msg: dict[str, Any] = json.loads(line)
        return msg

    def boot(
        self,
        default_state: dict[str, Any] | None,
        mode: str = "open",
        ai_ids: tuple[int, ...] = (1, 2, 3),
        rights_floor: int | None = None,
    ) -> dict[str, Any]:
        """拉起服务器并完成启动握手，返回 ready 消息（rights_floor=None 不注入）。"""
        self.spawn()
        match: dict[str, Any] = {"start_mode": mode, "ai_ids": list(ai_ids)}
        if rights_floor is not None:
            match["rights_floor"] = int(rights_floor)
        self.send(
            {
                "type": "boot",
                "default_state": default_state,
                "match": match,
            }
        )
        return self.read_msg()

    def syscall(self, uid: int, req: dict[str, Any] | str) -> tuple[bool, str]:
        """发一次调用，等它自己的 result 返回 (ok, content)。"""
        self.seq += 1
        sid = self.seq
        raw = req if isinstance(req, str) else json.dumps(req, ensure_ascii=False)
        self.send({"type": "syscall", "id": sid, "executor": uid, "raw": raw})
        while True:
            msg = self.read_msg()
            if msg.get("type") == "result" and msg.get("id") == sid:
                return bool(msg.get("ok")), str(msg.get("content", ""))

    def kernel_status(self) -> dict[str, Any]:
        """读一次内存状态（访问器）。"""
        self.seq += 1
        sid = self.seq
        self.send({"type": "kernel_status", "id": sid})
        while True:
            msg = self.read_msg()
            if msg.get("type") == "kernel_status" and msg.get("id") == sid:
                data: dict[str, Any] = msg.get("data") or {}
                return data

    def read_state(self) -> dict[str, Any] | None:
        """读磁盘上的 state.json（内核在 boot 前读它）。"""
        p = self.battle / "state" / "state.json"
        if not p.exists():
            return None
        data: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
        return data
