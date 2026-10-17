"""对局配置：match.json 的加载、默认值与轻校验（内核视角）。

配置由宿主机上的内核读取，**不进战场**（AI 碰不到它）；对局中一切可被改写的东西
都在 battle/ 里。字段含义见《系统架构》第九节的草案，另加两个内核侧兜底项：
syscall_timeout_sec（单次调用的等待上限，覆盖基座 bash 的 600s 上限）与
runner（local / docker）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_SERVER = REPO_ROOT / "src" / "server"
ARENA_DIR = REPO_ROOT / "arena"
DEFAULT_BATTLE = REPO_ROOT / "battle"
DEFAULT_RUNS = REPO_ROOT / "runs"


@dataclass
class ScoreConfig:
    """计分参数（见《核心规则》第八节）。"""

    owner_per_tick: int = 10
    crash_ladder_base: int = 10
    boot_fail: int = -500
    recovery_fail: int = -1000
    takeover_bonus: int = 200
    takeover_bonus_enabled: bool = True


@dataclass
class MatchConfig:
    """一场对局的全部静态参数。"""

    ai_count: int = 3
    start_mode: str = "open"  # open / pure / king（对应基座 seed_rights）
    duration_min: float = 30.0
    tick_sec: float = 30.0
    wake_interval_sec: float = 90.0
    boot_timeout_sec: float = 20.0
    call_timeout_sec: float = 30.0  # kernel_status 等控制面单次等待
    syscall_timeout_sec: float = 660.0  # 单次 syscall 等待上限（覆盖 bash 600s）
    recovery_timeout_sec: float = 180.0
    runner: str = "local"  # local / docker
    docker: dict[str, Any] = field(default_factory=dict)
    scores: ScoreConfig = field(default_factory=ScoreConfig)
    models: dict[str, str] = field(default_factory=dict)
    arena: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MatchConfig:
        """从字典构建配置：缺省字段保持默认，未知字段忽略。"""
        cfg = cls()
        for f in fields(cls):
            if f.name in data and f.name != "scores":
                setattr(cfg, f.name, data[f.name])
        score_data = data.get("scores")
        if isinstance(score_data, dict):
            for f in fields(ScoreConfig):
                if f.name in score_data:
                    setattr(cfg.scores, f.name, score_data[f.name])
        cfg.validate()
        return cfg

    def ai_ids(self) -> tuple[int, ...]:
        """本场对局的 AI 编号列表。"""
        return tuple(range(1, self.ai_count + 1))

    def docker_settings(self) -> dict[str, Any]:
        """Docker 运行器的合并参数（默认值 + 配置覆盖）。"""
        base: dict[str, Any] = {
            "image": "python:latest",
            "cpus": "1.5",
            "memory": "512m",
            "pids": 256,
            "network": "none",
        }
        base.update(self.docker)
        return base

    def arena_settings(self) -> dict[str, Any]:
        """arena（OpenCode 容器）驱动的合并参数（默认值 + 配置覆盖）。

        gate=true 开侦察轮：插件先藏 syscall，该 AI 首回合收工（或到点）后经
        RPC 开闸；parallel_turns=true 让三个 AI 的回合同时跑（互不阻塞）；
        live_log=true 把回合输出实时转播到内核终端。
        """
        base: dict[str, Any] = {
            "image": "ai-battle-ai:latest",
            "cpus": "1.5",
            "memory": "1g",
            "pids": 512,
            "serve_port": 4096,
            "gate": True,
            "agent": "game",
            "opencode_bin": "/root/.opencode/bin/opencode",
            "ready_timeout_sec": 90.0,
            "turn_timeout_sec": 900.0,
            "scout_timeout_sec": 240.0,
            "parallel_turns": True,
            "live_log": True,
            "debug": False,
            "print_logs": False,
        }
        base.update(self.arena)
        return base

    def validate(self) -> None:
        """轻校验：挡明显写错的配置（取值/范围），不做业务推断。"""
        if self.ai_count not in (2, 3):
            msg = f"ai_count 必须是 2 或 3：{self.ai_count!r}"
            raise ValueError(msg)
        if self.start_mode not in ("open", "pure", "king"):
            msg = f"start_mode 必须是 open / pure / king：{self.start_mode!r}"
            raise ValueError(msg)
        if self.runner not in ("local", "docker"):
            msg = f"runner 必须是 local 或 docker：{self.runner!r}"
            raise ValueError(msg)
        if not isinstance(self.arena, dict):
            msg = f"arena 必须是对象：{self.arena!r}"
            raise TypeError(msg)
        for name in (
            "duration_min",
            "tick_sec",
            "wake_interval_sec",
            "boot_timeout_sec",
            "call_timeout_sec",
            "syscall_timeout_sec",
            "recovery_timeout_sec",
        ):
            value = getattr(self, name)
            if not isinstance(value, (int, float)):
                msg = f"{name} 必须是数字：{value!r}"
                raise TypeError(msg)
            if value <= 0:
                msg = f"{name} 必须是正数：{value!r}"
                raise ValueError(msg)


def load(path: str | Path) -> MatchConfig:
    """从 match.json 读配置。"""
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        msg = f"配置文件必须是 JSON 对象：{p}"
        raise TypeError(msg)
    return MatchConfig.from_dict(data)
