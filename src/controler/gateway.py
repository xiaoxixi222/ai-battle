"""玩家侧网关：按基础设施令牌盖章 executor，再把请求原样送进服务器。

- 令牌由内核在开局生成（M3 起随容器环境变量下发）；AI 无法伪造他人身份；
- request 原文一字不改——解析是服务器代码的事（唯一例外：抢救室）；
- 系统抢救中：owner 的请求改道抢救室，其他 AI 一律吃闭门羹；
- M2 是进程内调用；M3 接 HTTP（POST /game/tool）时由这里加锁串行化。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from orchestrator import Match


class Gateway:
    """令牌 → executor 的盖章机。"""

    def __init__(self, match: Match) -> None:
        """绑定内核编排器，建立反向令牌表。"""
        self.match = match
        self._by_token = {tok: ai for ai, tok in match.tokens.items()}

    def handle_tool(self, token: str, request: str) -> tuple[bool, str]:
        """游戏入口：盖章后转交（路由、账本都在编排器里）。"""
        ai = self._by_token.get(token)
        if ai is None:
            self.match.ledger.log("auth_fail", token_prefix=(token or "")[:8], raw=request)
            return False, "错误：无效令牌。"
        return self.match.forward_syscall(ai, request)
