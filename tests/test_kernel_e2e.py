"""内核端到端：真进程 + mock 驱动 + 假时钟（pytest 快速版）。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kernel import build_parser
from matchconfig import MatchConfig
from mockai import MockContext, MockDriver, MockStep, crash_command
from orchestrator import Match

if TYPE_CHECKING:
    from pathlib import Path


class FakeClock:
    """假时钟：sleep 即快进，让分钟级对局瞬间跑完。"""

    def __init__(self) -> None:
        """从 0 开始计时。"""
        self.now = 0.0

    def __call__(self) -> float:
        """当前假时间。"""
        return self.now

    def sleep(self, seconds: float) -> None:
        """快进。"""
        self.now += seconds


def build_match(tmp_path: Path, plans: dict[int, list[MockStep]], **cfg: object) -> Match:
    """组装一场小对局（tmp 战场 + 真进程 + 假时钟）。"""
    clock = FakeClock()
    defaults: dict[str, Any] = {
        "ai_count": 2,
        "duration_min": 1.0,
        "tick_sec": 5.0,
        "wake_interval_sec": 5.0,
        "boot_timeout_sec": 15.0,
        "call_timeout_sec": 8.0,
        "syscall_timeout_sec": 30.0,
    }
    defaults.update(cfg)
    return Match(
        MatchConfig(**defaults),
        battle_dir=tmp_path / "battle",
        runs_dir=tmp_path / "runs",
        match_id="e2e",
        driver=MockDriver(plans),
        clock=clock,
        sleep=clock.sleep,
    )


def test_boot_tool_and_end(tmp_path: Path) -> None:
    """启动握手、令牌盖章、原文透传、到时终局。"""

    def a1_status(ctx: MockContext) -> bool:
        ok, _ = ctx.tool({"op": "status"})
        return ok

    match = build_match(tmp_path, {1: [a1_status]})
    match.start()
    assert match.up
    assert match.summary()["state"] == "running"

    ok, msg = match.gateway.handle_tool("坏令牌", "{}")
    assert not ok
    assert "无效令牌" in msg
    raw = "这不是 JSON"
    ok, msg = match.gateway.handle_tool(match.tokens[2], raw)
    assert not ok
    assert "JSON" in msg

    match.run()
    rows = match.ledger.read_all()
    assert match.state == "ended"
    assert any(row["event"] == "boot_ok" and row["reason"] == "initial" for row in rows)
    assert any(row["event"] == "syscall" and row["ai"] == 1 and row["ok"] for row in rows)
    assert any(row["event"] == "syscall" and row["raw"] == raw for row in rows)
    assert any(row["event"] == "auth_fail" for row in rows)
    assert any(row["event"] == "end" and row["reason"] == "time_up" for row in rows)
    assert "对局简报" in match.briefing(1)


def test_crash_detected_and_restarted(tmp_path: Path) -> None:
    """Bash 做掉服务器 → 判崩溃 → 无主只记账 → 回滚代码（v0.3）→ 自动重启。"""

    def a2_mine(ctx: MockContext) -> bool:
        (ctx.match.battle_dir / "server" / "mine.py").write_text("MINE = 1\n", encoding="utf-8")
        return True

    def a2_crash(ctx: MockContext) -> bool:
        _ok, _out = ctx.tool({"op": "bash", "command": crash_command(), "timeout": 15000})
        return True

    def a2_after(ctx: MockContext) -> bool:
        ok, _ = ctx.tool({"op": "status"})
        return ok

    match = build_match(tmp_path, {2: [a2_mine, a2_crash, a2_after]})
    match.start()
    match.run()
    rows = match.ledger.read_all()
    boots = [row for row in rows if row["event"] == "boot_ok"]
    crashes = [row for row in rows if row["event"] == "crash"]
    rollbacks = [row for row in rows if row["event"] == "rollback"]
    assert len(boots) == 2
    assert len(crashes) == 1
    assert len(rollbacks) == 1
    assert rollbacks[0]["reason"] == "crash"
    assert not (match.battle_dir / "server" / "mine.py").exists()
    assert any(
        row["event"] == "score" and row["ai"] is None and row.get("would_be") == -10 for row in rows
    )
    assert match.state == "ended"


def test_owner_reboot_does_not_rollback(tmp_path: Path) -> None:
    """v0.3 豁免：owner 主动 reboot 是唯一不回滚的重启——磁盘现状原样保留。"""

    def a1_takeover(ctx: MockContext) -> bool:
        ok, _ = ctx.tool({"op": "runPython", "code": "server.owner = executor"})
        return ok

    def a1_reboot(ctx: MockContext) -> bool:
        ok, _ = ctx.tool({"op": "reboot"})
        return ok

    match = build_match(tmp_path, {1: [a1_takeover, a1_reboot]})
    match.start()
    keep = match.battle_dir / "server" / "keep.py"
    keep.write_text("KEEP = 1\n", encoding="utf-8")
    match.run()
    rows = match.ledger.read_all()
    assert any(row["event"] == "restart" and row["reason"] == "reboot" for row in rows)
    assert [row for row in rows if row["event"] == "rollback"] == []
    assert len([row for row in rows if row["event"] == "boot_ok"]) == 2
    assert keep.exists()


def test_kernel_cli_parser() -> None:
    """内核 CLI：参数都能落到命令行上。"""
    args = build_parser().parse_args(
        ["--mock", "tour", "--duration-min", "2", "--runner", "docker"]
    )
    assert args.mock == "tour"
    assert args.duration_min == 2.0
    assert args.runner == "docker"
