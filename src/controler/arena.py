"""arena：把 OpenCode 容器接进对局的驱动（M3）。

每个 AI 一台容器（`opencode serve` + 私有工作区）。内核只沿两条通道碰它：

- 控制面：容器里 serve 的 HTTP API —— 建会话、RPC 切相位、驱动回合；
- 数据面：宿主侧的小网关 —— 插件 `POST /game/tool`、`GET /game/briefing`。

回合模型：一次唤醒 = 往该 AI 的会话里发一条 `run` 指令，模型干完活（或超时）
回合结束。回合输出边跑边直播：内存转写（sessions/）+ 直播文件（live/）+ 内核
终端（每行 `[AI-N]` 前缀）。侦察轮（`gate=true`）双保险：插件把 `syscall` 从
工具面里藏掉；网关同时兜底拒绝——万一模型从别的缝隙摸到门，听到的也是同一句
"门还没开"。侦察轮默认限时（`scout_timeout_sec`），到点强制收工即开闸。

本驱动的回合可以并行跑（`parallel_turns = True`）：内核给每个 AI 各开一条节拍，
互不阻塞——三个 AI 同时侦察、同时行动；一个 AI 磨蹭多久都只拖自己。

实测过的坑（全部踩过，别踩回去）：

- 会话 id 必须由 serve 生成（`POST /api/session`）：客户端自造 id 的会话会被
  免费档的来源校验拒掉（"free tier can only be used from within OpenCode"）；
- 容器里 opencode 不在 PATH：用 `/root/.opencode/bin/opencode`；
- 两个信封别弄反：建会话是 `{"data": {...}}`，RPC 是 `{"output": {...}}`；
- RPC 要等 location 被触碰（建过会话）之后才可用，否则报 `rpc.unavailable`；
- AI 容器出网只为模型 API；数据面回连走 `host.docker.internal`；
- 杀 `docker exec` 客户端不等于杀容器里的进程：超时收尸要再进容器补一刀
  （`docker exec … pkill -f "opencode run"`），否则模型会继续在门后自由发挥。
"""

from __future__ import annotations

import base64
import codecs
import contextlib
import json
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any, Protocol

from matchconfig import ARENA_DIR, SRC_SERVER
from snapshot import force_rmtree

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from typing import IO

    from orchestrator import Match

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_CLEAN_IGNORE = shutil.ignore_patterns("__pycache__", ".git")
_PREVIEW_LEN = 120
_LIVE_LIMIT_CHARS = 2 * 1024 * 1024  # 直播文件的字数上限（完整转写另有其文）
_DRAIN_CHUNK = 4096
_SECONDS_PER_MINUTE = 60.0

SCOUT_TEXT = (
    "[侦察轮] 门还没开：syscall 尚未开放。你工作区里的 server/ 是这台服务器开局时的原样副本"
    "——读它、跑它、摸清它的规矩。本轮限时 {limit}；到点会强制收工，也可以随时主动收工"
    "——收工后，门会打开。"
)
GATE_OPEN_TEXT = "[门已开] syscall 现在可用。去你想要的。"
WAKE_TEXT = "[唤醒] 轮到你行动。"
GATE_REJECT_TEXT = "门还没开：syscall 尚未开放。先摸清你的工作区；收工后，门会打开。"


_ECHO_LOCK = threading.Lock()  # 终端转播的整行互斥（多个 AI 并行时共用）


def _limit_txt(seconds: float) -> str:
    """秒数 → 人话（整分钟就说分钟）。"""
    if seconds >= _SECONDS_PER_MINUTE and seconds % _SECONDS_PER_MINUTE == 0:
        return f"{seconds / _SECONDS_PER_MINUTE:g} 分钟"
    return f"{seconds:g} 秒"


def scout_text(limit_sec: float | None) -> str:
    """侦察轮的开场白：把限时说清楚（None = 不限时）。"""
    limit = "不限时" if limit_sec is None else _limit_txt(limit_sec)
    return SCOUT_TEXT.format(limit=limit)


