"""抢救室 v0.2：工作区镜像搬运与「不解析原文」的门（内核单元测试）。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from recovery import RecoveryRoom

if TYPE_CHECKING:
    from pathlib import Path


def make_env(tmp_path: Path) -> tuple[RecoveryRoom, Path, list[int]]:
    """搭一个战场（server/ + state/）和一个 owner 工作区；boots 记录门被按了几次。"""
    battle = tmp_path / "battle"
    (battle / "server").mkdir(parents=True)
    (battle / "server" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (battle / "server" / "__pycache__").mkdir()
    (battle / "server" / "__pycache__" / "junk.pyc").write_bytes(b"\x00")
    (battle / "server" / ".git").mkdir()
    (battle / "state").mkdir()
    (battle / "state" / "state.json").write_text('{"owner": 2}', encoding="utf-8")
    workspace = tmp_path / "workspaces" / "ai-2"
    boots: list[int] = []
    room = RecoveryRoom(battle, lambda: boots.append(1))
    return room, workspace, boots


def test_stage_copies_battle_without_git_or_cache(tmp_path: Path) -> None:
    """stage：代码与状态原样复制进 rescue/；git 与 __pycache__ 不进。"""
    room, workspace, _ = make_env(tmp_path)
    rescue = room.stage(workspace)
    assert (rescue / "server" / "main.py").read_text(encoding="utf-8") == "print('hi')\n"
    assert (rescue / "state" / "state.json").exists()
    assert not (rescue / "server" / ".git").exists()
    assert not (rescue / "server" / "__pycache__").exists()


def test_stage_mirrors_over_stale_rescue(tmp_path: Path) -> None:
    """镜像语义：stage 时工作区里遗留的旧 rescue/ 先清空再复制。"""
    room, workspace, _ = make_env(tmp_path)
    stale = workspace / "rescue" / "server"
    stale.mkdir(parents=True)
    (stale / "stale.py").write_text("old\n", encoding="utf-8")
    room.stage(workspace)
    assert not (workspace / "rescue" / "server" / "stale.py").exists()


def test_commit_mirrors_back_add_delete_edit(tmp_path: Path) -> None:
    """commit：增/删/改都如实搬回；目标里的杂散文件被清掉（先清空再复制）。"""
    room, workspace, _ = make_env(tmp_path)
    room.stage(workspace)
    rescue = workspace / "rescue"
    (rescue / "state" / "state.json").write_text('{"owner": 2, "fixed": true}', encoding="utf-8")
    (rescue / "server" / "main.py").unlink()
    (rescue / "server" / "new.py").write_text("x = 1\n", encoding="utf-8")
    extra = tmp_path / "battle" / "server" / "extra.py"
    extra.write_text("y = 2\n", encoding="utf-8")

    room.commit(workspace)

    battle = tmp_path / "battle"
    assert "fixed" in (battle / "state" / "state.json").read_text(encoding="utf-8")
    assert not (battle / "server" / "main.py").exists()
    assert (battle / "server" / "new.py").exists()
    assert not extra.exists()


def test_handle_ignores_raw_and_requests_boot(tmp_path: Path) -> None:
    """门只看「被按了一下」：老协议 JSON、随便写的字、空串，一律受理并登记重启。"""
    room, _workspace, boots = make_env(tmp_path)
    ok1, msg1 = room.handle(1, '{"op": "read", "path": "battle/server/main.py"}')
    ok2, _ = room.handle(1, "重启")
    ok3, _ = room.handle(1, "")
    assert ok1
    assert "受理" in msg1
    assert ok2
    assert ok3
    assert boots == [1, 1, 1]
