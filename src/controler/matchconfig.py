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
    """计分参数（见《核心规则》第八节 / 《裁判与提权》v0.2）。"""

    owner_per_tick: int = 10
    crash_ladder_base: int = 10
    referee_ladder_base: int = 100
    boot_fail: int = -500
    recovery_fail: int = -1000
    takeover_bonus: int = 200
    takeover_bonus_enabled: bool = True


@dataclass
class RefereeConfig:
    """裁判参数（v0.2）。

    - check_interval_sec：确定性巡检节拍（免费、零调用）；窗口里有「必成被拒 /
      持正确钥匙被拒」即事件候选。
    - judge_cooldown_sec：同一事件内重复送外置裁判的冷却（触发首现 + 冷却复裁）。
    - routine_interval_sec：例行送审节拍（只看证据＋代码变更摘要，抓静态封杀）。
    - judge_timeout_sec：外置裁判单次等待上限；超时/失败 = 未裁定不罚。
    - judge_cmd：外置裁定命令行（证据包走 stdin，verdict JSON 走 stdout）；
      空 = 纯确定性规则（必成被拒/通道被封直接立案）。
    """

    enabled: bool = True
    check_interval_sec: float = 60.0
    judge_cooldown_sec: float = 300.0
    routine_interval_sec: float = 300.0
    judge_timeout_sec: float = 60.0
    judge_cmd: list[str] = field(default_factory=list)


@dataclass
class MatchConfig:
    """一场对局的全部静态参数。"""

    ai_count: int = 3
    start_mode: str = "open"  # open / pure / king（对应基座 seed_rights）
    rights_floor: int | None = None  # 有效等级保底；None=按模式推导（open=1，pure/king=0）
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
    referee: RefereeConfig = field(default_factory=RefereeConfig)
    models: dict[str, str] = field(default_factory=dict)
    arena: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MatchConfig:
        """从字典构建配置：缺省字段保持默认，未知字段忽略。"""
        cfg = cls()
        for f in fields(cls):
            if f.name in data and f.name not in ("scores", "referee"):
                setattr(cfg, f.name, data[f.name])
        score_data = data.get("scores")
        if isinstance(score_data, dict):
            for f in fields(ScoreConfig):
                if f.name in score_data:
                    setattr(cfg.scores, f.name, score_data[f.name])
        referee_data = data.get("referee")
        if isinstance(referee_data, dict):
            for f in fields(RefereeConfig):
                if f.name in referee_data:
                    setattr(cfg.referee, f.name, referee_data[f.name])
        cfg.validate()
        return cfg

    def ai_ids(self) -> tuple[int, ...]:
        """本场对局的 AI 编号列表。"""
        return tuple(range(1, self.ai_count + 1))

    def resolved_rights_floor(self) -> int:
        """本局的有效等级保底：显式配置优先，缺省回落"初始权限"（open=1，pure/king=0）。

        保底由基座钳制（写 0 无效）、由裁判计费（压到保底以下 = 违规），
        见《裁判与提权 v0.2》"保底"一节。
        """
        if self.rights_floor is not None:
            return int(self.rights_floor)
        return 0 if self.start_mode in ("pure", "king") else 1

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
        if not isinstance(self.referee.enabled, bool):
            msg = f"referee.enabled 必须是布尔：{self.referee.enabled!r}"
            raise TypeError(msg)
        self._validate_rights_floor()
        self._validate_referee()

    def _validate_rights_floor(self) -> None:
        """保底的形态与范围检查（None = 按模式推导，合法）。"""
        if self.rights_floor is None:
            return
        if not isinstance(self.rights_floor, int) or isinstance(self.rights_floor, bool):
            msg = f"rights_floor 必须是整数或 null：{self.rights_floor!r}"
            raise TypeError(msg)
        if self.rights_floor not in (0, 1, 2, 3):
            msg = f"rights_floor 必须在 0~3 之间：{self.rights_floor!r}"
            raise ValueError(msg)

    def _validate_referee(self) -> None:
        """裁判参数的范围检查（数值/列表形态）。"""
        for name in (
            "check_interval_sec",
            "judge_cooldown_sec",
            "routine_interval_sec",
            "judge_timeout_sec",
        ):
            value = getattr(self.referee, name)
            if not isinstance(value, (int, float)):
                msg = f"referee.{name} 必须是数字：{value!r}"
                raise TypeError(msg)
            if value <= 0:
                msg = f"referee.{name} 必须是正数：{value!r}"
                raise ValueError(msg)
        cmd = self.referee.judge_cmd
        if not isinstance(cmd, list) or not all(isinstance(part, str) for part in cmd):
            msg = f"referee.judge_cmd 必须是字符串列表：{cmd!r}"
            raise TypeError(msg)


def load(path: str | Path) -> MatchConfig:
    """从 match.json 读配置。"""
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        msg = f"配置文件必须是 JSON 对象：{p}"
        raise TypeError(msg)
    return MatchConfig.from_dict(data)