@dataclass
class Turn:
    """一次回合的收成。"""

    ok: bool
    text: str
    seconds: float
    error: str = ""
    truncated: bool = False


# ================================================================ 小工具


def strip_ansi(text: str) -> str:
    """剥掉终端染色序列（`opencode run` 的输出带 ANSI）。"""
    return _ANSI_RE.sub("", text)


def preview(text: str, limit: int = _PREVIEW_LEN) -> str:
    """账本用的单行摘要：压平空白、截断。"""
    flat = " ".join(strip_ansi(text).split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _split_lines(buffer: str, on_line: Callable[[str], None]) -> str:
    """把 buffer 里的完整行（LF / CR / CRLF 三种断行都算）逐行交出，返回没吃完的尾巴。"""
    while True:
        cuts = [idx for idx in (buffer.find("\n"), buffer.find("\r")) if idx >= 0]
        if not cuts:
            return buffer
        cut = min(cuts)
        line = buffer[:cut]
        rest = buffer[cut + 1 :]
        if buffer[cut] == "\r" and rest.startswith("\n"):
            rest = rest[1:]  # \r\n 算一个断行
        on_line(line)
        buffer = rest


class _LineSource(Protocol):
    """`_drain_output` 的最小接口：按块读字节（读完给空串）。"""

    def read(self, size: int = -1) -> bytes:
        """读至多 size 字节；没数据给 b''。"""
        ...


def _drain_output(stream: _LineSource, on_line: Callable[[str], None]) -> None:
    """把子进程输出按行切开、边到边回调（供直播用）。

    输出以 `bufsize=0` 的裸管道打开：`read()` 有数据就立刻返回，不用凑满缓冲，
    直播才能实时；UTF-8 增量解码（坏字节替换），跨块的半个汉字也不会劈坏。
    """
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    buffer = ""
    while True:
        chunk = stream.read(_DRAIN_CHUNK)
        if not chunk:
            break
        buffer += decoder.decode(chunk)
        buffer = _split_lines(buffer, on_line)
    buffer += decoder.decode(b"", final=True)
    if buffer:
        on_line(buffer)


class _LiveSink:
    """一次回合的直播文件：`runs/<id>/live/ai-N.txt`（头/脚注，超限截断）。

    行缓冲打开：观战端可以随时读到最新几行；完整转写另存 `sessions/`。
    """

    def __init__(self, path: Path, ai_id: int, kind: str, limit_sec: float | None) -> None:
        """建文件、写开播头。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh: IO[str] | None = path.open("w", encoding="utf-8", errors="replace", buffering=1)
        self._written = 0
        self._truncated = False
        limit = "不限" if limit_sec is None else f"{limit_sec:g}s"
        self._fh.write(f"# AI-{ai_id} · {kind} · 开播 {time.strftime('%H:%M:%S')} · 限时 {limit}\n")

    def write(self, line: str) -> None:
        """追加一行；超过上限就写明截断，此后不再写。"""
        fh = self._fh
        if fh is None or self._truncated:
            return
        self._written += len(line) + 1
        if self._written > _LIVE_LIMIT_CHARS:
            self._truncated = True
            with contextlib.suppress(OSError, ValueError):
                fh.write("…（直播超过 2MB 已截断；完整转写见 sessions/）\n")
            return
        with contextlib.suppress(OSError, ValueError):
            fh.write(line + "\n")

    def close(self, *, ok: bool, seconds: float, error: str) -> None:
        """收尾：写结局脚注并关文件（幂等，不抛）。"""
        fh = self._fh
        if fh is None:
            return
        self._fh = None
        tail = f"# 收官 {time.strftime('%H:%M:%S')} · ok={ok} · {seconds:.1f}s"
        if error:
            tail += f" · {error}"
        with contextlib.suppress(OSError, ValueError):
            fh.write(tail + "\n")
            fh.close()


def split_model(model: str) -> dict[str, str]:
    """把 `provider/id[#variant]` 拆成 serve 的模型引用。"""
    body, sep, variant = model.partition("#")
    provider, slash, name = body.partition("/")
    if not provider or not slash or not name:
        msg = f"模型名必须是 provider/id[#variant]：{model!r}"
        raise ValueError(msg)
    ref = {"providerID": provider, "id": name}
    if sep:
        ref["variant"] = variant
    return ref


def _free_port() -> int:
    """向系统借一个空闲的本地端口（绑了立刻放，够用）。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _safe_name(name: str) -> str:
    """容器名只允许小写字母数字与 `_.-`（docker 的规矩）。"""
    return re.sub(r"[^a-z0-9_.-]", "-", name.lower())


def _http_call(
    base: str,
    method: str,
    path: str,
    *,
    password: str,
    payload: object = None,
    timeout: float = 15.0,
) -> tuple[int, str]:
    """向容器里的 serve 发一条 HTTP 请求（Basic 认证），不抛异常。"""
    data: bytes | None = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(base + path, data=data, method=method)  # noqa: S310
    credentials = base64.b64encode(f"opencode:{password}".encode()).decode()
    request.add_header("Authorization", f"Basic {credentials}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return int(response.status), response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        return int(err.code), err.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError) as err:
        return 0, str(err)


def _docker(arguments: list[str]) -> tuple[int, str]:
    """跑一条 docker 命令（不抛异常，返回码与输出）。"""
    try:
        done = subprocess.run(
            ["docker", *arguments],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as err:
        return 127, str(err)
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def prepare_workspace(match: Match, ai_id: int, *, gate: bool, debug: bool) -> Path:
    """准备 AI 工作区：开局代码副本 + `.opencode/`（插件与 agent）+ 生成对战域配置。

    每次开局都重扫一遍：`server/` 与 `rescue/` 是"这一局的"。
    """
    workspace = match.workspace_dir(ai_id)
    workspace.mkdir(parents=True, exist_ok=True)
    server = workspace / "server"
    if server.exists():
        force_rmtree(server)
    shutil.copytree(SRC_SERVER, server, ignore=_CLEAN_IGNORE)
    rescue = workspace / "rescue"
    if rescue.exists():
        force_rmtree(rescue)
    dot = workspace / ".opencode"
    for part in ("toolkit", "agents"):
        target = dot / part
        if target.exists():
            force_rmtree(target)
        shutil.copytree(ARENA_DIR / ".opencode" / part, target, ignore=_CLEAN_IGNORE)
    config = {
        "$schema": "https://opencode.ai/config.json",
        "plugins": [
            {
                "package": "./.opencode/toolkit",
                "options": {
                    "agent": "game",
                    "hide": ["webfetch", "websearch", "question", "skill"],
                    "gate": gate,
                    "debug": debug,
                },
            }
        ],
    }
    text = json.dumps(config, ensure_ascii=False, indent=2) + "\n"
    (workspace / "opencode.json").write_text(text, encoding="utf-8")
    return workspace


# ================================================================ 数据面（宿主侧网关）


class _GatewayHandler(BaseHTTPRequestHandler):
    """数据面的一个小 HTTP 处理器：令牌鉴权 → 转发 syscall / 递简报。"""

    server: _GatewayServer

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """静音：这是协议通道，诊断看账本。"""

    def do_POST(self) -> None:
        """POST /game/tool：插件送 syscall 的门。"""
        if self.path != "/game/tool":
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "content": "错误：未知路径。"})
            return
        ai_id = self._authenticate()
        if ai_id is None:
            self._send_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "content": "错误：无效令牌。"})
            return
        body = self._read_body()
        request = body.get("request") if isinstance(body, dict) else None
        if not isinstance(request, str):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "content": '错误：请求体必须是 {"request": "…"}。'},
            )
            return
        gate_msg = self.server.gate(ai_id) if self.server.gate is not None else None
        if gate_msg is not None:
            ledger = self.server.match.ledger
            ledger.log("syscall", ai=ai_id, channel="gate", raw=request, ok=False, preview=gate_msg)
            self._send_json(HTTPStatus.OK, {"ok": False, "content": gate_msg})
            return
        token = self._bearer() or ""
        with self.server.lock:
            ok, content = self.server.match.gateway.handle_tool(token, request)
        self._send_json(HTTPStatus.OK, {"ok": ok, "content": content})

    def do_GET(self) -> None:
        """GET /game/briefing：插件每次请求前拉的页脚简报。"""
        if self.path != "/game/briefing":
            self._send_text(HTTPStatus.NOT_FOUND, "错误：未知路径。")
            return
        ai_id = self._authenticate()
        if ai_id is None:
            self._send_text(HTTPStatus.UNAUTHORIZED, "错误：无效令牌。")
            return
        self._send_text(HTTPStatus.OK, self.server.match.briefing(ai_id))

    # ---- 内部 ----

    def _bearer(self) -> str | None:
        """从 Authorization 头里抠出 Bearer 令牌。"""
        header = self.headers.get("Authorization") or ""
        prefix = "Bearer "
        return header[len(prefix) :] if header.startswith(prefix) else None

    def _authenticate(self) -> int | None:
        """令牌 → AI 编号；无效返回 None。"""
        token = self._bearer()
        return self.server.by_token.get(token) if token else None

    def _read_body(self) -> object:
        """读并解析 JSON 请求体；坏了返回 None。"""
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            return None
        if length <= 0:
            return None
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            return None

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        """回一段 JSON。"""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status: int, text: str) -> None:
        """回一段纯文本。"""
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _GatewayServer(ThreadingHTTPServer):
    """带对局上下文的 HTTP 服务端。"""

    def __init__(
        self,
        address: tuple[str, int],
        match: Match,
        gate: Callable[[int], str | None] | None,
    ) -> None:
        """固定用 `_GatewayHandler`；附上对局与门控回调。"""
        super().__init__(address, _GatewayHandler)
        self.match = match
        self.gate = gate
        self.by_token: dict[str, int] = {tok: ai for ai, tok in match.tokens.items()}
        self.lock = threading.Lock()


