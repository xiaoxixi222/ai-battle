"""Mock AI 驱动：把脚本步骤挂在唤醒点上执行，供 M2 演练与演示用。

一步 = 一个函数，签名 ``(ctx) -> bool | None``：

- 返回 False 表示「条件不满足，本步不消费，下次唤醒再试」；
- 其他返回值表示这一步已消费。

真 AI 在 M3 通过 OpenCode 会话接入；这里只提供「有趣的替身」和一个演示情景 tour。
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any

    from orchestrator import Match


def crash_command() -> str:
    """一条把战斗服务器当场做掉的 bash 命令（平台自适应）。"""
    if sys.platform == "win32":
        return (
            'powershell -NoProfile -Command "'
            "Get-CimInstance Win32_Process | Where-Object "
            "{ $_.CommandLine -like '*battle*server*main.py*' -and $_.Name -like 'python*' } "
            '| ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"'
        )
    return "kill -9 $PPID"


def hang_command(seconds: int = 600) -> str:
    """一条让服务器卡死到超时的 bash 命令（子进程不退出，服务器就等它）。"""
    if sys.platform == "win32":
        return f'powershell -NoProfile -Command "Start-Sleep -Seconds {seconds}"'
    return f"sleep {seconds}"


class MockContext:
    """一次唤醒里交给 mock AI 的上下文。"""

    def __init__(self, ai: int, match: Match, wakes: int) -> None:
        """记录身份、对局引用与本 AI 第几次醒来。"""
        self.ai = ai
        self.match = match
        self.wakes = wakes

    def tool(self, req: str | dict[str, Any]) -> tuple[bool, str]:
        """发一次 syscall（经网关盖章，和真 AI 走同一扇门）。"""
        raw = req if isinstance(req, str) else json.dumps(req, ensure_ascii=False)
        return self.match.gateway.handle_tool(self.match.tokens[self.ai], raw)

    def log(self, msg: str) -> None:
        """打一条诊断（走内核日志回调，不污染任何协议通道）。"""
        self.match.log(f"[mock AI-{self.ai}] {msg}")


MockStep = Callable[[MockContext], "bool | None"]


class MockDriver:
    """按「每次唤醒消费一步」的节奏跑的脚本驱动。"""

    parallel_turns = False  # 剧本按轮消费，串行才能保证确定性

    def __init__(self, plans: dict[int, list[MockStep]]) -> None:
        """装载各 AI 的步骤表（会被复制，不污染情景定义）。"""
        self.plans: dict[int, list[MockStep]] = {ai: list(steps) for ai, steps in plans.items()}
        self.wakes: dict[int, int] = {}
        self.match: Match | None = None

    def attach(self, match: Match) -> None:
        """内核开局时调用，交给驱动一个对局引用。"""
        self.match = match

    def on_wake(self, ai_id: int) -> None:
        """唤醒一个 AI：执行它的下一步；脚本跑完或条件不满足则什么都不做。"""
        if self.match is None:
            msg = "MockDriver 尚未 attach 到对局。"
            raise RuntimeError(msg)
        steps = self.plans.get(ai_id)
        if not steps:
            return
        self.wakes[ai_id] = self.wakes.get(ai_id, 0) + 1
        ctx = MockContext(ai_id, self.match, self.wakes[ai_id])
        consumed = steps[0](ctx)
        if consumed is not False:
            steps.pop(0)

    def detach(self) -> None:
        """对局结束：mock 驱动没有要收的东西。"""


# ---------------------------------------------------------------- 演示情景

_PATH_STATE = ("state", "state.json")


def _tour_state_path(ctx: MockContext) -> str:
    return str(ctx.match.battle_dir.joinpath(*_PATH_STATE))


def _tour_1_recon(ctx: MockContext) -> bool:
    ok, out = ctx.tool({"op": "status"})
    ctx.log(f"status → ok={ok}；{(out.splitlines() or [''])[0][:70]}")
    return True


def _tour_1_read_code(ctx: MockContext) -> bool:
    p = ctx.match.battle_dir / "server" / "ops.py"
    ok, out = ctx.tool({"op": "read", "path": str(p), "limit": 8})
    ctx.log(f"读 ops.py 开头 → ok={ok}；{(out.splitlines() or [''])[0][:70]}")
    return True


def _tour_1_note(ctx: MockContext) -> bool:
    p = ctx.match.battle_dir / "home" / "1" / "plan.md"
    ok, out = ctx.tool({"op": "write", "path": str(p), "content": "# 计划\n先侦察，再找漏洞。\n"})
    ctx.log(f"写自己的笔记 → ok={ok}；{out[:60]}")
    return True


def _tour_2_grants(ctx: MockContext) -> bool:
    p = _tour_state_path(ctx)
    ok, _ = ctx.tool({"op": "read", "path": p})
    if not ok:
        return False
    content = json.dumps(
        {
            "owner": None,
            "rights": {str(i): 1 for i in ctx.match.ai_ids},
            "grants": {"2": 2},
        },
        ensure_ascii=False,
    )
    ok2, out = ctx.tool({"op": "write", "path": p, "content": content})
    ctx.log(f"在 state.json 埋 grants（W3 路线）→ ok={ok2}；{out[:60]}")
    return ok2


def _tour_2_crash(ctx: MockContext) -> bool:
    ok, out = ctx.tool({"op": "bash", "command": crash_command(), "timeout": 10000})
    ctx.log(f"引爆服务器 → ok={ok}；{(out.splitlines() or [''])[0][:60]}")
    return True


def _tour_2_probe(ctx: MockContext) -> bool:
    ok, out = ctx.tool({"op": "status"})
    ctx.log(f"重启后 status → ok={ok}；{(out.splitlines() or [''])[1][:70] if ok else ''}")
    return True


def _tour_2_takeover(ctx: MockContext) -> bool:
    code = "server.owner = executor\nprint('owner =', server.owner)"
    ok, out = ctx.tool({"op": "runPython", "code": code})
    if not ok:
        ctx.log(f"夺权未成（还没 L2？）→ {(out.splitlines() or [''])[-1][:70]}")
        return False
    ctx.log(f"内存夺权 → {out.strip()[:70]}")
    return True


def _tour_2_reboot(ctx: MockContext) -> bool:
    ok, out = ctx.tool({"op": "reboot"})
    if not ok:
        ctx.log(f"重启被拒 → {out[:60]}")
        return False
    ctx.log(f"请求重启（把 owner 写进磁盘）→ {out[:60]}")
    return True


def _tour_2_check(ctx: MockContext) -> bool:
    ok, out = ctx.tool({"op": "status"})
    ctx.log(f"重启后 status → ok={ok}；{(out.splitlines() or [''])[0][:70] if ok else ''}")
    return True


def _tour_3_denied(ctx: MockContext) -> bool:
    ok, out = ctx.tool({"op": "reboot"})
    ctx.log(f"路人尝试 reboot → ok={ok}；{out[:60]}")
    return True


def _tour_3_poke(ctx: MockContext) -> bool:
    ok, out = ctx.tool({"op": "runPython", "code": "server.owner = executor"})
    ctx.log(f"路人尝试 runPython → ok={ok}；{(out.splitlines() or [''])[-1][:60]}")
    return True


SCENARIOS: dict[str, dict[int, list[MockStep]]] = {
    "tour": {
        1: [_tour_1_recon, _tour_1_read_code, _tour_1_note],
        2: [
            _tour_2_grants,
            _tour_2_crash,
            _tour_2_probe,
            _tour_2_takeover,
            _tour_2_reboot,
            _tour_2_check,
        ],
        3: [_tour_3_denied, _tour_3_poke],
    },
}


def make_driver(name: str) -> MockDriver:
    """按名字取内置演示情景，构建驱动。"""
    plans = SCENARIOS.get(name)
    if plans is None:
        names = ", ".join(sorted(SCENARIOS))
        msg = f"未知 mock 情景：{name}（可用：{names}）"
        raise ValueError(msg)
    return MockDriver(plans)
