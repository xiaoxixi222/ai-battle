"""对局账本：追加、相对时钟与读取（内核单元测试）。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ledger import Ledger

if TYPE_CHECKING:
    from pathlib import Path


def test_append_and_read(tmp_path: Path) -> None:
    """按行追加、rel 用注入时钟计、可按事件名过滤。"""
    now = [100.0]
    led = Ledger(tmp_path / "nested" / "ledger.jsonl", clock=lambda: now[0])
    led.log("boot_ok", reason="initial")
    now[0] = 112.5
    led.log("end", reason="time_up")
    rows = led.read_all()
    assert [row["event"] for row in rows] == ["boot_ok", "end"]
    assert rows[1]["rel"] == 12.5
    assert led.events("end") == [rows[1]]


def test_read_all_missing_file(tmp_path: Path) -> None:
    """没写过就还没有——返回空列表。"""
    led = Ledger(tmp_path / "l.jsonl")
    assert led.read_all() == []