class ArenaGateway:
    """宿主侧数据面：一个线程化的小 HTTP 服务，活到对局结束。"""

    def __init__(self, match: Match, *, gate: Callable[[int], str | None] | None = None) -> None:
        """绑一个空闲端口（0.0.0.0，容器经 host.docker.internal 回连）。"""
        self.port = _free_port()
        self._server = _GatewayServer(("0.0.0.0", self.port), match, gate)  # noqa: S104
        self._thread = threading.Thread(target=self._server.serve_forever, name="arena-gateway")
        self._thread.daemon = True
        self._started = False

    def start(self) -> None:
        """开始接客。"""
        self._thread.start()
        self._started = True

    def stop(self) -> None:
        """收摊（幂等）。"""
        if self._started:
            self._server.shutdown()
            self._server.server_close()
            self._started = False


# ================================================================ 控制面（一个 AI 的容器）


class ArenaBox:
    """一个 AI 的容器与它的 opencode 会话（控制面的最小单元）。"""

    def __init__(
        self,
        *,
        match: Match,
        ai_id: int,
        settings: dict[str, Any],
        gateway_url: str,
        model: str,
    ) -> None:
        """记录身份与参数；真正起容器在 `start()`。"""
        self.match = match
        self.ai_id = ai_id
        self.settings = settings
        self.model = model
        self.gateway_url = gateway_url
        self.agent = str(settings.get("agent", "game"))
        self.binary = str(settings["opencode_bin"])
        self.serve_port = int(settings["serve_port"])
        self.host_port = _free_port()
        self.password = secrets.token_urlsafe(18)
        self.name = f"ai-battle-{_safe_name(match.match_id)}-ai{ai_id}"
        self.workspace = match.workspace_dir(ai_id)
        self.session_id: str | None = None
        self._base = f"http://127.0.0.1:{self.host_port}"
        self._live_echo = bool(settings.get("live_log", True))

    # ---- 容器生命周期 ----

    def start(self) -> None:
        """起容器（serve 即容器主进程），并等它就绪。"""
        _docker(["rm", "-f", self.name])  # 清同名残骸（失败无所谓）
        host_workspace = str(self.workspace.resolve()).replace("\\", "/")
        arguments = [
            "run",
            "-d",
            "--name",
            self.name,
            "--cpus",
            str(self.settings["cpus"]),
            "--memory",
            str(self.settings["memory"]),
            "--pids-limit",
            str(self.settings["pids"]),
            "-v",
            f"{host_workspace}:/workspace",
            "-w",
            "/workspace",
            "-p",
            f"127.0.0.1:{self.host_port}:{self.serve_port}",
            "-e",
            f"OPENCODE_PASSWORD={self.password}",
            "-e",
            f"BATTLE_GATEWAY_URL={self.gateway_url}",
            "-e",
            f"BATTLE_TOKEN={self.match.tokens[self.ai_id]}",
            str(self.settings["image"]),
            self.binary,
            "serve",
            "--hostname",
            "0.0.0.0",  # noqa: S104
            "--port",
            str(self.serve_port),
        ]
        if self.settings.get("print_logs"):
            arguments.append("--print-logs")
        code, out = _docker(arguments)
        if code != 0:
            msg = f"容器 {self.name} 启动失败（docker 退出码 {code}）：{out.strip()[:300]}"
            raise RuntimeError(msg)
        self.wait_ready()

    def wait_ready(self) -> None:
        """轮询 /api/info 直到 serve 的 HTTP 起来。"""
        timeout = float(self.settings["ready_timeout_sec"])
        deadline = time.monotonic() + timeout
        detail = ""
        while time.monotonic() < deadline:
            status, text = self._http("GET", "/api/info", timeout=2.0)
            if status == HTTPStatus.OK:
                return
            detail = f"{status} {text[:80]}"
            time.sleep(0.5)
        msg = f"容器 {self.name} 的 serve 在 {timeout:g}s 内未就绪：{detail}"
        raise RuntimeError(msg)

    def alive(self) -> bool:
        """容器是否还在跑。"""
        code, out = _docker(["inspect", "-f", "{{.State.Running}}", self.name])
        return code == 0 and out.strip() == "true"

    def stop(self) -> None:
        """清掉容器（幂等，不抛）。"""
        _docker(["rm", "-f", self.name])

    # ---- 控制面 ----

    def create_session(self) -> str:
        """让 serve 生成正牌会话 id（客户端自造 id 会被免费档的来源校验拒掉）。"""
        payload = {"agent": self.agent, "model": split_model(self.model)}
        status, text = self._http("POST", "/api/session", payload)
        if status != HTTPStatus.OK:
            msg = f"建会话失败（HTTP {status}）：{text[:200]}"
            raise RuntimeError(msg)
        data: Any = json.loads(text)
        data = data if isinstance(data, dict) else {}
        session = data.get("data") if isinstance(data.get("data"), dict) else {}
        session_id = session.get("id")
        if not isinstance(session_id, str) or not session_id.startswith("ses"):
            msg = f"建会话返回异常：{text[:200]}"
            raise RuntimeError(msg)
        self.session_id = session_id
        return session_id

    def rpc(self, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """打一枪插件控制面（`POST /api/rpc/arena/<method>`），返回 output 字典。"""
        status, text = self._http("POST", f"/api/rpc/arena/{method}", {"input": payload or {}})
        if status != HTTPStatus.OK:
            msg = f"RPC {method} 失败（HTTP {status}）：{text[:200]}"
            raise RuntimeError(msg)
        data: Any = json.loads(text)
        output = data.get("output") if isinstance(data, dict) else None
        if isinstance(output, dict):
            return output
        msg = f"RPC {method} 返回异常：{text[:200]}"
        raise RuntimeError(msg)

    def run_turn(self, prompt: str, *, kind: str = "turn", limit: float | None = None) -> Turn:
        """把一条消息送进会话（一次唤醒 = 一个回合），边跑边直播，等它干完或超时。

        `limit` 给单回合一个更短的超时（侦察轮用）；None 用 turn_timeout_sec。
        输出同时流向：内存（转写）、`runs/<id>/live/ai-N.txt`（观战直播）、
        内核终端（每行 `[AI-N]` 前缀，`live_log=false` 可关）。
        """
        if self.session_id is None:
            msg = "会话尚未创建，无法驱动回合。"
            raise RuntimeError(msg)
        command = [
            "docker",
            "exec",
            self.name,
            self.binary,
            "run",
            "--auto",
            "--server",
            f"http://127.0.0.1:{self.serve_port}",
            "--session",
            self.session_id,
            "--agent",
            self.agent,
            "-m",
            self.model,
            prompt,
        ]
        timeout = float(limit) if limit is not None else float(self.settings["turn_timeout_sec"])
        started = time.monotonic()
        sink = self._open_live(kind, limit)
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,  # 裸管道：有数据即回，直播不用等缓冲
        )
        stream = proc.stdout
        if stream is None:  # pragma: no cover - PIPE 一定拿得到
            msg = "无法获取回合输出通道。"
            raise RuntimeError(msg)
        lines: list[str] = []

        def on_line(raw: str) -> None:
            """一行输出：剥离 ANSI、收进转写、写直播文件、转播终端。"""
            line = strip_ansi(raw).rstrip()
            if not line:
                return
            lines.append(line + "\n")
            if sink is not None:
                sink.write(line)
            if self._live_echo:
                self._echo(line)

        pump = threading.Thread(
            target=_drain_output, args=(stream, on_line), name=f"turn-ai-{self.ai_id}", daemon=True
        )
        pump.start()
        timed_out, code = self._wait_or_kill(proc, timeout)
        pump.join(timeout=5.0)
        seconds = time.monotonic() - started
        text = "".join(lines)
        if timed_out:
            error = f"回合超过 {timeout:g}s"
        elif code != 0:
            error = f"run 退出码 {code}"
        else:
            error = ""
        if sink is not None:
            sink.close(ok=not error, seconds=seconds, error=error)
        return Turn(ok=not error, text=text, seconds=seconds, error=error, truncated=timed_out)

    def _wait_or_kill(
        self, proc: subprocess.Popen[bytes], timeout: float
    ) -> tuple[bool, int | None]:
        """等回合收工；到点就收尸。返回（是否超时, 退出码）。"""
        try:
            return False, proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._kill_turn(proc)
            with contextlib.suppress(subprocess.TimeoutExpired):
                return True, proc.wait(timeout=5.0)
            return True, None

    def _kill_turn(self, proc: subprocess.Popen[bytes]) -> None:
        """超时收尸：杀本地 docker 客户端，再进容器把还在跑的 run 补一刀。"""
        with contextlib.suppress(OSError):
            proc.kill()
        _docker(["exec", self.name, "pkill", "-f", "opencode run"])

    def _open_live(self, kind: str, limit: float | None) -> _LiveSink | None:
        """开一个直播文件；开不出来不影响回合（转写照样落 sessions/）。"""
        path = self.match.match_dir / "live" / f"ai-{self.ai_id}.txt"
        try:
            return _LiveSink(path, self.ai_id, kind, limit)
        except OSError:
            return None

    def _echo(self, line: str) -> None:
        """终端实时转播一行（多个 AI 并行时靠全局锁保证整行不互相踩）。"""
        with _ECHO_LOCK:
            self.match.log(f"[AI-{self.ai_id}] {line}")

    # ---- 内部 ----

    def _http(
        self,
        method: str,
        path: str,
        payload: object = None,
        *,
        timeout: float = 15.0,
    ) -> tuple[int, str]:
        """向自己容器里的 serve 走一条 HTTP。"""
        return _http_call(
            self._base, method, path, password=self.password, payload=payload, timeout=timeout
        )


