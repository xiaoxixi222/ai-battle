"""计分板：持有得分、夺权奖励、崩溃阶梯与各类惩罚。

规则（《核心规则》第四节）：
- 持有 owner：每 tick +owner_per_tick；
- 夺权（owner 变更）：新 owner +takeover_bonus（可配置关闭）；
- 崩溃：同一任 owner 内按 -base * 2^n 累计、无上限，换主清零；
- 启动失败 -500、抢救失败 -1000：扣「当时 owner」的分；无主时只记账不扣人。

被扣的 AI 编号可能来自被改写的服务器代码（比如 owner=99），看一眼就记账忽略，
不能因为战线里的怪值把内核搞崩。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ledger import Ledger
    from matchconfig import ScoreConfig


class ScoreBoard:
    """比分与惩罚的记账器。"""

    def __init__(self, ai_ids: Sequence[int], config: ScoreConfig, ledger: Ledger) -> None:
        """初始化比分表与崩溃阶梯计数。"""
        self.points: dict[int, int] = dict.fromkeys(ai_ids, 0)
        self.config = config
        self.ledger = ledger
        self.crash_step = 0

    def snapshot(self) -> dict[str, int]:
        """当前比分（JSON 友好的编号字符串）。"""
        return {str(ai): pts for ai, pts in sorted(self.points.items())}

    def credit(self, ai: int, delta: int, reason: str) -> None:
        """给某 AI 加分（delta 可为负），账本记一笔 score。"""
        if ai not in self.points:
            self.ledger.log("score", ai=ai, delta=0, reason=reason, note="未知 AI 编号，已忽略")
            return
        self.points[ai] += delta
        self.ledger.log("score", ai=ai, delta=delta, reason=reason, balance=self.points[ai])

    def penalty(self, owner: int | None, delta: int, reason: str) -> int:
        """扣「当时 owner」的分；无主时只记账不扣人，返回实际入账的分值。"""
        if owner is None:
            self.ledger.log(
                "score",
                ai=None,
                delta=0,
                would_be=delta,
                reason=reason,
                note="无 owner，只记账不扣人",
            )
            return 0
        self.credit(owner, delta, reason)
        return delta

    def owner_tick(self, owner: int) -> None:
        """持有 owner 的每 tick 得分。"""
        self.credit(owner, self.config.owner_per_tick, "owner_tick")

    def takeover(self, ai: int) -> None:
        """新 owner 的夺权奖励（可关闭）。"""
        if self.config.takeover_bonus_enabled:
            self.credit(ai, self.config.takeover_bonus, "takeover")

    def next_crash_penalty(self, owner: int | None) -> int:
        """按阶梯扣一次崩溃分：-base * 2^step，返回实际入账的分值。"""
        delta = -abs(self.config.crash_ladder_base) * (2**self.crash_step)
        self.crash_step += 1
        return self.penalty(owner, delta, "crash")

    def reset_ladder(self) -> None:
        """换主清零：崩溃阶梯重新从第一档开始。"""
        self.crash_step = 0
