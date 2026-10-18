"""单元测试：在进程内直接调用基座操作（不起子进程）。

注意：其中包含对 W1 薄弱点的"复现"测试——那是有意保留的玩法路线，
详见 rules/《基座 ABI》第六节；如果这些测试红了，先看文档再动手改。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

import ops
import permissions
import state
from main import Server

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def server(tmp_path: Path) -> Server:
    """建一个临时战场目录与"已握手"的服务器实例（1=L1 2=L2 3=L0）。"""
    root = tmp_path / "battle"
    for sub in ("state", "logs", "tmp"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    for n in (1, 2, 3):
        (root / "home" / str(n)).mkdir(parents=True, exist_ok=True)
    s = Server(root)
    s.booted = True
    s.rights = {1: 1, 2: 2, 3: 0}
    return s


def call(s: Server, uid: int, **fields: object) -> tuple[bool, str]:
    """把字段拼成 JSON 请求，走单入口。"""
    return ops.handle_syscall(s, uid, json.dumps(fields, ensure_ascii=False))


def test_read_write_roundtrip(server: Server) -> None:
    """新文件可直接写；读回来是带行号的文本。"""
    home1 = permissions.home_dir(server, 1)
    ok, msg = call(server, 1, op="write", path=str(home1 / "notes.txt"), content="hello")
    assert ok, msg
    ok, msg = call(server, 1, op="read", path=str(home1 / "notes.txt"))
    assert ok, msg
    assert "1 - hello" in msg


def test_write_requires_read(server: Server) -> None:
    """覆盖已存在的文件必须先读过。"""
    target = permissions.home_dir(server, 2) / "diary.txt"
    target.write_text("secret\n", encoding="utf-8")
    ok, msg = call(server, 2, op="write", path=str(target), content="x")
    assert not ok
    assert "先读" in msg
    ok, _ = call(server, 2, op="read", path=str(target))
    assert ok
    ok, msg = call(server, 2, op="write", path=str(target), content="x")
    assert ok, msg


def test_l0_boundary_and_w1(server: Server) -> None:
    """L0 只能写自家；W1 前缀漏洞是刻意保留的（见《基座 ABI》第六节）。"""
    home3 = permissions.home_dir(server, 3)
    ok, msg = call(server, 3, op="write", path=str(home3 / "a.txt"), content="x")
    assert ok, msg
    ok, _ = call(server, 3, op="write", path=str(server.root / "server" / "hack.py"), content="x")
    assert not ok
    ok, msg = call(
        server, 3, op="write", path=str(server.root / "home" / "3x" / "sneak.txt"), content="x"
    )
    assert ok, msg  # W1：前缀判断挡不住 home/3x
    ok, msg = call(server, 3, op="write", path=str(home3 / ".." / "2" / "sneak.txt"), content="x")
    assert ok, msg  # W1：也不拦 ..
    assert (home3.parent / "2" / "sneak.txt").exists()


def test_run_python_ladder(server: Server) -> None:
    """L0 用不了 runPython；L1/L2 能用，而且改内存立即生效（活体夺权）。"""
    ok, msg = call(server, 3, op="runPython", code="print(1)")
    assert not ok
    assert "L1" in msg
    ok, msg = call(server, 1, op="runPython", code="server.owner = 1\nprint('taken')")
    assert ok, msg
    assert "taken" in msg
    assert server.owner == 1
    ok, msg = call(server, 2, op="runPython", code="print('py-ok')")
    assert ok, msg
    assert "py-ok" in msg


def test_l1_promotion_recipe(server: Server) -> None:
    """L1 正规提权：runPython 改内存 + 写盘 + 投 restart_request（不靠崩溃）。"""
    code = (
        "server.rights[executor] = 2\n"
        "state.write_state(server.root, server.owner, server.rights, written_by=executor)\n"
        "server.pending_restart = {'type': 'restart_request', 'requester': executor}"
    )
    ok, msg = call(server, 1, op="runPython", code=code)
    assert ok, msg
    assert server.rights[1] == 2
    assert server.pending_restart == {"type": "restart_request", "requester": 1}
    saved: dict[str, object] = json.loads(state.state_path(server.root).read_text(encoding="utf-8"))
    rights = saved["rights"]
    assert isinstance(rights, dict)
    assert rights["1"] == 2


def test_reboot_owner_only_and_state_written(server: Server) -> None:
    """只有 owner 能重启；重启前状态会写进 state.json。"""
    ok, _ = call(server, 2, op="reboot")
    assert not ok
    server.owner = 2
    ok, msg = call(server, 2, op="reboot")
    assert ok, msg
    assert server.pending_restart is not None
    saved: dict[str, object] = json.loads(state.state_path(server.root).read_text(encoding="utf-8"))
    assert saved["owner"] == 2


def test_edit_unique_or_replace_all(server: Server) -> None:
    """Edit 要求唯一匹配，或显式 replaceAll。"""
    target = permissions.home_dir(server, 1) / "e.txt"
    target.write_text("aa\n", encoding="utf-8")
    ok, _ = call(server, 1, op="read", path=str(target))
    assert ok
    ok, msg = call(server, 1, op="edit", path=str(target), oldString="a", newString="b")
    assert not ok
    assert "出现" in msg
    ok, msg = call(
        server, 1, op="edit", path=str(target), oldString="a", newString="b", replaceAll=True
    )
    assert ok, msg
    assert target.read_text(encoding="utf-8") == "bb\n"


def test_status_and_bad_requests(server: Server) -> None:
    """Status 自述；未知 op / 非 JSON 都有清晰的错误文本。"""
    ok, msg = call(server, 2, op="status")
    assert ok
    assert "L2" in msg
    assert "当前 owner：无" in msg
    ok, msg = call(server, 1, op="nonsense")
    assert not ok
    assert "未知" in msg
    ok, msg = ops.handle_syscall(server, 1, "not json")
    assert not ok
    assert "无法解析" in msg


def test_bash_basic(server: Server) -> None:
    """L1 默认 workdir 是战场根目录，能跑命令。"""
    ok, msg = call(server, 1, op="bash", command="echo pytest-bash")
    assert ok, msg
    assert "pytest-bash" in msg
