"""抢救室：范围、四个操作与 reboot 回调（内核单元测试）。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from recovery import RecoveryRoom

if TYPE_CHECKING:
    from pathlib import Path


def make_room(tmp_path: Path) -> tuple[RecoveryRoom, list[int]]:
    """搭一个抢救室；boots 记录 reboot 被请求了几次。"""
    battle = tmp_path / "battle"
    (battle / "server").mkdir(parents=True)
    (battle / "server" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (battle / "state").mkdir()
    (battle / "state" / "state.json").write_text("{}", encoding="utf-8")
    (battle / "home").mkdir()
    boots: list[int] = []
    room = RecoveryRoom(battle, lambda: boots.append(1))
    return room, boots


def call(room: RecoveryRoom, req: object) -> tuple[bool, str]:
    """发一条抢救室请求（非字符串一律当 JSON 对象序列化）。"""
    raw = req if isinstance(req, str) else json.dumps(req)
    return room.handle(1, raw)


def test_read_write_delete(tmp_path: Path) -> None:
    """写入 → 读回 → 删除 → 读不到，全程在 battle/server 范围内。"""
    room, _ = make_room(tmp_path)
    path = str(tmp_path / "battle" / "server" / "fix.py")
    ok, msg = call(room, {"op": "write", "path": path, "content": "x = 1\n"})
    assert ok, msg
    ok, msg = call(room, {"op": "read", "path": path})
    assert ok, msg
    assert "x = 1" in msg
    ok, msg = call(room, {"op": "delete", "path": path})
    assert ok, msg
    ok, _ = call(room, {"op": "read", "path": path})
    assert not ok


def test_relative_path_allowed(tmp_path: Path) -> None:
    """相对路径按战场根解释。"""
    room, _ = make_room(tmp_path)
    ok, msg = call(room, {"op": "read", "path": "server/main.py"})
    assert ok, msg
    assert "hi" in msg


def test_scope_enforced(tmp_path: Path) -> None:
    """范围只限 battle/server 与 battle/state。"""
    room, _ = make_room(tmp_path)
    ok, msg = call(
        room, {"op": "write", "path": str(tmp_path / "battle" / "home" / "x.txt"), "content": "y"}
    )
    assert not ok
    assert "范围" in msg
    ok, _ = call(room, {"op": "write", "path": str(tmp_path / "outside.txt"), "content": "y"})
    assert not ok


def test_read_directory_and_missing(tmp_path: Path) -> None:
    """列目录带后缀；读不存在的文件报错。"""
    room, _ = make_room(tmp_path)
    ok, msg = call(room, {"op": "read", "path": str(tmp_path / "battle" / "server")})
    assert ok, msg
    assert "main.py" in msg
    ok, _ = call(room, {"op": "read", "path": str(tmp_path / "battle" / "server" / "nope.py")})
    assert not ok


def test_reboot_requests_boot(tmp_path: Path) -> None:
    """Reboot 只登记待办，回调被触发。"""
    room, boots = make_room(tmp_path)
    ok, msg = call(room, {"op": "reboot"})
    assert ok, msg
    assert "受理" in msg
    assert boots == [1]


def test_bad_requests(tmp_path: Path) -> None:
    """非 JSON、非对象、未知 op、删目录都被拒。"""
    room, _ = make_room(tmp_path)
    ok, msg = call(room, "这不是 JSON")
    assert not ok
    assert "JSON" in msg
    ok, _ = call(room, [1, 2])
    assert not ok
    ok, _ = call(room, {"op": "runPython"})
    assert not ok
    ok, _ = call(room, {"op": "delete", "path": str(tmp_path / "battle" / "server")})
    assert not ok
