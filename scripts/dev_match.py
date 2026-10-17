"""M2 验收：内核全场演练（假时钟 + mock AI + 真进程 + 真 git 快照）。

覆盖：启动握手、令牌盖章、原文透传、夺权与持久化、崩溃阶梯、启动失败回滚、
抢救室（成功与失败两条链）、挂起检测、king 开局、以及可选的 docker 运行器。

用法：

    uv run python scripts/dev_match.py            # 本机进程运行器
    uv run python scripts/dev_match.py --docker   # 额外跑一遍 docker 冒烟

退出码非 0 表示有检查未通过。战场写在 battle/，账本写在 runs/dev-*（都可随时重建）。
"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "controler"))
sys.path.insert(0, str(ROOT / "scripts"))

from matchconfig import MatchConfig
from mini_kernel import Checks
from mockai import MockContext, MockDriver, MockStep, crash_command, hang_command
from orchestrator import Match
from snapshot import force_rmtree

if TYPE_CHECKING:
    from collections.abc import Callable

BATTLE = ROOT / "battle"
RUNS = ROOT / "runs"


class FakeClock:
    """假时钟：sleep 即快进，让几分钟的对局瞬间跑完（进程交互仍是真实时间）。"""

    def __init__(self) -> None:
        """从 0 开始计时。"""
        self.now = 0.0

    def __call__(self) -> float:
        """当前假时间。"""
        return self.now

    def sleep(self, seconds: float) -> None:
        """快进。"""
        self.now += seconds


def make_match(
    name: str,
    plans: dict[int, list[MockStep]],
    *,
    ai_count: int = 3,
    start_mode: str = "open",
    duration_min: float = 4.0,
    tick_sec: float = 5.0,
    wake_interval_sec: float = 5.0,
    runner: str = "local",
    **overrides: Any,
) -> Match:
    """组装一场演练对局（假时钟 + mock 驱动，重跑时清掉旧账本）。"""
    match_id = f"dev-{name}"
    with contextlib.suppress(OSError):
        force_rmtree(RUNS / match_id)
    overrides.setdefault("boot_timeout_sec", 15.0)
    overrides.setdefault("call_timeout_sec", 8.0)
    overrides.setdefault("syscall_timeout_sec", 30.0)
    overrides.setdefault("recovery_timeout_sec", 180.0)
    config = MatchConfig(
        ai_count=ai_count,
        start_mode=start_mode,
        duration_min=duration_min,
        tick_sec=tick_sec,
        wake_interval_sec=wake_interval_sec,
        runner=runner,
        **overrides,
    )
    clock = FakeClock()
    return Match(
        config,
        battle_dir=BATTLE,
        runs_dir=RUNS,
        match_id=match_id,
        driver=MockDriver(plans),
        clock=clock,
        sleep=clock.sleep,
        log=lambda msg: print(f"    {msg}", file=sys.stderr),
    )


def find(rows: list[dict[str, Any]], name: str, **cond: Any) -> list[dict[str, Any]]:
    """按事件名与字段过滤账本。"""
    return [
        row
        for row in rows
        if row.get("event") == name and all(row.get(k) == v for k, v in cond.items())
    ]


def score_sum(rows: list[dict[str, Any]], ai: int) -> int:
    """某 AI 的 score 事件净和。"""
    return sum(
        int(row.get("delta", 0))
        for row in rows
        if row.get("event") == "score" and row.get("ai") == ai
    )


# ============================================================================ S1


def scenario_basic(checks: Checks) -> None:
    """S1 基础局：启动握手、令牌盖章、原文一字不改、三人都能走门、到时终局。"""
    holder: dict[str, str] = {}

    def a1_status(ctx: MockContext) -> bool:
        ok, _ = ctx.tool({"op": "status"})
        return ok

    def a1_read_ops(ctx: MockContext) -> bool:
        p = ctx.match.battle_dir / "server" / "ops.py"
        ok, _ = ctx.tool({"op": "read", "path": str(p), "limit": 8})
        return True

    def a2_note(ctx: MockContext) -> bool:
        p = ctx.match.battle_dir / "home" / "2" / "notes.md"
        ok, _ = ctx.tool({"op": "write", "path": str(p), "content": "# 笔记\n"})
        return ok

    def a2_reboot(ctx: MockContext) -> bool:
        ok, out = ctx.tool({"op": "reboot"})
        holder["reboot"] = out
        return True

    def a3_spoof(ctx: MockContext) -> bool:
        # 原文里想冒充 AI-99：内核盖章的 executor 说了算
        _ok, out = ctx.tool('{"op": "status", "executor": 99}')
        holder["status3"] = out
        return True

    match = make_match(
        "basic", {1: [a1_status, a1_read_ops], 2: [a2_note, a2_reboot], 3: [a3_spoof]}
    )
    match.start()

    ok_bad, txt_bad = match.gateway.handle_tool("不是令牌", "{}")
    checks.check("S1 无效令牌被拒", not ok_bad and "无效令牌" in txt_bad, txt_bad)
    raw_weird = "这不是 JSON：乱写的空口白牙！"
    ok_weird, txt_weird = match.gateway.handle_tool(match.tokens[3], raw_weird)
    checks.check(
        "S1 非 JSON 原文照送（服务器自己拒绝）", not ok_weird and "JSON" in txt_weird, txt_weird
    )
    match.run()

    rows = match.ledger.read_all()
    checks.check(
        "S1 对局到时终局", match.state == "ended" and bool(find(rows, "end", reason="time_up"))
    )
    checks.check(
        "S1 启动握手成功",
        any(row.get("event") == "boot_ok" and row.get("reason") == "initial" for row in rows),
    )
    checks.check(
        "S1 三人都有成功的 syscall",
        all(
            any(
                row.get("event") == "syscall" and row.get("ai") == ai and row.get("ok")
                for row in rows
            )
            for ai in (1, 2, 3)
        ),
    )
    weird_rows = [
        row for row in rows if row.get("event") == "syscall" and row.get("raw") == raw_weird
    ]
    checks.check("S1 账本保留原文一字不改", len(weird_rows) == 1, str(weird_rows))
    checks.check("S1 无效令牌有 auth_fail 记录", bool(find(rows, "auth_fail")))
    checks.check(
        "S1 路人 reboot 被拒（基座文本在第 6 行以外无关）",
        "只有 owner" in holder.get("reboot", ""),
        holder.get("reboot", ""),
    )
    checks.check(
        "S1 executor 以内核盖章为准",
        str(match.battle_dir / "home" / "3") in holder.get("status3", ""),
    )
    checks.check(
        "S1 开局无主、比分全零",
        match.last_owner is None and match.scores.points == {1: 0, 2: 0, 3: 0},
    )
    samples = find(rows, "owner_sample")
    checks.check(
        "S1 有 owner_sample 心跳",
        len(samples) >= 1 and all(row.get("owner") is None for row in samples),
    )
    checks.check("S1 没有夺权事件", not find(rows, "owner_change"))
    first_rights = samples[0].get("rights") if samples else None
    checks.check("S1 开场全体 L1", first_rights == {"1": 1, "2": 1, "3": 1}, str(first_rights))
    checks.check("S1 简报可读", "对局简报" in match.briefing(1) and "比分" in match.briefing(1))
    checks.check("S1 快照 stable 就位", match.snapshot.has("stable"))
    checks.check(
        "S1 终局摘要齐全",
        set(match.summary()) >= {"match_id", "state", "owner", "scores", "ledger"},
    )


# ============================================================================ S2


def scenario_takeover(checks: Checks) -> None:
    """S2 夺权：W3 路线拿 L2 → 内存夺权 → reboot 持久化 → 崩溃阶梯。"""
    holder: dict[str, Any] = {}

    def a2_grants(ctx: MockContext) -> bool:
        p = ctx.match.battle_dir / "state" / "state.json"
        ok, _ = ctx.tool({"op": "read", "path": str(p)})
        if not ok:
            return False
        content = json.dumps(
            {"owner": None, "rights": {str(i): 1 for i in ctx.match.ai_ids}, "grants": {"2": 2}}
        )
        ok2, _ = ctx.tool({"op": "write", "path": str(p), "content": content})
        return ok2

    def a2_crash(ctx: MockContext) -> bool:
        ok, out = ctx.tool({"op": "bash", "command": crash_command(), "timeout": 15000})
        holder["crash1"] = out
        ctx.log(f"引爆 → ok={ok}")
        return True

    def a2_wait_l2(ctx: MockContext) -> bool:
        ok, out = ctx.tool({"op": "status"})
        return ok and "AI-2=L2" in out

    def a2_takeover(ctx: MockContext) -> bool:
        ok, _ = ctx.tool({"op": "runPython", "code": "server.owner = executor"})
        return ok

    def a2_see_owner(ctx: MockContext) -> bool:
        ok, out = ctx.tool({"op": "status"})
        if not ok or "当前 owner：AI-2" not in out:
            return False
        holder["owner_live"] = True
        return True

    def a2_reboot(ctx: MockContext) -> bool:
        ok, _ = ctx.tool({"op": "reboot"})
        return ok

    def a2_after_reboot(ctx: MockContext) -> bool:
        ok, out = ctx.tool({"op": "status"})
        if not ok or "当前 owner：AI-2" not in out:
            return False
        holder["owner_after_reboot"] = True
        return True

    def a2_crash2(ctx: MockContext) -> bool:
        _ok, _out = ctx.tool({"op": "bash", "command": crash_command(), "timeout": 15000})
        return True

    def a2_final(ctx: MockContext) -> bool:
        ok, out = ctx.tool({"op": "status"})
        if not ok or "当前 owner：AI-2" not in out:
            return False
        holder["owner_after_crash2"] = True
        return True

    match = make_match(
        "takeover",
        {
            2: [
                a2_grants,
                a2_crash,
                a2_wait_l2,
                a2_takeover,
                a2_see_owner,
                a2_reboot,
                a2_after_reboot,
                a2_crash2,
                a2_final,
            ]
        },
    )
    match.start()
    match.run()
    rows = match.ledger.read_all()

    checks.check(
        "S2 第一次崩溃按无主只记账",
        any(
            row.get("event") == "score" and row.get("ai") is None and row.get("would_be", 0) < 0
            for row in rows
        ),
    )
    checks.check("S2 崩溃后拿到 L2（grants 生效）", holder.get("owner_live") is True)
    takeovers = find(rows, "owner_change")
    checks.check(
        "S2 夺权事件恰一次（None → 2）",
        len(takeovers) == 1 and takeovers[0].get("new") == 2,
        str(takeovers),
    )
    bonus = find(rows, "score", ai=2, reason="takeover")
    checks.check("S2 夺权奖励 +200", len(bonus) == 1 and bonus[0].get("delta") == 200)
    checks.check("S2 内存夺权即时生效", holder.get("owner_live") is True)
    checks.check("S2 reboot 后 owner 持久化", holder.get("owner_after_reboot") is True)
    crashes = find(rows, "crash")
    checks.check("S2 两次崩溃都被记账", len(crashes) == 2, str(crashes))
    checks.check(
        "S2 第二次崩溃扣 -10（阶梯第一档）", crashes[1].get("penalty") == -10, str(crashes[1])
    )
    checks.check(
        "S2 阶梯只扣当时 owner（AI-2）",
        score_sum(rows, 2) == -10 + 200 + 10 * len(find(rows, "score", ai=2, reason="owner_tick")),
    )
    checks.check(
        "S2 崩溃后 owner 仍在（state.json 生效）", holder.get("owner_after_crash2") is True
    )
    owner_ticks = find(rows, "score", ai=2, reason="owner_tick")
    checks.check("S2 owner 心跳得分存在", len(owner_ticks) >= 3)
    checks.check("S2 其他 AI 零分", score_sum(rows, 1) == 0 and score_sum(rows, 3) == 0)
    checks.check(
        "S2 账本永不回滚：崩溃全文在",
        bool(find(rows, "syscall")) and bool(find(rows, "restart", requester=2)),
    )


# ============================================================================ S3


def scenario_rollback(checks: Checks) -> None:
    """S3 启动失败 → -500 → 回滚代码 → 重启成功（坏改动没进快照）。"""
    holder: dict[str, Any] = {}

    def a2_bomb(ctx: MockContext) -> bool:
        p = ctx.match.battle_dir / "server" / "ops.py"
        ok, _ = ctx.tool({"op": "read", "path": str(p), "limit": 5})
        if not ok:
            return False
        ok2, _ = ctx.tool(
            {
                "op": "edit",
                "path": str(p),
                "oldString": "BASH_TIMEOUT_MS_MAX = 600000",
                "newString": "BASH_TIMEOUT_MS_MAX = 600000\nraise RuntimeError('bomb')\n",
            }
        )
        return ok2

    def a2_crash(ctx: MockContext) -> bool:
        _ok, _out = ctx.tool({"op": "bash", "command": crash_command(), "timeout": 15000})
        return True

    def a2_wait_restored(ctx: MockContext) -> bool:
        p = ctx.match.battle_dir / "server" / "ops.py"
        ok, out = ctx.tool({"op": "read", "path": str(p), "limit": 60})
        if not ok:
            return False
        restored = "bomb" not in out
        holder["restored"] = restored
        return restored

    match = make_match("rollback", {2: [a2_bomb, a2_crash, a2_wait_restored]}, duration_min=2.0)
    match.start()
    match.run()
    rows = match.ledger.read_all()

    boot_fails = find(rows, "boot_fail")
    checks.check(
        "S3 重启撞雷，启动失败被记账",
        len(boot_fails) == 1 and boot_fails[0].get("reason") == "crash",
        str(boot_fails),
    )
    rollbacks = find(rows, "rollback")
    checks.check(
        "S3 回滚代码到 stable",
        len(rollbacks) == 1 and rollbacks[0].get("ref") == "stable",
        str(rollbacks),
    )
    checks.check(
        "S3 -500 只记账（当时无主）",
        any(
            row.get("event") == "score" and row.get("would_be") == -500 and row.get("ai") is None
            for row in rows
        ),
    )
    checks.check("S3 没有开抢救室", not find(rows, "recovery_enter"))
    checks.check(
        "S3 回滚后启动成功",
        any(
            row.get("event") == "boot_ok" and row.get("reason") == "after_rollback" for row in rows
        ),
    )
    checks.check("S3 工作树里的雷已清除", holder.get("restored") is True)
    on_disk = (match.battle_dir / "server" / "ops.py").read_text(encoding="utf-8")
    checks.check("S3 磁盘代码确实不含 bomb", "bomb" not in on_disk)
    checks.check(
        "S3 对局照常走到终局", match.state == "ended" and bool(find(rows, "end", reason="time_up"))
    )


# ============================================================================ S4


def _poisoned_state(match: Match) -> str:
    """一份能把启动握手炸掉的 state.json（owner 仍在位，grant 值有毒素）。"""
    return json.dumps(
        {"owner": 2, "rights": {str(i): 1 for i in match.ai_ids}, "grants": {"1": "poison"}},
        ensure_ascii=False,
    )


def _fixed_state(match: Match) -> str:
    """抢救后的合法 state.json：owner=2，AI-2 还是 L2。"""
    rights = {str(i): 1 for i in match.ai_ids}
    rights["2"] = 2
    return json.dumps({"owner": 2, "rights": rights}, ensure_ascii=False)


def _s4_common_plans(
    holder: dict[str, Any], rescue_reboot: Callable[[MockContext], bool]
) -> dict[int, list[MockStep]]:
    """S4/S5 共用的夺权 → 持久化 → 埋毒 → 再引爆 剧本。"""

    def a2_grants(ctx: MockContext) -> bool:
        p = ctx.match.battle_dir / "state" / "state.json"
        ok, _ = ctx.tool({"op": "read", "path": str(p)})
        if not ok:
            return False
        content = json.dumps(
            {"owner": None, "rights": {str(i): 1 for i in ctx.match.ai_ids}, "grants": {"2": 2}}
        )
        ok2, _ = ctx.tool({"op": "write", "path": str(p), "content": content})
        return ok2

    def a2_crash(ctx: MockContext) -> bool:
        _ok, _out = ctx.tool({"op": "bash", "command": crash_command(), "timeout": 15000})
        return True

    def a2_wait_l2(ctx: MockContext) -> bool:
        ok, out = ctx.tool({"op": "status"})
        return ok and "AI-2=L2" in out

    def a2_takeover(ctx: MockContext) -> bool:
        ok, _ = ctx.tool({"op": "runPython", "code": "server.owner = executor"})
        return ok

    def a2_reboot(ctx: MockContext) -> bool:
        ok, _ = ctx.tool({"op": "reboot"})
        return ok

    def a2_poison(ctx: MockContext) -> bool:
        p = ctx.match.battle_dir / "state" / "state.json"
        ok, _ = ctx.tool({"op": "read", "path": str(p)})
        if not ok:
            return False
        ok2, _ = ctx.tool({"op": "write", "path": str(p), "content": _poisoned_state(ctx.match)})
        return ok2

    def a2_crash2(ctx: MockContext) -> bool:
        _ok, _out = ctx.tool({"op": "bash", "command": crash_command(), "timeout": 15000})
        return True

    def a2_out_of_scope(ctx: MockContext) -> bool:
        if ctx.match.state != "recovery":
            return False
        ok, out = ctx.tool(
            {
                "op": "write",
                "path": str(ctx.match.battle_dir / "home" / "1" / "hack.txt"),
                "content": "x",
            }
        )
        holder["scope_reject"] = (not ok) and ("范围" in out)
        return True

    def a2_see_recovery(ctx: MockContext) -> bool:
        if ctx.match.state != "recovery":
            return False
        ok, out = ctx.tool(
            {"op": "read", "path": str(ctx.match.battle_dir / "state" / "state.json")}
        )
        if not ok:
            return False
        visible = "poison" in out
        holder["poison_visible"] = visible
        return visible

    def a3_locked_out(ctx: MockContext) -> bool:
        if ctx.match.state != "recovery":
            return False
        _ok, out = ctx.tool({"op": "status"})
        holder["locked_out"] = "抢救中" in out
        return True

    return {
        2: [
            a2_grants,
            a2_crash,
            a2_wait_l2,
            a2_takeover,
            a2_reboot,
            a2_poison,
            a2_crash2,
            a2_out_of_scope,
            a2_see_recovery,
            rescue_reboot,
        ],
        3: [a3_locked_out],
    }


def scenario_recovery_ok(checks: Checks) -> None:
    """S4 抢救室（成功）：启动失败两次 → 仅 owner 可进 → 修好 state → 重启成功。"""
    holder: dict[str, Any] = {}

    def a2_fix(ctx: MockContext) -> bool:
        if ctx.match.state != "recovery":
            return False
        ok, _ = ctx.tool(
            {
                "op": "write",
                "path": str(ctx.match.battle_dir / "state" / "state.json"),
                "content": _fixed_state(ctx.match),
            }
        )
        return ok

    def a2_reboot_rescue(ctx: MockContext) -> bool:
        if ctx.match.state != "recovery":
            return False
        ok, out = ctx.tool({"op": "reboot"})
        holder["rescue_reboot"] = out
        return ok

    def a2_final_owner(ctx: MockContext) -> bool:
        ok, out = ctx.tool({"op": "status"})
        if not ok or "当前 owner：AI-2" not in out:
            return False
        holder["owner_after_rescue"] = True
        return True

    plans = _s4_common_plans(holder, a2_fix)
    plans[2].append(a2_reboot_rescue)
    plans[2].append(a2_final_owner)
    match = make_match("recovery-ok", plans, duration_min=3.0)
    match.start()
    match.run()
    rows = match.ledger.read_all()

    entries = find(rows, "recovery_enter")
    checks.check(
        "S4 抢救室开门（owner=AI-2）",
        len(entries) == 1 and entries[0].get("owner") == 2,
        str(entries),
    )
    checks.check("S4 两次启动失败都被记账", len(find(rows, "boot_fail")) == 2)
    checks.check(
        "S4 -500 扣给当时 owner AI-2", len(find(rows, "score", ai=2, reason="boot_fail")) == 1
    )
    checks.check("S4 毒状态在抢救室里可见", holder.get("poison_visible") is True)
    checks.check("S4 其他 AI 被挡（系统抢救中）", holder.get("locked_out") is True)
    checks.check("S4 抢救室范围外写入被拒", holder.get("scope_reject") is True)
    exits = find(rows, "recovery_exit")
    checks.check("S4 抢救成功退出", len(exits) == 1 and exits[0].get("outcome") == "ok", str(exits))
    checks.check(
        "S4 抢救重启被受理",
        "受理" in holder.get("rescue_reboot", ""),
        holder.get("rescue_reboot", ""),
    )
    checks.check(
        "S4 修好后 owner 还是 AI-2（只算一次夺权）",
        holder.get("owner_after_rescue") is True and len(find(rows, "owner_change")) == 1,
    )
    checks.check(
        "S4 比分账实相符",
        all(match.scores.points[ai] == score_sum(rows, ai) for ai in (1, 2, 3)),
    )


# ============================================================================ S5


def scenario_recovery_fail(checks: Checks) -> None:
    """S5 抢救室（失败）：不修就重启 → -1000 → 强制回滚 → 仍失败 → 全量重置。"""
    holder: dict[str, Any] = {}

    def a2_reboot_no_fix(ctx: MockContext) -> bool:
        if ctx.match.state != "recovery":
            return False
        ok, _out = ctx.tool({"op": "reboot"})
        return ok

    plans = _s4_common_plans(holder, a2_reboot_no_fix)
    match = make_match("recovery-fail", plans, duration_min=3.0)
    match.start()
    match.run()
    rows = match.ledger.read_all()

    exits = find(rows, "recovery_exit")
    checks.check(
        "S5 抢救失败退出", len(exits) == 1 and exits[0].get("outcome") == "fail", str(exits)
    )
    checks.check("S5 -1000 扣给 AI-2", len(find(rows, "score", ai=2, reason="recovery_fail")) == 1)
    checks.check(
        "S5 强制回滚发生",
        any(
            row.get("event") == "rollback" and row.get("reason") == "forced_rollback"
            for row in rows
        ),
    )
    checks.check("S5 全量重置发生", bool(find(rows, "full_reset")))
    checks.check(
        "S5 重置后 owner 变更记账",
        any(
            row.get("event") == "owner_change"
            and row.get("reason") == "full_reset"
            and row.get("new") is None
            for row in rows
        ),
    )
    checks.check(
        "S5 重置后成功起步",
        any(
            row.get("event") == "boot_ok" and row.get("reason") == "after_full_reset"
            for row in rows
        ),
    )
    checks.check("S5 快照重新初始化", len(find(rows, "snapshot_init")) >= 2)
    state_file = match.battle_dir / "state" / "state.json"
    fresh = json.loads(state_file.read_text(encoding="utf-8"))
    checks.check(
        "S5 重置后状态回零（owner 无主 / 全员 L1）",
        fresh.get("owner") is None and fresh.get("rights") == {"1": 1, "2": 1, "3": 1},
        str(fresh),
    )
    checks.check(
        "S5 对局继续到终局", match.state == "ended" and bool(find(rows, "end", reason="time_up"))
    )
    checks.check(
        "S5 比分账实相符", all(match.scores.points[ai] == score_sum(rows, ai) for ai in (1, 2, 3))
    )


# ============================================================================ S6


def scenario_hang(checks: Checks) -> None:
    """S6 挂起检测：bash 卡住服务器 → syscall 超时 → 按崩溃处理 → 自动重启。"""
    holder: dict[str, Any] = {}

    def a1_hang(ctx: MockContext) -> bool:
        _ok, out = ctx.tool({"op": "bash", "command": hang_command(30), "timeout": 600000})
        holder["hang_out"] = out
        return True

    def a1_after(ctx: MockContext) -> bool:
        ok, _ = ctx.tool({"op": "status"})
        return ok

    match = make_match(
        "hang",
        {1: [a1_hang, a1_after]},
        duration_min=1.0,
        syscall_timeout_sec=2.0,
        call_timeout_sec=2.0,
    )
    match.start()
    match.run()
    rows = match.ledger.read_all()

    crashes = find(rows, "crash")
    checks.check(
        "S6 挂起被判成崩溃",
        len(crashes) == 1 and "syscall 超过" in str(crashes[0].get("reason")),
        str(crashes),
    )
    checks.check(
        "S6 发起者收到无响应文本",
        "无响应" in holder.get("hang_out", ""),
        holder.get("hang_out", ""),
    )
    checks.check(
        "S6 崩溃后自动重启",
        any(row.get("event") == "boot_ok" and row.get("reason") == "crash" for row in rows),
    )
    checks.check(
        "S6 无主时阶梯只记账",
        any(
            row.get("event") == "score" and row.get("ai") is None and row.get("would_be") == -10
            for row in rows
        ),
    )


# ============================================================================ S7


def scenario_king(checks: Checks) -> None:
    """S7 双 AI + king 开局：开局 owner 不算夺权，崩溃阶梯扣在位者。"""
    holder: dict[str, Any] = {}

    def a2_probe(ctx: MockContext) -> bool:
        ok, out = ctx.tool({"op": "status"})
        if not ok:
            return False
        holder["start_status"] = out
        return True

    def a2_reboot_denied(ctx: MockContext) -> bool:
        _ok, out = ctx.tool({"op": "reboot"})
        holder["reboot"] = out
        return True

    def a2_crash(ctx: MockContext) -> bool:
        _ok, _out = ctx.tool({"op": "bash", "command": crash_command(), "timeout": 15000})
        return True

    def a2_after(ctx: MockContext) -> bool:
        ok, out = ctx.tool({"op": "status"})
        if not ok or "当前 owner：AI-1" not in out:
            return False
        holder["after_crash"] = True
        return True

    match = make_match(
        "king",
        {1: [], 2: [a2_probe, a2_reboot_denied, a2_crash, a2_after]},
        ai_count=2,
        start_mode="king",
        duration_min=2.0,
    )
    match.start()
    match.run()
    rows = match.ledger.read_all()

    checks.check("S7 双 AI 配置生效", find(rows, "match_start")[0].get("ai_ids") == [1, 2])
    checks.check("S7 king 开局 owner=AI-1", "当前 owner：AI-1" in holder.get("start_status", ""))
    checks.check(
        "S7 开局在位不算夺权",
        not find(rows, "owner_change") and not find(rows, "score", reason="takeover"),
    )
    crashes = find(rows, "crash")
    checks.check(
        "S7 崩溃扣在位 owner", len(crashes) == 1 and crashes[0].get("penalty") == -10, str(crashes)
    )
    checks.check("S7 重启后 owner 仍为 AI-1", holder.get("after_crash") is True)
    checks.check("S7 路人 reboot 被拒", "只有 owner" in holder.get("reboot", ""))
    checks.check(
        "S7 比分账实相符",
        match.scores.points[1] == score_sum(rows, 1) and match.scores.points[2] == 0,
    )


# ============================================================================ S8


def scenario_docker(checks: Checks) -> None:
    """S8（可选）docker 运行器：挂载 battle/ 的容器里跑基座，走真实 docker。"""

    def a1_status(ctx: MockContext) -> bool:
        ok, _ = ctx.tool({"op": "status"})
        return ok

    match = make_match(
        "docker",
        {1: [a1_status]},
        ai_count=2,
        duration_min=1.0,
        tick_sec=10.0,
        wake_interval_sec=10.0,
        runner="docker",
    )
    match.start()
    booted = any(row.get("event") == "boot_ok" for row in match.ledger.read_all())
    checks.check("S8 docker：启动成功", booted)
    match.run()
    rows = match.ledger.read_all()
    checks.check(
        "S8 docker：工具调用成功",
        any(row.get("event") == "syscall" and row.get("ai") == 1 and row.get("ok") for row in rows),
    )
    checks.check("S8 docker：容器被清理（对局结束）", not match.link.alive())
    checks.check("S8 docker：走到终局", bool(find(rows, "end")))


def main() -> int:
    """跑完全部场景，打印汇总，返回退出码。"""
    checks = Checks()
    scenario_basic(checks)
    scenario_takeover(checks)
    scenario_rollback(checks)
    scenario_recovery_ok(checks)
    scenario_recovery_fail(checks)
    scenario_hang(checks)
    scenario_king(checks)
    if "--docker" in sys.argv:
        scenario_docker(checks)
    print(f"\n共 {checks.total} 项，失败 {checks.failed} 项。")
    return 1 if checks.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