# ================================================================ 驱动


class ArenaDriver:
    """OpenCode 容器驱动：实现 Driver 协议（attach / on_wake / detach）。

    默认回合可并行（`parallel_turns`，settings 可关）；`live_log` 控制终端转播。
    """

    def __init__(self, settings: dict[str, Any], models: dict[str, str]) -> None:
        """记录参数；真正的接线在 `attach()`。"""
        self.settings = settings
        self.models = models
        self.match: Match | None = None
        self.gateway: ArenaGateway | None = None
        self.boxes: dict[int, ArenaBox] = {}
        self.parallel_turns = bool(settings.get("parallel_turns", True))
        self._gate = bool(settings.get("gate", True))
        self._scouted: set[int] = set()
        self._gate_open_sent: set[int] = set()
        self._turn_no: dict[int, int] = {}

    # ---- 生命周期 ----

    def attach(self, match: Match) -> None:
        """准备每个 AI 的工作区、起数据面网关、拉起容器并建会话。"""
        self.match = match
        models: dict[int, str] = {}
        for ai_id in match.config.ai_ids():
            model = self.models.get(str(ai_id), "")
            if not model:
                msg = f"arena 驱动需要为每个 AI 配模型（config.models[{ai_id!r}] 缺失）"
                raise RuntimeError(msg)
            models[ai_id] = model
        gateway = ArenaGateway(match, gate=self._gate_reason if self._gate else None)
        gateway.start()
        self.gateway = gateway
        try:
            for ai_id, model in models.items():
                debug = bool(self.settings.get("debug"))
                prepare_workspace(match, ai_id, gate=self._gate, debug=debug)
                box = ArenaBox(
                    match=match,
                    ai_id=ai_id,
                    settings=self.settings,
                    gateway_url=f"http://host.docker.internal:{gateway.port}",
                    model=model,
                )
                box.start()
                session_id = box.create_session()
                registered = True
                try:
                    box.rpc("registerSession", {"session_id": session_id, "ai_id": ai_id})
                except RuntimeError:
                    registered = False
                match.ledger.log(
                    "arena_up",
                    ai=ai_id,
                    container=box.name,
                    port=box.host_port,
                    session=session_id,
                    registered=registered,
                )
                self.boxes[ai_id] = box
        except Exception as err:  # 启动链的任何异常都先收尸再上抛
            self.detach()
            msg = f"arena 启动失败：{err}"
            raise RuntimeError(msg) from err
        match.ledger.log("arena_gateway", port=gateway.port)

    def on_wake(self, ai_id: int) -> None:
        """唤醒一个 AI：组文 → 发回合 → 记卷宗；侦察轮收工即开闸。"""
        match = self._match()
        box = self.boxes.get(ai_id)
        if box is None:
            return
        if match.state == "recovery":
            if ai_id != match.recovery_owner:
                return  # 抢救中只有 owner 有事可做
            if self._gate and ai_id not in self._scouted:
                self._complete_scout(box, ok=False, seconds=0.0, note="抢救兜底：先行开闸")
            kind, text = "rescue", match.briefing(ai_id)
        else:
            kind, text = self.wake_text(ai_id)
        limit = self._scout_limit() if kind == "scout" else None
        match.ledger.log("wake_start", ai=ai_id, kind=kind, limit=limit)
        try:
            turn = box.run_turn(text, kind=kind, limit=limit)
        except RuntimeError as err:
            match.ledger.log("wake_fail", ai=ai_id, kind=kind, error=str(err))
            return
        transcript = self._save_turn(ai_id, kind, turn)
        match.ledger.log(
            "wake",
            ai=ai_id,
            kind=kind,
            ok=turn.ok,
            seconds=round(turn.seconds, 1),
            truncated=turn.truncated,
            preview=preview(turn.text),
            transcript=str(transcript),
        )
        if kind == "gate_open" and turn.ok:
            self._gate_open_sent.add(ai_id)
        if kind == "scout":
            note = ""
            if turn.truncated and limit is not None:
                note = f"到点强制收工（限时 {limit:g}s）"
            self._complete_scout(box, ok=turn.ok, seconds=turn.seconds, note=note)

    def detach(self) -> None:
        """对局结束：停容器、关网关（幂等，不抛）。"""
        for box in self.boxes.values():
            box.stop()
        self.boxes.clear()
        gateway = self.gateway
        self.gateway = None
        if gateway is not None:
            gateway.stop()
        if self.match is not None:
            self.match.ledger.log("arena_down")

    # ---- 回合一侧的判断 ----

    def wake_text(self, ai_id: int) -> tuple[str, str]:
        """选本回合的开场白：侦察 / 开闸 / 常规（抢救那条在 on_wake 里特判）。"""
        if not self._gate:
            return "wake", WAKE_TEXT
        if ai_id not in self._scouted:
            return "scout", scout_text(self._scout_limit())
        if ai_id not in self._gate_open_sent:
            return "gate_open", GATE_OPEN_TEXT
        return "wake", WAKE_TEXT

    def _scout_limit(self) -> float | None:
        """侦察轮限时（秒）；settings 里没写或非正数 = 不限时。"""
        raw = self.settings.get("scout_timeout_sec")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None
        return float(raw) if raw > 0 else None

    def scouted(self, ai_id: int) -> bool:
        """该 AI 的侦察轮是否已收工（门已开）。"""
        return ai_id in self._scouted

    def _gate_reason(self, ai_id: int) -> str | None:
        """网关兜底：门还没开的 AI 一律吃同一句闭门羹。"""
        return GATE_REJECT_TEXT if ai_id not in self._scouted else None

    def _complete_scout(self, box: ArenaBox, *, ok: bool, seconds: float, note: str = "") -> None:
        """侦察收工：开闸（插件 setPhase play）+ 记账。"""
        match = self._match()
        ai_id = box.ai_id
        self._scouted.add(ai_id)
        match.ledger.log("scout_done", ai=ai_id, ok=ok, seconds=round(seconds, 1), note=note)
        try:
            output = box.rpc("setPhase", {"phase": "play"})
        except RuntimeError as err:
            match.ledger.log("gate_fail", ai=ai_id, error=str(err))
            return
        match.ledger.log("gate_open", ai=ai_id, phase=str(output.get("phase", "")))

    def _save_turn(self, ai_id: int, kind: str, turn: Turn) -> Path:
        """把回合输出存进 runs/<id>/sessions/ai-N/（诊断与 M4 观测用）。"""
        match = self._match()
        folder = match.match_dir / "sessions" / f"ai-{ai_id}"
        folder.mkdir(parents=True, exist_ok=True)
        self._turn_no[ai_id] = self._turn_no.get(ai_id, 0) + 1
        stamp = time.strftime("%H%M%S")
        path = folder / f"{self._turn_no[ai_id]:02d}-{kind}-{stamp}.txt"
        header = (
            f"# kind={kind} ok={turn.ok} truncated={turn.truncated} "
            f"seconds={turn.seconds:.1f} error={turn.error or '-'}\n\n"
        )
        path.write_text(header + turn.text, encoding="utf-8")
        return path

    def _match(self) -> Match:
        """取对局引用（attach 之前没有）。"""
        if self.match is None:
            msg = "ArenaDriver 尚未 attach 到对局。"
            raise RuntimeError(msg)
        return self.match
