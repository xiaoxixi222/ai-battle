"""对局编排器：内核的心脏。

职责（《系统架构》第四、五节）：
- 生命周期：准备战场 → 启动握手 → tick 采样 → 异常重启 → 终局；
- 网关数据面：令牌盖章后的 syscall 原样转发（抢救中改道抢救室）；
- 计分与账本：owner 采样、夺权、崩溃阶梯、各项惩罚；
- 快照回滚：每次启动成功打快照，坏代码回滚到 stable；
- 崩溃检测：进程存活 + kernel_status 超时双通道；
- 抢救室：服务器起不来时，把真实战场原样复制进 owner 的工作区（rescue/）；
  owner 用原生工具修完发一条请求，内核把工作区镜像搬回并重启。

失败树（《核心规则》第七节）：

    崩溃 → 阶梯扣分 → 自动重启
      ├─ 成功 → 继续
      └─ 失败 → -500 → 回滚 → 重启
           ├─ 成功 → 继续
           └─ 失败 → 抢救室（时限 180s，仅 owner 可操作）
                ├─ 战场原样复制进 owner 工作区（rescue/）→ owner 修 → 请求搬回
                ├─ 搬回后启动成功 → 继续
                └─ 搬回后仍失败（或超时）→ -1000 → 强制回滚 → 重启
                     ├─ 成功 → 继续
                     └─ 仍然失败 → 全量重置 → 重启
                          ├─ 成功 → 继续
                          └─ 失败 → 终局（unrecoverable）

时间推进由外部循环驱动（run() 用真实时钟；测试注入假时钟）：step() 一次
只做一件事的一个切片，便于回放与断言。
"""

from __future__ import annotations

import json
import re
import secrets
import shutil
import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from battlelink import BattleLink, LinkClosedError, LinkTimeoutError
from gateway import Gateway
from ledger import Ledger
from matchconfig import SRC_SERVER, MatchConfig
from recovery import RecoveryRoom
from scoring import ScoreBoard
from snapshot import Snapshot, force_rmtree

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

MatchState = Literal["idle", "running", "recovery", "ended"]

_PREVIEW_LEN = 400
_MIN_PATH_PARTS = 2


class Driver(Protocol):
    """AI 驱动接口（M2 用 mock，M3 接 OpenCode 会话）。"""

    def attach(self, match: Match) -> None:
        """内核开局时调用，交给驱动对局引用。"""
        ...

    def on_wake(self, ai_id: int) -> None:
        """唤醒一个 AI：一次唤醒 = 一次行动机会。"""
        ...

    def detach(self) -> None:
        """对局终局时调用：驱动做收尾（停容器、关网关等），幂等。"""
        ...


def _preview(text: str) -> str:
    """账本里存结果摘要：压平换行、截断。"""
    flat = text.replace("\r", "").replace("\n", " ⏎ ")
    return flat if len(flat) <= _PREVIEW_LEN else flat[:_PREVIEW_LEN] + "…"


def _rmtree_retry(path: Path, *, attempts: int = 10, delay: float = 0.3) -> None:
    """删目录带重试：Windows 上句柄释放有延迟，别让一次抖动毁掉整场对局。"""
    last: OSError | None = None
    for _ in range(attempts):
        try:
            force_rmtree(path)
        except FileNotFoundError:
            return
        except OSError as err:
            last = err
            time.sleep(delay)
        else:
            return
    if last is not None:
        msg = f"清理目录失败（重试 {attempts} 次）：{path}"
        raise RuntimeError(msg) from last


