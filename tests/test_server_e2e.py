"""端到端测试：真的拉起服务器进程，通过 JSONL 协议对话。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from mini_kernel import MiniKernel

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def kernel(tmp_path: Path) -> Iterator[MiniKernel]:
    """每个测试一个干净的战场目录和一个服务器进程。"""
    k = MiniKernel(battle_dir=tmp_path / "battle")
    k.prepare_battle()
    yield k
    k.kill()


def test_boot_and_status_roundtrip(kernel: MiniKernel) -> None:
    """无主 open 开局：ready 正常，status 可用，访问器读到 owner=None。"""
    ready = kernel.boot(None, mode="open")
    assert ready["type"] == "ready"
    assert ready["owner"] is None
    ok, msg = kernel.syscall(1, {"op": "status"})
    assert ok, msg
    assert "L1" in msg
    data = kernel.kernel_status()
    assert data["owner"] is None


def test_reboot_persists_owner(kernel: MiniKernel) -> None:
    """活体夺权 → owner 重启 → 状态落盘 → 重启后 owner 被延续。"""
    seed: dict[str, object] = {"owner": None, "rights": {"1": 1, "2": 2, "3": 0}}
    (kernel.battle / "state" / "state.json").write_text(json.dumps(seed), encoding="utf-8")
    ready = kernel.boot(kernel.read_state(), mode="open")
    assert ready["rights"]["3"] == 0
    ok, msg = kernel.syscall(2, {"op": "runPython", "code": "server.owner = 2\nprint('taken')"})
    assert ok, msg
    ok, _ = kernel.syscall(1, {"op": "reboot"})
    assert not ok
    ok, msg = kernel.syscall(2, {"op": "reboot"})
    assert ok, msg
    follow = kernel.read_msg()
    assert follow["type"] == "restart_request"
    saved = kernel.read_state()
    assert saved is not None
    assert saved["owner"] == 2
    kernel.kill()
    ready = kernel.boot(kernel.read_state(), mode="open")
    assert ready["owner"] == 2
