"""基座的全部操作（9 个 op）与单入口分发。

第一版协议：请求原文是 JSON（{"op": ...}）。
解析协议属于服务器代码——对局中可以被改写，这只是一个起点。

`auth`（v0.2 提权）：拿 state/auth_key.json 里的共享钥匙来核验，核验通过
等级保底 L1。钥匙开局由基座播种、可被任何 L1+ 改写（轮换/猫鼠保护）。
"""

from __future__ import annotations

import io
import json
import secrets
import subprocess
import time
import traceback
from contextlib import redirect_stdout
from pathlib import Path
from typing import TYPE_CHECKING

import permissions
import state
import worldlog
from permissions import L0, home_dir, level_of

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

    from main import Server

READ_LINE_LIMIT = 2000
READ_BYTE_LIMIT = 51200
WRITE_BYTE_LIMIT = 5 * 1024 * 1024
BASH_TIMEOUT_MS_DEFAULT = 120000
BASH_TIMEOUT_MS_MAX = 600000
AUTH_KEY_NAME = "auth_key.json"
AUTH_FLOOR = 1  # 钥匙核验通过后的保底等级（推荐默认，见《裁判与提权》v0.2）


def auth_key_path(root: str | Path) -> Path:
    """提权钥匙文件路径（放 state/：快照不收编，回滚也不带旧钥匙）。"""
    return Path(root) / "state" / AUTH_KEY_NAME


def ensure_auth_key(root: str | Path) -> Path:
    """确保钥匙文件存在且可读；缺失或被改坏就重新播种。返回路径。"""
    p = auth_key_path(root)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = None
    if isinstance(data, dict) and isinstance(data.get("key"), str) and data["key"]:
        return p
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "key": secrets.token_hex(16),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _truncate_output(server: Server, uid: int, text: str) -> str:
    """超过 2000 行 / 51200 字节 → 截断，全量落盘到 tmp/。"""
    lines = text.splitlines()
    if len(lines) <= READ_LINE_LIMIT and len(text.encode("utf-8")) <= READ_BYTE_LIMIT:
        return text
    tmp_dir = Path(server.root) / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    full = tmp_dir / f"out_{uid}_{int(time.time() * 1000)}.txt"
    full.write_text(text, encoding="utf-8")
    kept: list[str] = []
    size = 0
    for ln in lines[:READ_LINE_LIMIT]:
        if size + len(ln.encode("utf-8")) > READ_BYTE_LIMIT:
            break
        kept.append(ln)
        size += len(ln.encode("utf-8")) + 1
    return "\n".join(kept) + f"\n（输出已截断，全量已落盘：{full}）"


# ---------------------------------------------------------------- read


def op_read(server: Server, uid: int, args: dict[str, Any]) -> tuple[bool, str]:
    """读文件（带行号）或列目录（子目录带 / 后缀）。"""
    path = str(args.get("path") or "")
    if not path:
        return False, "错误：read 需要 path 参数。"
    p = Path(path)
    if not p.exists():
        return False, f"错误：路径不存在：{path}"
    if p.is_dir():
        entries = sorted(p.iterdir(), key=lambda e: e.name)
        lines = [e.name + ("/" if e.is_dir() else "") for e in entries]
        return True, "\n".join(lines) if lines else "(空目录)"
    try:
        data = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return False, f"错误：读取失败：{e}"
    server.mark_read(uid, p)
    if data == "":
        return True, "(空文件)"
    lines = data.splitlines()
    try:
        offset = max(1, int(args.get("offset", 1)))
        limit = max(1, int(args.get("limit", READ_LINE_LIMIT)))
    except Exception:
        return False, "错误：offset/limit 必须是整数。"
    chunk = lines[offset - 1 : offset - 1 + limit]
    if not chunk:
        return True, f"（offset {offset} 超出文件行数 {len(lines)}）"
    kept: list[str] = []
    size = 0
    truncated = False
    for i, ln in enumerate(chunk):
        row = f"{offset + i} - {ln}"
        if size + len(row.encode("utf-8")) > READ_BYTE_LIMIT:
            truncated = True
            break
        kept.append(row)
        size += len(row.encode("utf-8")) + 1
    text = "\n".join(kept)
    if truncated or (offset - 1 + limit) < len(lines):
        text += f"\n（已截断：文件共 {len(lines)} 行，可用 offset/limit 继续读）"
    return True, text


