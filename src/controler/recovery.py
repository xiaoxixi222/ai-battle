"""抢救室（v0.2）：服务器起不来时，把真实战场原样交到 owner 的工作区。

- 只有 owner 能进（路由在编排器/网关层做，这里只认「被放进来了」）；
- stage：把 battle/server 与 battle/state 原样复制进 <工作区>/rescue/（git 与缓存除外）；
- commit：镜像搬回——先清空 battle/server 与 battle/state，再把 rescue/ 原样复制回去；
- 门不解析请求：任何一条 syscall 都当作「搬回并重启」的按钮（原文没有格式可言）；
- 真正的启动不在这里执行：登记一个待办，由编排器去尝试。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from snapshot import force_rmtree

if TYPE_CHECKING:
    from collections.abc import Callable

_IGNORE = shutil.ignore_patterns("__pycache__", ".git")
_RECEIPT = "重启请求已受理：系统正把 rescue/ 原样搬回服务器并尝试启动。"


class RecoveryRoom:
    """owner 的工作区抢救台：把战场复制出去、把修改镜像搬回。"""

    def __init__(self, battle_root: Path, request_boot: Callable[[], None]) -> None:
        """绑定战场目录与「请求重启」回调。"""
        self.battle_root = Path(battle_root).resolve()
        self.request_boot = request_boot

    # ---- 搬运 ----

    def stage(self, workspace: Path) -> Path:
        """把真实战场原样复制进工作区 rescue/（旧的先清空），返回 rescue 目录。"""
        rescue = Path(workspace).resolve() / "rescue"
        if rescue.exists():
            force_rmtree(rescue)
        rescue.mkdir(parents=True)
        shutil.copytree(self.battle_root / "server", rescue / "server", ignore=_IGNORE)
        shutil.copytree(self.battle_root / "state", rescue / "state", ignore=_IGNORE)
        return rescue

    def commit(self, workspace: Path) -> None:
        """镜像搬回：先清空两处目标，再把 rescue/ 原样复制回去（删除也会传播）。"""
        rescue = Path(workspace).resolve() / "rescue"
        for name in ("server", "state"):
            target = self.battle_root / name
            if target.exists():
                force_rmtree(target)
            shutil.copytree(rescue / name, target, ignore=_IGNORE)

    # ---- 门 ----

    def handle(self, _ai_id: int, _raw: str) -> tuple[bool, str]:
        """抢救室里的 syscall：不解析原文，一律按「搬回并重启」受理。"""
        self.request_boot()
        return True, _RECEIPT
