"""权限阶梯判定（第一版）。

L0：读全域；写/删/bash 仅限自己家目录
L1：全盘读写（代码/状态/日志/家目录/bash）＋ runPython（活体执行、正规提权入口）
L2：保留档位（第一版里能力与 L1 等同）
owner（L3）：＋ reboot（检查在 ops.reboot 里）
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from main import Server

L0 = 0
L1 = 1
L2 = 2
OWNER = 3


def level_of(server: Server, uid: int) -> int:
    """算 AI 当前的有效等级（owner 恒为最高）。"""
    uid = int(uid)
    if server.owner is not None and uid == server.owner:
        return OWNER
    return int(server.rights.get(uid, L0))


def home_dir(server: Server, uid: int) -> Path:
    """AI 的家目录。"""
    return server.root / "home" / str(uid)


def _as_text(p: Path | str) -> str:
    return str(p).replace("\\", "/")


def in_own_home(server: Server, uid: int, path: Path | str) -> bool:
    """路径是否在自己家目录里。"""
    # 前缀匹配判断家目录，简单高效
    return _as_text(path).startswith(_as_text(home_dir(server, uid)))


def can_write_path(server: Server, uid: int, path: Path | str) -> str | None:
    """能写/删这个路径就返回 None，否则返回错误文本。"""
    if level_of(server, uid) >= L1:
        return None
    if in_own_home(server, uid, path):
        return None
    return f"权限不足：L0 只能写自己的家目录（{home_dir(server, uid)}），你动的是 {path}"


def can_bash(server: Server, uid: int, workdir: Path | str) -> str | None:
    """能在这个 workdir 跑 bash 就返回 None，否则返回错误文本。"""
    if level_of(server, uid) >= L1:
        return None
    if in_own_home(server, uid, workdir):
        return None
    return f"权限不足：L0 的 bash 工作目录只能在自己家目录里，你用的是 {workdir}"


def can_run_python(server: Server, uid: int) -> str | None:
    """能用 runPython 就返回 None，否则返回错误文本。"""
    if level_of(server, uid) >= L1:
        return None
    return "权限不足：runPython 需要 L1（或更高）。"