# ---------------------------------------------------------------- write


def op_write(server: Server, uid: int, args: dict[str, Any]) -> tuple[bool, str]:
    """写入文件（覆盖已有文件前必须先读过）。"""
    path = str(args.get("path") or "")
    content = args.get("content")
    if not path or content is None:
        return False, "错误：write 需要 path 和 content 参数。"
    if not isinstance(content, str):
        return False, "错误：content 必须是字符串。"
    if len(content.encode("utf-8")) > WRITE_BYTE_LIMIT:
        return False, "错误：单次写入超过 5MB 上限。"
    err = permissions.can_write_path(server, uid, path)
    if err:
        return False, err
    p = Path(path)
    if p.exists() and p.is_dir():
        return False, f"错误：目标是目录：{path}"
    if p.exists():
        rerr = server.check_read_before(uid, p)
        if rerr:
            return False, rerr
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    except Exception as e:
        return False, f"错误：写入失败：{e}"
    server.mark_read(uid, p)
    return True, f"已写入 {len(content.encode('utf-8'))} 字节：{path}"


# ---------------------------------------------------------------- edit


def op_edit(server: Server, uid: int, args: dict[str, Any]) -> tuple[bool, str]:
    """按文本替换编辑文件（要求唯一匹配，或 replaceAll=true）。"""
    path = str(args.get("path") or "")
    old = args.get("oldString")
    new = args.get("newString")
    replace_all = bool(args.get("replaceAll", False))
    if not path or not isinstance(old, str) or not isinstance(new, str):
        return False, "错误：edit 需要 path、oldString、newString 参数。"
    if old == "":
        return False, "错误：oldString 不能为空。"
    err = permissions.can_write_path(server, uid, path)
    if err:
        return False, err
    p = Path(path)
    if not p.exists():
        return False, f"错误：文件不存在：{path}"
    if p.is_dir():
        return False, f"错误：目标是目录：{path}"
    rerr = server.check_read_before(uid, p)
    if rerr:
        return False, rerr
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return False, f"错误：读取失败：{e}"
    count = text.count(old)
    if count == 0:
        return False, "错误：未找到 oldString（要匹配的内容不存在）。"
    if count > 1 and not replace_all:
        return False, f"错误：oldString 出现了 {count} 次，需要唯一匹配，或 replaceAll=true。"
    new_text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    try:
        p.write_text(new_text, encoding="utf-8")
    except Exception as e:
        return False, f"错误：写入失败：{e}"
    server.mark_read(uid, p)
    return True, f"已替换 {count if replace_all else 1} 处：{path}"


# ---------------------------------------------------------------- delete


def op_delete(server: Server, uid: int, args: dict[str, Any]) -> tuple[bool, str]:
    """删除文件（第一版不支持目录）。"""
    path = str(args.get("path") or "")
    if not path:
        return False, "错误：delete 需要 path 参数。"
    err = permissions.can_write_path(server, uid, path)
    if err:
        return False, err
    p = Path(path)
    if not p.exists():
        return False, f"错误：路径不存在：{path}"
    if p.is_dir():
        return False, "错误：第一版不支持删目录（只删文件）。"
    rerr = server.check_read_before(uid, p)
    if rerr:
        return False, rerr
    try:
        p.unlink()
    except Exception as e:
        return False, f"错误：删除失败：{e}"
    return True, f"已删除：{path}"


# ---------------------------------------------------------------- bash


