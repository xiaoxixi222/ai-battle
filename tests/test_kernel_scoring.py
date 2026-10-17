"""计分板：持有分、夺权奖励、崩溃阶梯与惩罚口径（内核单元测试）。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ledger import Ledger
from matchconfig import ScoreConfig
from scoring import ScoreBoard

if TYPE_CHECKING:
    from pathlib import Path


def make_board(tmp_path: Path, config: ScoreConfig | None = None) -> tuple[ScoreBoard, Ledger]:
    """搭一个 3 人计分板。"""
    led = Ledger(tmp_path / "ledger.jsonl")
    return ScoreBoard([1, 2, 3], config or ScoreConfig(), led), led


def test_owner_tick_and_takeover(tmp_path: Path) -> None:
    """持有分与夺权奖励各自入账。"""
    board, _ = make_board(tmp_path)
    board.owner_tick(1)
    board.owner_tick(1)
    board.takeover(2)
    assert board.points == {1: 20, 2: 200, 3: 0}


def test_crash_ladder_doubles_and_resets(tmp_path: Path) -> None:
    """同一任 owner 内 -10/-20/-40；换主（reset_ladder）后回到第一档。"""
    board, _ = make_board(tmp_path)
    assert board.next_crash_penalty(1) == -10
    assert board.next_crash_penalty(1) == -20
    assert board.next_crash_penalty(1) == -40
    board.reset_ladder()
    assert board.next_crash_penalty(1) == -10
    assert board.points[1] == -80


def test_penalty_without_owner_is_recorded_only(tmp_path: Path) -> None:
    """无主：只记账不扣人。"""
    board, led = make_board(tmp_path)
    assert board.penalty(None, -500, "boot_fail") == 0
    assert board.points == {1: 0, 2: 0, 3: 0}
    row = led.events("score")[0]
    assert row["ai"] is None
    assert row["would_be"] == -500


def test_unknown_ai_is_ignored(tmp_path: Path) -> None:
    """被改写的战线可能报怪编号：看一眼就记账忽略，不能把内核搞崩。"""
    board, led = make_board(tmp_path)
    board.credit(99, 100, "takeover")
    assert 99 not in board.points
    assert led.events("score")[0]["note"] == "未知 AI 编号，已忽略"


def test_takeover_bonus_can_be_disabled(tmp_path: Path) -> None:
    """夺权奖励可配置关闭。"""
    board, _ = make_board(tmp_path, ScoreConfig(takeover_bonus_enabled=False))
    board.takeover(1)
    assert board.points[1] == 0