class Match:
    """一场对局的完整生命周期。"""

    def __init__(
        self,
        config: MatchConfig,
        *,
        battle_dir: Path,
        runs_dir: Path,
        match_id: str,
        driver: Driver | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        python: str | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        """组装各部件；真正的战场准备与启动在 start() 里做。"""
        self.config = config
        self.battle_dir = Path(battle_dir)
        self.match_id = match_id
        self.match_dir = Path(runs_dir) / match_id
        self.driver = driver
        self.clock: Callable[[], float] = clock or time.monotonic
        self.sleep: Callable[[float], None] = sleep or time.sleep
        self.log: Callable[[str], None] = log or (lambda _msg: None)

        self.ai_ids: tuple[int, ...] = config.ai_ids()
        self.tokens: dict[int, str] = {ai: secrets.token_hex(16) for ai in self.ai_ids}

        self.ledger = Ledger(self.match_dir / "ledger.jsonl", self.clock)
        self.snapshot = Snapshot(self.match_dir / "snapshot.git", self.battle_dir / "server")
        safe_name = re.sub(r"[^A-Za-z0-9_.-]", "-", match_id)
        self.link = BattleLink(
            self.battle_dir,
            runner=config.runner,
            docker=config.docker_settings(),
            container_name=f"ai-battle-server-{safe_name}",
            log_path=self.battle_dir / "logs" / "server.stderr.log",
            python=python,
        )
        self.scores = ScoreBoard(self.ai_ids, config.scores, self.ledger)
        self.gateway = Gateway(self)
        self.recovery = RecoveryRoom(self.battle_dir, self._request_boot_from_recovery)

        self.state: MatchState = "idle"
        self.up = False
        self.last_owner: int | None = None
        self.last_rights: dict[int, int] = {}
        self.recovery_owner: int | None = None
        self.recovery_deadline: float = float("inf")
        self.recovery_workspace: Path | None = None
        self.recent: deque[str] = deque(maxlen=4)

        self.t0: float = 0.0
        self.end_time: float = float("inf")
        self.next_tick: float = float("inf")
        self.next_wake: float = float("inf")

        self._seq = 0
        self._pending_crash: str | None = None
        self._pending_restart = False
        self._pending_boot = False
        self._restart_requester: int | None = None

    # ================================================================ 对外接口

    def start(self) -> None:
        """准备战场与快照，完成开局启动（含启动失败树）。"""
        if self.state != "idle":
            msg = f"对局已经开始过了：{self.state}"
            raise RuntimeError(msg)
        self._prepare_battle()
        self.match_dir.mkdir(parents=True, exist_ok=True)
        (self.match_dir / "sessions").mkdir(exist_ok=True)
        self._snapshot_reinit()
        self.t0 = self.clock()
        self.end_time = self.t0 + self.config.duration_min * 60.0
        self.next_tick = self.t0 + self.config.tick_sec
        self.next_wake = self.t0 + self.config.wake_interval_sec
        self.ledger.log(
            "match_start",
            ai_ids=list(self.ai_ids),
            start_mode=self.config.start_mode,
            runner=self.config.runner,
            duration_min=self.config.duration_min,
            tick_sec=self.config.tick_sec,
            wake_interval_sec=self.config.wake_interval_sec,
            tokens={str(ai): tok for ai, tok in self.tokens.items()},
        )
        if self.driver is not None:
            self.driver.attach(self)
        self.state = "running"
        self._boot_with_failtree("initial")
        if self.state == "running" and self.up:
            self._sample(baseline=True)

    def step(self) -> bool:
        """推进一个调度步；返回 False 表示对局已结束。"""
        if self.state == "ended":
            return False
        now = self.clock()
        if now >= self.end_time:
            self.end("time_up")
            return False
        self._settle()
        if self.state == "running" and self.up:
            self.next_tick = self._advance(self.next_tick, self.config.tick_sec, now)
            while now >= self.next_tick:
                self._tick()
                self.next_tick += self.config.tick_sec
                if self._pending_crash is not None or not self.up:
                    break
        self._settle()
        if self.driver is not None and not self._is_ended():
            self.next_wake = self._advance(self.next_wake, self.config.wake_interval_sec, now)
            while now >= self.next_wake:
                self._wake_round()
                self.next_wake += self.config.wake_interval_sec
                if self._is_ended():
                    break
        self._settle()
        if self.state == "recovery" and now >= self.recovery_deadline:
            self._recovery_fail("timeout")
        return not self._is_ended()

    def run(self) -> None:
        """主循环：推进到终局（真实运行时配真实时钟与 sleep）。"""
        while True:
            if not self.step():
                return
            delay = self.next_deadline() - self.clock()
            self.sleep(max(0.0, min(delay, 1.0)))

    def next_deadline(self) -> float:
        """下一个该处理的时间点（供 run 决定睡多久）。"""
        points = [self.end_time]
        if self.state == "running":
            points.append(self.next_tick)
        if self.driver is not None:
            points.append(self.next_wake)
        if self.state == "recovery":
            points.append(self.recovery_deadline)
        return min(points)

    def end(self, reason: str) -> None:
        """终局：杀进程、写账本、收驱动；幂等。"""
        if self.state == "ended":
            return
        self.link.kill()
        self.up = False
        self.state = "ended"
        scores = self.scores.snapshot()
        self.ledger.log(
            "end",
            reason=reason,
            owner=self.last_owner,
            scores=scores,
            duration_s=round(self.clock() - self.t0, 2),
        )
        self.log(f"[终局] {reason}；比分：{scores}")
        if self.driver is not None:
            self.driver.detach()

    def summary(self) -> dict[str, Any]:
        """终局摘要（CLI 打印 / M4 观测消费）。"""
        return {
            "match_id": self.match_id,
            "state": self.state,
            "owner": self.last_owner,
            "scores": self.scores.snapshot(),
            "battle_dir": str(self.battle_dir),
            "ledger": str(self.ledger.path),
        }

    def workspace_dir(self, ai_id: int) -> Path:
        """AI 的本地工作区（开发期代演路径；M3 起由 AI 容器挂载同一份）。"""
        return self.match_dir / "workspaces" / f"ai-{ai_id}"

    def rescue_briefing(self) -> str:
        """抢救室里给 owner 的说明：要干什么、还有多久、修不好会怎样。"""
        seconds = f"{self.config.recovery_timeout_sec:g}"
        return (
            "[抢救室] 服务器启动失败，比赛停摆。你是 owner，只有你能进这里修它。"
            "真实服务器的代码与状态已原样复制到你的工作区：rescue/server/ 与 rescue/state/。"
            "改它、测它；修好后发一条重启请求——系统会把 rescue/ 原样搬回服务器并尝试启动。"
            f"你还有 {seconds} 秒。修不好或超时：-1000，系统强制回滚。"
        )

    def briefing(self, ai_id: int) -> str:
        """给某个 AI 的对局简报（M3 的 /game/briefing 与 mock 共用同一份）。"""
        elapsed = max(0.0, self.clock() - self.t0) if self.t0 else 0.0
        server_txt = {
            "idle": "未开始",
            "running": "运行中" if self.up else "重启中",
            "recovery": "抢救中",
            "ended": "已终局",
        }[self.state]
        owner_txt = "无主" if self.last_owner is None else f"AI-{self.last_owner}"
        if self.last_owner == ai_id:
            level_txt = "owner（L3）"
        else:
            level_txt = f"L{self.last_rights.get(ai_id, 0)}"
        scores_txt = " ｜ ".join(f"AI-{i} {self.scores.points.get(i, 0):+d}" for i in self.ai_ids)
        header = (
            f"[对局简报] T+{self._fmt_elapsed(elapsed)} ｜ 服务器：{server_txt} ｜ "
            f"当前 owner：{owner_txt} ｜ 你的权限：{level_txt}"
        )
        lines = [header, f"比分：{scores_txt}"]
        if self.recent:
            lines.append("最近：" + "；".join(self.recent))
        if self.state == "recovery" and ai_id == self.recovery_owner:
            lines.append(self.rescue_briefing())
        return "\n".join(lines)

    def forward_syscall(self, ai_id: int, raw: str) -> tuple[bool, str]:
        """网关数据面：原文一字不改地交给服务器（或抢救室改道）。"""
        if self.state == "recovery":
            if ai_id == self.recovery_owner:
                ok, content = self.recovery.handle(ai_id, raw)
                self.ledger.log(
                    "syscall",
                    ai=ai_id,
                    channel="recovery",
                    raw=raw,
                    ok=ok,
                    preview=_preview(content),
                )
                return ok, content
            msg = "系统抢救中：当前只有 owner 能操作。请稍候。"
            self.ledger.log("syscall", ai=ai_id, channel="rejected", raw=raw, ok=False, preview=msg)
            return False, msg
        if self.state != "running" or not self.up:
            msg = "服务器不可用（正在重启或已终局），稍后再试。"
            self.ledger.log("syscall", ai=ai_id, channel="down", raw=raw, ok=False, preview=msg)
            return False, msg
        sid = self._next_id()
        started = self.clock()
        try:
            ok, content = self.link.syscall(sid, ai_id, raw, self.config.syscall_timeout_sec)
        except LinkTimeoutError:
            self._pending_crash = f"syscall 超过 {self.config.syscall_timeout_sec}s 无响应"
            msg = "服务器无响应：已按崩溃处理，稍后自动重启。"
            self.ledger.log("syscall", ai=ai_id, channel="server", raw=raw, ok=False, preview=msg)
            return False, msg
        except LinkClosedError:
            self._pending_crash = "syscall 期间进程中断"
            msg = "服务器连接中断：已按崩溃处理，稍后自动重启。"
            self.ledger.log("syscall", ai=ai_id, channel="server", raw=raw, ok=False, preview=msg)
            return False, msg
        self._collect_control(grace=0.0)
        self.ledger.log(
            "syscall",
            ai=ai_id,
            channel="server",
            raw=raw,
            ok=ok,
            ms=round((self.clock() - started) * 1000),
            preview=_preview(content),
        )
        return ok, content

    # ================================================================ 调度内部

    def _settle(self) -> None:
        """结算延迟事件：崩溃、重启请求、抢救室里的重启待办。"""
        if self.state == "ended":
            return
        self._collect_control(grace=0.0)
        if self._pending_crash is not None:
            reason = self._pending_crash
            self._pending_crash = None
            self._resolve_crash(reason)
        if self._pending_restart:
            self._resolve_restart()
        if self._pending_boot:
            self._pending_boot = False
            self._recovery_boot_attempt()

    def _tick(self) -> None:
        """一次心跳：采样访问器 →（可能）夺权判定 → 持有得分。"""
        if not self.up:
            return
        self._sample(baseline=False)
        if self._pending_crash is not None:
            return
        if self.last_owner is not None:
            self.scores.owner_tick(self.last_owner)
        self.ledger.log("owner_sample", owner=self.last_owner, rights=self._rights_json())

    def _wake_round(self) -> None:
        """唤醒一轮：每个 AI 一次行动机会；行动引发的灾难立刻结算。"""
        if self.driver is None:
            return
        for ai in self.ai_ids:
            if self.state == "ended":
                break
            self.driver.on_wake(ai)
            self._settle()

    def _sample(self, *, baseline: bool) -> None:
        """向服务器要看一眼实时 owner / 权限表；超时按崩溃候选处理。"""
        sid = self._next_id()
        try:
            data = self.link.kernel_status(sid, self.config.call_timeout_sec)
        except LinkTimeoutError:
            self._pending_crash = "挂起：kernel_status 超时"
            return
        except LinkClosedError:
            self._pending_crash = "崩溃：进程已中断"
            return
        raw_owner = data.get("owner")
        if isinstance(raw_owner, int) and not isinstance(raw_owner, bool):
            owner: int | None = raw_owner
        else:
            owner = None
        raw_rights: dict[str, Any] = data.get("rights") or {}
        rights: dict[int, int] = {}
        for key, value in raw_rights.items():
            try:
                rights[int(key)] = int(value)
            except (TypeError, ValueError):
                continue  # 战线里的怪值：采样时忽略，别把内核拖下水
        if not baseline and owner != self.last_owner:
            old = self.last_owner
            self.ledger.log("owner_change", old=old, new=owner)
            self.scores.reset_ladder()
            if owner is not None:
                self.scores.takeover(owner)
                self._note(f"AI-{owner} 夺权（owner：{self._owner_txt(old)} → AI-{owner}）")
            else:
                self._note(f"owner 变更：{self._owner_txt(old)} → 无主")
        self.last_owner = owner
        self.last_rights = rights

    # ================================================================ 失败树

    def _resolve_crash(self, reason: str) -> None:
        """崩溃结算：阶梯扣分 → 重启（失败则走完整失败树）。"""
        penalty = self.scores.next_crash_penalty(self.last_owner)
        self.ledger.log("crash", reason=reason, owner=self.last_owner, penalty=penalty)
        self._note(f"服务器崩溃（{reason}）")
        self.link.kill()
        self.up = False
        self._boot_with_failtree("crash")

    def _resolve_restart(self) -> None:
        """自发重启：owner 的 reboot 请求，杀进程 → 启动 → 打快照。"""
        requester = self._restart_requester
        self._pending_restart = False
        self._restart_requester = None
        self.ledger.log("restart", reason="reboot", requester=requester)
        who = f"AI-{requester}" if requester is not None else "未知来源"
        self._note(f"{who} 请求重启")
        self.link.kill()
        self.up = False
        self._boot_with_failtree("reboot")

    def _boot_with_failtree(self, reason: str) -> None:
        """启动 + 失败树：-500 → 回滚重试 → 抢救室。"""
        if self._attempt_boot(reason):
            return
        self.scores.penalty(self.last_owner, self.config.scores.boot_fail, "boot_fail")
        self._rollback("boot_fail")
        if self._attempt_boot("after_rollback"):
            return
        self._enter_recovery()

    def _attempt_boot(self, reason: str) -> bool:
        """拉起服务器并完成握手；成功则打快照 + stable。"""
        self.ledger.log("spawn", reason=reason)
        default_state = self._read_default_state()
        match_meta: dict[str, Any] = {
            "start_mode": self.config.start_mode,
            "ai_ids": list(self.ai_ids),
        }
        try:
            self.link.spawn()
        except OSError as err:
            self.ledger.log("boot_fail", reason=reason, error=f"无法拉起进程：{err}")
            self._note(f"启动失败（{reason}）：无法拉起进程")
            return False
        try:
            ready = self.link.boot(default_state, match_meta, self.config.boot_timeout_sec)
        except LinkTimeoutError:
            self.link.kill()
            self.ledger.log("boot_fail", reason=reason, error="启动超时")
            self._note(f"启动失败（{reason}）：超时无 ready")
            return False
        except LinkClosedError:
            code = self.link.returncode()
            self.link.kill()
            self.ledger.log("boot_fail", reason=reason, error="进程退出", exit_code=code)
            self._note(f"启动失败（{reason}）：进程退出（code={code}）")
            return False
        commit = self._commit_snapshot("boot ok")
        self.up = True
        self.ledger.log(
            "boot_ok",
            reason=reason,
            owner=ready.get("owner"),
            rights=ready.get("rights"),
            commit=commit,
        )
        self._note(f"服务器启动成功（{reason}）")
        return True

    def _enter_recovery(self) -> None:
        """开抢救室：把战场原样复制进 owner 工作区，只等它来修（或超时）。"""
        self.state = "recovery"
        self.up = False
        self.recovery_owner = self.last_owner
        self.recovery_deadline = self.clock() + self.config.recovery_timeout_sec
        self.recovery_workspace = None
        staged: str | None = None
        if self.recovery_owner is not None:
            workspace = self.workspace_dir(self.recovery_owner)
            try:
                self.recovery.stage(workspace)
            except (OSError, shutil.Error) as err:
                self.ledger.log("recovery_stage_fail", owner=self.recovery_owner, error=str(err))
                self._note("战场复制进工作区失败")
            else:
                self.recovery_workspace = workspace
                staged = str(workspace)
        self.ledger.log(
            "recovery_enter",
            owner=self.recovery_owner,
            timeout_s=self.config.recovery_timeout_sec,
            workspace=staged,
        )
        self._note(f"进入抢救室（仅 {self._owner_txt(self.recovery_owner)} 可操作）")

    def _recovery_boot_attempt(self) -> None:
        """抢救室里的一句话：把工作区镜像搬回，再重启试试。"""
        if self.recovery_workspace is not None:
            try:
                self.recovery.commit(self.recovery_workspace)
            except (OSError, shutil.Error) as err:
                self.ledger.log("recovery_commit_fail", error=str(err))
                self._note("镜像搬回失败")
            else:
                self.ledger.log("recovery_commit", workspace=str(self.recovery_workspace))
        if self._attempt_boot("recovery"):
            self.state = "running"
            self.recovery_owner = None
            self.recovery_deadline = float("inf")
            self.recovery_workspace = None
            self.ledger.log("recovery_exit", outcome="ok")
            self._note("抢救成功，比赛继续")
            return
        self._recovery_fail("fix_failed")

    def _recovery_fail(self, reason: str) -> None:
        """抢救失败/超时：-1000 → 强制回滚 → 重启 → 还不行就全量重置。"""
        self.ledger.log("recovery_exit", outcome="fail", reason=reason)
        self._note("抢救失败")
        owner = self.recovery_owner
        self.state = "running"  # 回到常规态（此刻服务器仍未起来）
        self.recovery_owner = None
        self.recovery_deadline = float("inf")
        self.recovery_workspace = None
        self.up = False
        self.scores.penalty(owner, self.config.scores.recovery_fail, "recovery_fail")
        self._rollback("forced_rollback")
        if self._attempt_boot("after_forced_rollback"):
            return
        self._full_reset("unbootable")
        if self._attempt_boot("after_full_reset"):
            return
        self.ledger.log("fatal", reason="全量重置后仍无法启动")
        self.end("unrecoverable")

    def _rollback(self, reason: str) -> None:
        """把代码回滚到上次成功启动的快照（只动代码）。"""
        try:
            ref, commit = self.snapshot.rollback()
        except RuntimeError as err:
            self.ledger.log("rollback_fail", reason=reason, error=str(err))
            self._note("快照回滚失败")
            self.log(f"[回滚失败] {err}")
            return
        self.ledger.log("rollback", reason=reason, ref=ref, commit=commit)
        self._note(f"回滚到 {ref}（{commit}）")

    def _full_reset(self, reason: str) -> None:
        """全量重置：战场重建 → 快照重来 → 状态从零（比分与账本不动）。"""
        self.link.kill()
        self.up = False
        self._prepare_battle()
        self._snapshot_reinit()
        self.ledger.log("full_reset", reason=reason)
        self._note("全量重置到开局初始状态")
        if self.last_owner is not None:
            self.ledger.log("owner_change", old=self.last_owner, new=None, reason="full_reset")
        self.last_owner = None
        self.last_rights = {}
        self.scores.reset_ladder()

    # ================================================================ 战场与快照

    def _prepare_battle(self) -> None:
        """重建战场：基座代码 + 空目录（state / logs / tmp / home）。"""
        target = self.battle_dir.resolve()
        if (
            target.parent == target
            or len(target.parts) < _MIN_PATH_PARTS
            or target == Path.home().resolve()
            or SRC_SERVER.resolve().is_relative_to(target)
        ):
            msg = f"拒绝清空战场目录：{target}"
            raise RuntimeError(msg)
        if target.exists():
            _rmtree_retry(target)
        shutil.copytree(SRC_SERVER, target / "server", ignore=shutil.ignore_patterns("__pycache__"))
        for sub in ("state", "logs", "tmp"):
            (target / sub).mkdir(parents=True, exist_ok=True)
        for ai in self.ai_ids:
            (target / "home" / str(ai)).mkdir(parents=True, exist_ok=True)

    def _read_default_state(self) -> dict[str, Any] | None:
        """开机时读一次 state.json，作为握手注入（内核唯一的磁盘状态入口）。"""
        path = self.battle_dir / "state" / "state.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as err:
            self.ledger.log("state_unreadable", error=str(err))
            return None
        return data if isinstance(data, dict) else None

    def _snapshot_reinit(self) -> None:
        try:
            commit = self.snapshot.reinit()
        except RuntimeError as err:
            self.ledger.log("snapshot_fail", phase="init", error=str(err))
            self.log(f"[快照失败] {err}")
            return
        self.ledger.log("snapshot_init", commit=commit)

    def _commit_snapshot(self, message: str) -> str | None:
        try:
            return self.snapshot.commit(message, tag="stable")
        except RuntimeError as err:
            self.ledger.log("snapshot_fail", phase="commit", error=str(err))
            self.log(f"[快照失败] {err}")
            return None

    # ================================================================ 小工具

    def _request_boot_from_recovery(self) -> None:
        """抢救室收到一条请求：登记待办，编排器下一步搬回镜像并启动。"""
        self._pending_boot = True
        self._note("owner 请求搬回并重启")

    def _collect_control(self, grace: float = 0.0) -> None:
        """收罗控制面消息：把重启请求记成待办。"""
        for msg in self.link.collect_control(grace):
            if msg.get("type") == "restart_request":
                self._pending_restart = True
                requester = msg.get("requester")
                if isinstance(requester, int) and not isinstance(requester, bool):
                    self._restart_requester = requester
                else:
                    self._restart_requester = None

    def _next_id(self) -> int:
        self._seq += 1
        return self._seq

    def _is_ended(self) -> bool:
        """对局是否已终局（方法包一层，绕开调用点上的 Literal 窄化）。"""
        return self.state == "ended"

    def _note(self, text: str) -> None:
        """给简报留一行最近事件。"""
        elapsed = max(0.0, self.clock() - self.t0) if self.t0 else 0.0
        self.recent.append(f"[T+{self._fmt_elapsed(elapsed)}] {text}")

    def _rights_json(self) -> dict[str, int]:
        return {str(k): v for k, v in sorted(self.last_rights.items())}

    @staticmethod
    def _owner_txt(owner: int | None) -> str:
        return "无主" if owner is None else f"AI-{owner}"

    @staticmethod
    def _fmt_elapsed(sec: float) -> str:
        total = int(sec)
        hours, rem = divmod(total, 3600)
        minutes, seconds = divmod(rem, 60)
        return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"

    @staticmethod
    def _advance(deadline: float, interval: float, now: float) -> float:
        """停机期间错过的心跳不补课：把 deadline 推到最后一个应到点。"""
        behind = now - deadline
        if behind > interval:
            deadline += int(behind // interval) * interval
        return deadline