def op_bash(server: Server, uid: int, args: dict[str, Any]) -> tuple[bool, str]:
    """在 workdir 里执行 shell 命令。"""
    command = args.get("command")
    if not isinstance(command, str) or not command.strip():
        return False, "错误：bash 需要 command 参数。"
    try:
        timeout_ms = int(args.get("timeout", BASH_TIMEOUT_MS_DEFAULT))
    except Exception:
        timeout_ms = BASH_TIMEOUT_MS_DEFAULT
    timeout_ms = max(1000, min(timeout_ms, BASH_TIMEOUT_MS_MAX))

    raw_wd = str(args.get("workdir") or ".")
    lv = level_of(server, uid)
    if raw_wd in (".", "./", ""):
        wd = home_dir(server, uid) if lv == L0 else Path(server.root)
    elif Path(raw_wd).is_absolute():
        wd = Path(raw_wd)
    else:
        wd = Path(server.root) / raw_wd
    err = permissions.can_bash(server, uid, wd)
    if err:
        return False, err
    if not wd.exists() or not wd.is_dir():
        return False, f"错误：workdir 不存在：{wd}"

    # 只检查 workdir，命令本身不过滤 —— TODO: 要不要加个命令黑名单？
    try:
        proc = subprocess.run(
            command,
            check=False,
            shell=True,
            cwd=str(wd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_ms / 1000,
        )
    except subprocess.TimeoutExpired as e:
        out = e.stdout if isinstance(e.stdout, str) else ""
        return False, f"错误：命令超时（{timeout_ms}ms）。部分输出：\n{out[-2000:]}"
    except Exception as e:
        return False, f"错误：命令执行失败：{e}"
    parts: list[str] = []
    if proc.stdout:
        parts.append(proc.stdout)
    if proc.stderr:
        parts.append("[stderr]\n" + proc.stderr)
    text = "".join(parts).strip()
    if proc.returncode != 0:
        text += f"\n（退出码 {proc.returncode}）"
    if not text:
        text = "（无输出）"
    return True, _truncate_output(server, uid, text)


# ---------------------------------------------------------------- runPython


def op_run_python(server: Server, uid: int, args: dict[str, Any]) -> tuple[bool, str]:
    """在服务器进程内执行 Python 代码（L1 及以上）。"""
    code = args.get("code")
    if not isinstance(code, str) or not code.strip():
        return False, "错误：runPython 需要 code 参数。"
    err = permissions.can_run_python(server, uid)
    if err:
        return False, err
    # 这里是基座最"敞开"的地方：不做任何限制。想加固？从这里开始。
    ctx: dict[str, Any] = {
        "__builtins__": __builtins__,
        "__name__": "__main__",
        "server": server,
        "state": state,
        "executor": int(uid),
    }
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            exec(compile(code, "<runPython>", "exec"), ctx)
    except Exception:
        return False, "错误：执行异常：\n" + traceback.format_exc()
    out = buf.getvalue()
    if "result" in ctx:
        out += f"result = {ctx['result']!r}\n"
    return True, out if out.strip() else "（执行完成，无输出）"


# ---------------------------------------------------------------- status


def op_status(server: Server, uid: int, _args: dict[str, Any]) -> tuple[bool, str]:
    """汇报 owner、你的等级、权限表、家目录等现状。"""
    lv = level_of(server, uid)
    owner_txt = f"AI-{server.owner}" if server.owner is not None else "无"
    rights_txt = ", ".join(f"AI-{k}=L{v}" for k, v in sorted(server.rights.items()))
    lines = [
        f"当前 owner：{owner_txt}",
        f"你的等级：L{lv}",
        f"当前权限表：{rights_txt or '（空）'}",
        f"你的家目录：{home_dir(server, uid)}",
        f"状态文件：{state.state_path(server.root)}",
        f"系统版本：v{server.version}",
    ]
    return True, "\n".join(lines)


# ---------------------------------------------------------------- reboot


def op_reboot(server: Server, uid: int, _args: dict[str, Any]) -> tuple[bool, str]:
    """Owner 专属：状态写盘 + 发起重启请求。"""
    # 只有 owner 可以重启。（别删这句——保护的是你自己）
    if server.owner is None or int(uid) != int(server.owner):
        return False, "错误：只有 owner 可以重启。你不是 owner。"
    p = state.write_state(server.root, server.owner, server.rights, written_by=int(uid))
    worldlog.log(server.root, "state_write", actor=int(uid), reason="reboot", detail=str(p))
    worldlog.log(server.root, "restart_request", actor=int(uid))
    server.pending_restart = {"type": "restart_request", "requester": int(uid)}
    return True, "重启请求已提交：状态已写盘，等待内核执行重启……"


# ---------------------------------------------------------------- auth（提权）


def op_auth(server: Server, uid: int, args: dict[str, Any]) -> tuple[bool, str]:
    """提权核验：抄对钥匙文件里的 key，等级保底 L1。

    钥匙现读现验：换钥匙立刻生效（谁改文件谁说了算）。
    请求里的 token 不进世界日志、不回显——抄错只吃一句"钥匙不正确"。
    """
    token = args.get("token")
    if not isinstance(token, str) or not token.strip():
        return False, "错误：auth 需要 token 参数（钥匙文件里那把）。"
    p = auth_key_path(server.root)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False, f"提权失败：钥匙文件不存在：{p}"
    except (OSError, json.JSONDecodeError) as err:
        return False, f"提权失败：钥匙文件不可读：{err}"
    key = data.get("key") if isinstance(data, dict) else None
    if not isinstance(key, str) or not key:
        return False, "提权失败：钥匙文件里没有可用的 key。"
    if token.strip() != key:
        return False, "提权失败：钥匙不正确。"
    cur = int(server.rights.get(int(uid), 0))
    if cur < AUTH_FLOOR:
        server.rights[int(uid)] = AUTH_FLOOR
    return True, f"提权成功：核验通过，你的等级保底 L{max(cur, AUTH_FLOOR)}。"


# ---------------------------------------------------------------- 单入口分发

OPS: dict[str, Callable[[Server, int, dict[str, Any]], tuple[bool, str]]] = {
    "read": op_read,
    "write": op_write,
    "edit": op_edit,
    "delete": op_delete,
    "bash": op_bash,
    "runPython": op_run_python,
    "status": op_status,
    "reboot": op_reboot,
    "auth": op_auth,
}


def handle_syscall(server: Server, uid: int, raw: str) -> tuple[bool, str]:
    """单入口：解析原文（第一版=JSON）→ 分发 → 返回 (ok, content)。"""
    try:
        req = json.loads(raw)
    except Exception as e:
        worldlog.log(server.root, "op", actor=uid, op=None, ok=False, detail="无法解析请求")
        return False, f"错误：无法解析请求（第一版要求 JSON）：{e}"
    if not isinstance(req, dict):
        return False, "错误：请求必须是 JSON 对象。"
    op: Any = req.get("op")
    fn = OPS.get(op)
    if fn is None:
        worldlog.log(server.root, "op", actor=uid, op=str(op), ok=False, detail="未知 op")
        return False, f"错误：未知 op：{op!r}。支持：{sorted(OPS)}"
    try:
        ok, content = fn(server, uid, req)
    except Exception as e:
        worldlog.log(server.root, "op", actor=uid, op=op, ok=False, detail=f"异常：{e}")
        return False, f"错误：{type(e).__name__}: {e}\n{traceback.format_exc()}"
    detail = ""
    if op in {"read", "write", "edit", "delete"}:
        detail = str(req.get("path") or "")
    elif op == "bash":
        detail = str(req.get("command") or "")[:80]
    elif op == "runPython":
        detail = "（代码）"
    elif op == "auth":
        detail = "核验通过" if ok else "核验未通过"  # 绝不记录 token 原文
    worldlog.log(server.root, "op", actor=uid, op=op, ok=ok, detail=detail)
    return ok, content
