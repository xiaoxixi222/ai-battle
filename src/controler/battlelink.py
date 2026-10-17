"""与战斗服务器的 JSONL 进程通道。

两种运行器，对上层是同一件事——一个可写可读的 stdin/stdout：
- local：直接以子进程方式跑 battle/server/main.py（开发期）；
- docker：docker run -i 挂载 battle/（隔离期；镜像在 M3 落地后替换为自建）。

stdout 是唯一协议通道（一行一条 JSON）；stderr 全部落进
battle/logs/server.stderr.log，不污染协议。

并发假设：本类**不是**线程安全的——`_q` 与 `_inbox` 共享且无锁，两个并发
`wait_msg` 会互相吞消息。约定：一发请求-应答（send + wait_msg）必须被外层
串行化（内核用 Match 的链路锁）；`kill()` 是例外，它允许被随时调用，在途等待
会以 `LinkClosedError` 收场。重启会递增代际，旧世代的等待直接作废（不误收）。
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import IO, Any

_CONTROL_TYPES = ("result", "kernel_status", "ready")


class LinkError(Exception):
    """通道错误基类。"""


class LinkTimeoutError(LinkError):
    """等待消息超时。"""


class LinkClosedError(LinkError):
    """进程退出 / 通道关闭。"""


def _reader(stdout: IO[str], q: queue.Queue[str | None]) -> None:
    """后台线程：把进程 stdout 逐行送进队列，EOF 时放一个 None。"""
    for line in stdout:
        q.put(line)
    q.put(None)


class BattleLink:
    """战斗服务器进程 + JSONL 通道。"""

    def __init__(
        self,
        battle_root: Path,
        *,
        runner: str = "local",
        docker: dict[str, Any] | None = None,
        container_name: str = "ai-battle-server",
        log_path: Path | None = None,
        python: str | None = None,
    ) -> None:
        """绑定战场目录与运行器参数；真正拉起进程是在 spawn() 时。"""
        self.battle_root = Path(battle_root)
        self.runner = runner
        self.docker = dict(docker or {})
        self.container_name = container_name
        self.log_path = Path(log_path) if log_path is not None else None
        self.python = python or sys.executable
        self.proc: subprocess.Popen[str] | None = None
        self._stdin: IO[str] | None = None
        self._log_fh: IO[str] | None = None
        self._q: queue.Queue[str | None] = queue.Queue()
        self._inbox: list[dict[str, Any]] = []
        self._generation = 0

    # ---- 命令行 ----

    def _command(self) -> list[str]:
        """按运行器拼出拉起服务器的命令行。"""
        main = self.battle_root / "server" / "main.py"
        if self.runner == "docker":
            settings = self.docker
            host = str(self.battle_root.resolve()).replace("\\", "/")
            limits: list[str] = ["--network", str(settings.get("network", "none"))]
            if settings.get("cpus"):
                limits += ["--cpus", str(settings["cpus"])]
            if settings.get("memory"):
                limits += ["--memory", str(settings["memory"])]
            if settings.get("pids"):
                limits += ["--pids-limit", str(settings["pids"])]
            return [
                "docker",
                "run",
                "-i",
                "--rm",
                "--name",
                self.container_name,
                "-v",
                f"{host}:/battle",
                "-w",
                "/battle",
                "-e",
                "BATTLE_ROOT=/battle",
                *limits,
                str(settings.get("image", "python:latest")),
                "python",
                "-X",
                "utf8",
                "/battle/server/main.py",
            ]
        return [self.python, "-X", "utf8", str(main)]

    # ---- 进程管理 ----

    def spawn(self) -> None:
        """拉起一个新的服务器进程，并起读线程。"""
        env = dict(os.environ)
        env["BATTLE_ROOT"] = str(self.battle_root)
        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_fh = self.log_path.open("a", encoding="utf-8", errors="replace")
        stderr_target: Any = self._log_fh if self._log_fh is not None else subprocess.DEVNULL
        self.proc = subprocess.Popen(
            self._command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_target,
            env=env,
            cwd=str(self.battle_root),
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=(os.name == "posix"),  # POSIX 下整组可杀
        )
        self._stdin = self.proc.stdin
        if self.proc.stdout is None:  # pragma: no cover - PIPE 一定拿得到
            msg = "无法获取服务器 stdout。"
            raise LinkError(msg)
        self._q = queue.Queue()
        self._inbox = []
        self._generation += 1  # 新世代：旧世代的等待者立即作废
        threading.Thread(target=_reader, args=(self.proc.stdout, self._q), daemon=True).start()

    def alive(self) -> bool:
        """进程是否还活着（无锁读，容忍与 kill/spawn 交错）。"""
        proc = self.proc
        return proc is not None and proc.poll() is None

    def returncode(self) -> int | None:
        """进程退出码（还活着时为 None）；会顺带回收退出状态。"""
        proc = self.proc
        if proc is None:
            return None
        return proc.poll()

    def kill(self) -> None:
        """杀掉服务器（崩溃模拟 / 重启的第一步 / 终局清理），幂等且不抛。

        本地运行器要杀整棵进程树：AI 的 bash 会往服务器身上挂孤儿进程，
        不连坐的话孤儿会占着 battle/ 目录，让下一次重建战场失败。
        """
        if self.proc is not None:
            pid = self.proc.pid
            if self.runner == "docker":
                with contextlib.suppress(OSError, subprocess.SubprocessError):
                    subprocess.run(
                        ["docker", "rm", "-f", self.container_name],
                        check=False,
                        capture_output=True,
                        text=True,
                    )
            elif self.proc.poll() is None:
                if sys.platform == "win32":
                    with contextlib.suppress(OSError, subprocess.SubprocessError):
                        subprocess.run(
                            ["taskkill", "/F", "/T", "/PID", str(pid)],
                            check=False,
                            capture_output=True,
                            text=True,
                        )
                else:
                    with contextlib.suppress(OSError, ProcessLookupError):
                        os.killpg(os.getpgid(pid), signal.SIGKILL)
            if self.proc.poll() is None:
                with contextlib.suppress(OSError):
                    self.proc.kill()
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                self.proc.wait(timeout=5)
        self.proc = None
        self._stdin = None
        if self._log_fh is not None:
            with contextlib.suppress(OSError):
                self._log_fh.close()
            self._log_fh = None

    # ---- 协议对话 ----

    def send(self, msg: dict[str, Any]) -> None:
        """向服务器 stdin 发一条协议消息（一行 JSON）。"""
        stdin = self._stdin
        if stdin is None or not self.alive():
            msg_err = "发送失败：服务器进程不可用。"
            raise LinkClosedError(msg_err)
        try:
            stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
            stdin.flush()
        except (OSError, ValueError, AttributeError) as err:
            msg_err = f"发送失败：通道已断（{err}）。"
            raise LinkClosedError(msg_err) from err

    def _take_inbox(self, kind: str, sid: int | None) -> dict[str, Any] | None:
        """从暂存区取一条匹配的消息。"""
        for i, msg in enumerate(self._inbox):
            if msg.get("type") == kind and (sid is None or msg.get("id") == sid):
                return self._inbox.pop(i)
        return None

    def wait_msg(self, kind: str, sid: int | None, timeout: float) -> dict[str, Any]:
        """等一条指定类型（可带 id）的消息；其余消息进暂存区。

        等待期间通道若被重建（服务器重启），立即放弃：新世代的礼包不能
        错递给旧回合的等待者。
        """
        generation = self._generation
        msg = self._take_inbox(kind, sid)
        if msg is not None:
            return msg
        deadline = time.monotonic() + timeout
        while True:
            if self._generation != generation:
                msg_err = "等待中断：通道已重建。"
                raise LinkClosedError(msg_err)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                msg_err = f"等待 {kind} 超时（{timeout}s）。"
                raise LinkTimeoutError(msg_err)
            try:
                line = self._q.get(timeout=remaining)
            except queue.Empty:
                msg_err = f"等待 {kind} 超时（{timeout}s）。"
                raise LinkTimeoutError(msg_err) from None
            if line is None:
                msg_err = "服务器进程已退出。"
                raise LinkClosedError(msg_err)
            try:
                parsed: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError:
                continue  # 脏行：协议容错，跳过
            if parsed.get("type") == kind and (sid is None or parsed.get("id") == sid):
                return parsed
            self._inbox.append(parsed)

    def collect_control(self, grace: float = 0.0) -> list[dict[str, Any]]:
        """收罗控制面消息（如 restart_request）：先看暂存区，再宽限 grace 秒收新行。

        普通 result / kernel_status / ready 留在暂存区，不在这里消费。
        """
        deadline = time.monotonic() + max(0.0, grace)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                line = self._q.get(timeout=remaining)
            except queue.Empty:
                break
            if line is None:
                break
            try:
                self._inbox.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        control = [m for m in self._inbox if m.get("type") not in _CONTROL_TYPES]
        self._inbox = [m for m in self._inbox if m.get("type") in _CONTROL_TYPES]
        return control

    # ---- 高层对话 ----

    def boot(
        self,
        default_state: dict[str, Any] | None,
        match: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        """发启动握手，等 ready。"""
        self.send({"type": "boot", "default_state": default_state, "match": match})
        return self.wait_msg("ready", None, timeout)

    def syscall(self, sid: int, executor: int, raw: str, timeout: float) -> tuple[bool, str]:
        """发一次 syscall，等它自己的 result，返回 (ok, content)。"""
        self.send({"type": "syscall", "id": sid, "executor": executor, "raw": raw})
        msg = self.wait_msg("result", sid, timeout)
        return bool(msg.get("ok")), str(msg.get("content", ""))

    def kernel_status(self, sid: int, timeout: float) -> dict[str, Any]:
        """发一次 kernel_status，拿内存访问器的实时状态。"""
        self.send({"type": "kernel_status", "id": sid})
        msg = self.wait_msg("kernel_status", sid, timeout)
        data: dict[str, Any] = msg.get("data") or {}
        return data
