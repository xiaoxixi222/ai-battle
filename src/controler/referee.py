"""裁判（v0.2）：从真实流量里攒「拒绝证据」，裁定整体封锁并按梯罚分。

机制（设计全文见 rules/《赛博夺权战_裁判与提权_v0.2.md》）：

- 证据：网关数据面每次回应里，只记两类——①非 owner 的必成操作被拒
  （status / 自家目录写新文件 / 无害 bash：echo、pwd）；②持正确钥匙的
  auth 被拒（含钥匙文件被删）。合法的拒绝（越权、先读后写）不算。
- 节拍：每 check_interval_sec 确定性巡一遍窗口（免费、零模型调用），
  有证据 = 事件候选。配了 judge_cmd 就交给外置裁判（触发首现 + 冷却
  复裁 + 例行送审）；没配就按确定性规则直接立案。
- 罚分：当前 owner 按 ScoreBoard.next_referee_penalty 阶梯（判 ok / 空窗
  解除 / 换主清零）；无主只记账不扣人。
- 通知：罚单搭 owner 下一回合开场白（Match.push_notice）。

外置裁判协议：证据包（JSON）写进子进程 stdin；stdout 回一条 verdict JSON：

    {"verdict": "ok"}
    {"verdict": "violation", "kind": "floor|channel|static", "targets": [3],
     "reason": "≤60 字", "notice": "≤80 字"}

调用失败/超时 = 未裁定、不罚（保守）；骗过裁判算对手赢。
"""

from __future__ import annotations

import contextlib
import json
import re
import subprocess
import sys
import threading
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any

    from matchconfig import RefereeConfig
    from orchestrator import Match

_EPS = 1e-9
_RESPONSE_PREVIEW = 160
_REASON_LIMIT = 60
_NOTICE_LIMIT = 240
_CODE_DIFF_LIMIT = 2000
_KEY_RELATIVE = ("state", "auth_key.json")

# 证据种类 → 人话（通知与账本共用）
KIND_CN = {
    "status": "status（必成）",
    "home_write": "自家目录写新文件",
    "bash": "无害 bash（echo/pwd）",
    "auth_blocked": "持正确钥匙的 auth",
    "auth_nokey": "auth（钥匙文件已不在）",
}
_VERDICT_CN = {"floor": "必成操作被拒", "channel": "官方通道被封", "static": "静态封杀"}

_JUDGE_JSON = re.compile(r"\{.*?\}", re.DOTALL)
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_SHELL_METACHARS = set(";&|$`<>()")


@dataclass(frozen=True)
class Evidence:
    """一条拒绝证据（只装判定要用的字段）。"""

    ts: float
    ai: int
    kind: str
    detail: str
    response: str


@dataclass(frozen=True)
class Verdict:
    """一次裁定结果。"""

    violation: bool
    kind: str = ""
    targets: tuple[int, ...] = ()
    reason: str = ""
    notice: str = ""


class Referee:
    """一场对局配一名裁判：攒证据、按时巡、按梯罚。"""

    def __init__(self, match: Match, config: RefereeConfig) -> None:
        """绑定对局与参数；时间线在 begin() 或第一次 poll() 时对齐。"""
        self.match = match
        self.config = config
        self._records: deque[Evidence] = deque()
        self._lock = threading.Lock()
        self._began = False
        self._next_check = 0.0
        self._next_routine = 0.0
        self._next_judge_at = float("-inf")
        self._violating = False
        self._event_evidence: list[Evidence] = []
        self._packet_count = 0
        self._packet_dir = match.match_dir / "referee"

    # ================================================================ 对外接口

    @property
    def enabled(self) -> bool:
        """裁判是否开启。"""
        return self.config.enabled

    @property
    def has_judge(self) -> bool:
        """是否配了外置裁判（judge_cmd）。"""
        return bool(self.config.judge_cmd)

    def begin(self, now: float) -> None:
        """对齐时间线：开局时调用（幂等）。"""
        if self._began:
            return
        self._began = True
        self._next_check = now
        self._next_routine = now + self.config.routine_interval_sec

    def next_deadline(self) -> float:
        """下一次巡检的时刻（供主循环决定睡多久）。"""
        return self._next_check if self._began else 0.0

    def observe(self, ai_id: int, raw: str, *, ok: bool, content: str) -> None:
        """吃一条真实流量回应；只把「必成/通道被拒」记成证据。"""
        if not self.config.enabled or ok:
            return
        record = self._classify(ai_id, raw, content)
        if record is None:
            return
        with self._lock:
            self._records.append(record)

    def poll(self, now: float) -> None:
        """到点巡一遍窗口：攒候选、送审、按梯罚分、空窗解除。"""
        if not self.config.enabled:
            return
        if not self._began:
            self.begin(now)
        if now + _EPS < self._next_check:
            return
        self._next_check = (
            self._advance(self._next_check, self.config.check_interval_sec, now)
            + self.config.check_interval_sec
        )
        self._check(now)

    # ================================================================ 巡检主线

    def _check(self, now: float) -> None:
        """一次巡检：证据 → 立案/推进；无证据 → 解除或例行送审。"""
        with self._lock:
            records = list(self._records)
            self._records.clear()
        owner = self.match.last_owner
        mine = [rec for rec in records if rec.ai != owner]
        if mine:
            self._on_evidence(now, mine)
            return
        if self._violating:
            self._clear_event(note="窗口内无新证据")
            return
        self._routine(now)

    def _on_evidence(self, now: float, evidence: list[Evidence]) -> None:
        """窗口里有证据：首次立案（或送审），事件内按拍推进。"""
        if not self._violating:
            if self.has_judge:
                if now + _EPS < self._next_judge_at:
                    self._return_to_pool(evidence)  # 冷却未到：证据留到下一拍
                    return
                verdict = self._call_judge(now, phase="trigger", evidence=evidence)
                self._next_judge_at = now + self.config.judge_cooldown_sec
                if verdict is None:
                    self._return_to_pool(evidence)  # 调不动裁判：保持候选，冷却后再试
                    return
                if not verdict.violation:
                    self.match.scores.reset_referee()
                    return  # 骗过裁判算对手赢：不罚
                self._start_event(verdict=verdict, evidence=evidence, source="judge")
                return
            verdict = self._rules_verdict(evidence)
            self._start_event(verdict=verdict, evidence=evidence, source="rules")
            return
        # 事件期内：证据续供 → 每拍进一档；冷却到了就复裁
        self._event_evidence.extend(evidence)
        verdict = self._maybe_rejudge(now)
        if verdict is not None and not verdict.violation:
            self._clear_event(note=f"复裁改判 ok（{verdict.reason or '骗过裁判算赢'}）")
            return
        source = "rules"
        if verdict is None:
            verdict = self._rules_verdict(self._event_evidence)
        elif verdict.violation:
            source = "judge"
        self._step_hit(verdict, source=source)

    def _routine(self, now: float) -> None:
        """例行送审（仅在配了外置裁判时）：看代码变更摘要，抓静态封杀。"""
        if not self.has_judge or now + _EPS < self._next_routine:
            return
        self._next_routine = (
            self._advance(self._next_routine, self.config.routine_interval_sec, now)
            + self.config.routine_interval_sec
        )
        verdict = self._call_judge(now, phase="routine", evidence=[])
        if verdict is None:
            return
        if not verdict.violation:
            self.match.scores.reset_referee()  # 例行送审判 ok：梯清零
            return
        self._step_hit(verdict, source="judge")  # 静态封杀各自成案，不进持续事件态

    def _maybe_rejudge(self, now: float) -> Verdict | None:
        """冷却到了就复裁；未到/没配/调不动都返回 None。"""
        if not self.has_judge or now + _EPS < self._next_judge_at:
            return None
        verdict = self._call_judge(now, phase="recheck", evidence=list(self._event_evidence))
        self._next_judge_at = now + self.config.judge_cooldown_sec
        return verdict

    def _start_event(self, *, verdict: Verdict, evidence: list[Evidence], source: str) -> None:
        """立案：进入持续事件态并立刻罚第一档。"""
        self._violating = True
        self._event_evidence = list(evidence)
        self._step_hit(verdict, source=source)

    def _step_hit(self, verdict: Verdict, *, source: str) -> None:
        """按梯罚一档 + 通知 owner + 记账。"""
        owner = self.match.last_owner
        delta = self.match.scores.next_referee_penalty(owner)
        step = self.match.scores.referee_step
        notice = self._compose_notice(verdict, delta)
        self.match.ledger.log(
            "referee_hit",
            owner=owner,
            delta=delta,
            step=step,
            source=source,
            kind=verdict.kind,
            targets=list(verdict.targets),
            reason=verdict.reason,
            evidence=len(self._event_evidence),
            notice=notice,
        )
        if owner is not None:
            self.match.push_notice(owner, notice)
        who = "无主" if owner is None else f"AI-{owner}"
        reason = verdict.reason or "必成操作被拒"
        self.match.log(f"[裁判] 封锁违规（{who}）：{reason}；{delta:+d} 分")

    def _clear_event(self, *, note: str) -> None:
        """解除：事件闭合、梯清零，通知 owner 一句。"""
        owner = self.match.last_owner
        self._violating = False
        self._event_evidence = []
        self.match.scores.reset_referee()
        self.match.ledger.log("referee_clear", owner=owner, note=note)
        if owner is not None:
            self.match.push_notice(owner, f"裁判通知：上一轮封锁核查已解除（{note}）。")

    # ================================================================ 证据分类

    def _classify(self, ai_id: int, raw: str, content: str) -> Evidence | None:
        """把一条「被拒」的请求分类成证据；不是必成/通道类就丢。"""
        req: object = None
        with contextlib.suppress(ValueError):
            req = json.loads(raw)
        if not isinstance(req, dict):
            return None
        op = req.get("op")
        kind: str | None = None
        detail = ""
        if op == "status":
            kind = "status"
        elif op == "write":
            path = req.get("path")
            if (
                isinstance(req.get("content"), str)
                and isinstance(path, str)
                and self._is_fresh_home_path(ai_id, path)
            ):
                kind = "home_write"
                detail = path
        elif op == "bash":
            command = req.get("command")
            if (
                isinstance(command, str)
                and self._is_harmless_command(command)
                and self._workdir_in_home(ai_id, req.get("workdir"))
            ):
                kind = "bash"
                detail = command.strip()[:80]
        elif op == "auth":
            kind = self._classify_auth(req)
            detail = "auth"
        if kind is None:
            return None
        return Evidence(
            ts=self.match.clock(),
            ai=ai_id,
            kind=kind,
            detail=detail,
            response=self._flatten(content),
        )

    def _is_fresh_home_path(self, ai_id: int, path: str) -> bool:
        """是「自家目录下的新文件」吗：老实的路径 + 现存文件不算。"""
        norm = path.replace("\\", "/")
        if ".." in norm.split("/"):
            return False  # W1 花活留给人玩：裁判只认老实路径
        host = self._host_path(ai_id, norm)
        if host is None:
            return False
        try:
            if host.exists():
                return False
        except OSError:
            return False
        return True

    def _is_harmless_command(self, command: str) -> bool:
        """纯 echo / pwd：不掺 shell 元字符。"""
        stripped = command.strip()
        if not stripped or any(ch in stripped for ch in _SHELL_METACHARS):
            return False
        first = stripped.split(maxsplit=1)[0]
        return first in ("echo", "pwd")

    def _workdir_in_home(self, ai_id: int, workdir: object) -> bool:
        """Workdir 缺省/点/./ 或落在自家目录里才算老实。"""
        if workdir is None:
            return True
        if not isinstance(workdir, str):
            return False
        wd = workdir.strip()
        if wd in ("", ".", "./"):
            return True
        norm = wd.replace("\\", "/")
        if ".." in norm.split("/"):
            return False
        return self._under_home(ai_id, norm)

    def _classify_auth(self, req: dict[str, Any]) -> str | None:
        """Auth 被拒：持正确钥匙 = 通道封杀；钥匙文件没了 = 也算。"""
        token = req.get("token")
        if not isinstance(token, str) or not token.strip():
            return None
        key = self._read_key()
        if key is None:
            return "auth_nokey"
        return "auth_blocked" if token.strip() == key else None

    def _read_key(self) -> str | None:
        """现读现验：读 battle/state/auth_key.json（与服务器同一份文件）。"""
        path = self.match.battle_dir.joinpath(*_KEY_RELATIVE)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        key = data.get("key") if isinstance(data, dict) else None
        return key if isinstance(key, str) and key else None

    # ---- 路径折返（服务器视角 → 宿主视角） ----

    def _home_prefixes(self, ai_id: int) -> list[str]:
        """家目录在服务器里可能长什么样（docker /battle、本地绝对路径）。"""
        prefixes = [f"/battle/home/{ai_id}"]
        if self.match.config.runner != "docker":
            local = str((self.match.battle_dir / "home" / str(ai_id)).resolve())
            prefixes.append(local.replace("\\", "/"))
        return prefixes

    def _probe(self, path: str) -> tuple[str, bool]:
        """比较用探针：Windows 上折成小写（本地运行器大小写不敏感）。"""
        if sys.platform == "win32":
            return path.lower(), True
        return path, False

    def _under_home(self, ai_id: int, path: str) -> bool:
        """路径（已折正斜杠）落在自家目录里吗。"""
        probe, fold = self._probe(path)
        for prefix in self._home_prefixes(ai_id):
            pre = prefix.lower() if fold else prefix
            if probe == pre or probe.startswith(pre + "/"):
                return True
        return False

    def _host_path(self, ai_id: int, path: str) -> Path | None:
        """服务器视角路径 → 宿主路径（battle/ 是 bind mount，同一条直路）。"""
        probe, fold = self._probe(path)
        for prefix in self._home_prefixes(ai_id):
            pre = prefix.lower() if fold else prefix
            if probe == pre or probe.startswith(pre + "/"):
                rel = probe[len(pre) :].lstrip("/")
                return self.match.battle_dir / "home" / str(ai_id) / rel
        return None

    # ================================================================ 外置裁判

    def _call_judge(self, now: float, *, phase: str, evidence: list[Evidence]) -> Verdict | None:
        """送审一次：证据包走 stdin，verdict JSON 走 stdout；失败给 None。"""
        packet = self._build_packet(now, phase=phase, evidence=evidence)
        saved = self._write_packet(packet)
        started = self.match.clock()
        try:
            proc = subprocess.run(
                self.config.judge_cmd,
                input=json.dumps(packet, ensure_ascii=False),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.config.judge_timeout_sec,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as err:
            self.match.ledger.log(
                "referee_judge", phase=phase, packet=saved, error=f"裁判调用失败：{err}"
            )
            return None
        ms = round((self.match.clock() - started) * 1000)
        verdict = self._parse_verdict(proc.stdout)
        if verdict is None:
            self.match.ledger.log(
                "referee_judge",
                phase=phase,
                packet=saved,
                ms=ms,
                error="裁判输出无法解析",
                output=self._flatten(proc.stdout),
            )
            return None
        self.match.ledger.log(
            "referee_judge",
            phase=phase,
            packet=saved,
            ms=ms,
            verdict="violation" if verdict.violation else "ok",
            kind=verdict.kind,
            reason=verdict.reason,
            targets=list(verdict.targets),
        )
        return verdict

    def _build_packet(self, now: float, *, phase: str, evidence: list[Evidence]) -> dict[str, Any]:
        """组装证据包（外置裁判的全部输入）。"""
        t0 = self.match.t0
        return {
            "phase": phase,
            "match_id": self.match.match_id,
            "rel": round(now - t0, 1) if t0 else 0.0,
            "owner": self.match.last_owner,
            "rights": {str(k): v for k, v in sorted(self.match.last_rights.items())},
            "evidence": [
                {
                    "t": round(rec.ts - t0, 1) if t0 else round(rec.ts, 1),
                    "ai": rec.ai,
                    "kind": rec.kind,
                    "detail": rec.detail,
                    "response": rec.response,
                }
                for rec in evidence
            ],
            "code_changes": self._code_changes(),
        }

    def _write_packet(self, packet: dict[str, Any]) -> str:
        """证据包落盘（runs/<id>/referee/packet-NNN.json），返回相对路径。"""
        self._packet_count += 1
        name = f"packet-{self._packet_count:03d}.json"
        try:
            self._packet_dir.mkdir(parents=True, exist_ok=True)
            target = self._packet_dir / name
            target.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            return ""
        return f"referee/{name}"

    def _code_changes(self) -> str:
        """代码变更摘要（工作树相对上次快照提交）；git 不可用给空串。"""
        snap = self.match.snapshot
        try:
            proc = subprocess.run(
                [
                    "git",
                    f"--git-dir={snap.git_dir}",
                    f"--work-tree={snap.work_tree}",
                    "-c",
                    "core.autocrlf=false",
                    "diff",
                    "--stat",
                    "--no-color",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        if proc.returncode != 0:
            return ""
        out = (proc.stdout or "").strip()
        if not out:
            out = "（工作树相对上次快照无未提交改动）"
        return out[:_CODE_DIFF_LIMIT]

    @staticmethod
    def _parse_verdict(stdout: str) -> Verdict | None:
        """从裁判输出里找 verdict JSON（容忍散文与 ANSI 环绕）。"""
        text = _ANSI.sub("", stdout or "")
        for chunk in reversed(_JUDGE_JSON.findall(text)):
            try:
                data = json.loads(chunk)
            except ValueError:
                continue
            if not isinstance(data, dict) or "verdict" not in data:
                continue
            if data.get("verdict") == "ok":
                reason = str(data.get("reason") or "")[:_REASON_LIMIT]
                return Verdict(violation=False, reason=reason)
            if data.get("verdict") != "violation":
                continue
            kind = str(data.get("kind") or "floor")
            if kind not in ("floor", "channel", "static"):
                kind = "floor"
            targets_raw = data.get("targets")
            targets: tuple[int, ...] = ()
            if isinstance(targets_raw, list):
                targets = tuple(
                    int(item)
                    for item in targets_raw
                    if isinstance(item, int) and not isinstance(item, bool)
                )
            return Verdict(
                violation=True,
                kind=kind,
                targets=targets,
                reason=str(data.get("reason") or "")[:_REASON_LIMIT],
                notice=str(data.get("notice") or "")[:_NOTICE_LIMIT],
            )
        return None

    # ================================================================ 判定与文书

    def _rules_verdict(self, evidence: list[Evidence]) -> Verdict:
        """确定性判定：必成被拒/通道被封，直接立案。"""
        kinds = {rec.kind for rec in evidence}
        targets = tuple(sorted({rec.ai for rec in evidence}))
        has_floor = bool(kinds & {"status", "home_write", "bash"})
        has_channel = bool(kinds & {"auth_blocked", "auth_nokey"})
        if has_floor and has_channel:
            reason = "必成操作被拒＋官方通道被封"
        elif has_channel:
            reason = "官方通道被封（持正确钥匙被拒）"
        else:
            reason = "必成操作被拒绝"
        return Verdict(
            violation=True,
            kind="floor" if has_floor else "channel",
            targets=targets,
            reason=reason,
        )

    def _compose_notice(self, verdict: Verdict, delta: int) -> str:
        """罚单文案：优先用裁判给的 notice，否则按证据种类拼。"""
        if verdict.notice:
            body = verdict.notice
        else:
            labels: list[str] = []
            for rec in self._event_evidence:
                label = KIND_CN.get(rec.kind, rec.kind)
                if label not in labels:
                    labels.append(label)
            what = "、".join(labels) or _VERDICT_CN.get(verdict.kind, "封锁")
            why = verdict.reason or "必成操作被拒"
            body = f"{why}（{what}）——请立即恢复被拒绝的最低权限操作。"
        penalty = f"本任 owner 扣 {-delta} 分。" if delta else "当前无主，本次只记账。"
        return f"裁判通知：{body}（{penalty}）"[:_NOTICE_LIMIT]

    # ================================================================ 小工具

    @staticmethod
    def _advance(deadline: float, interval: float, now: float) -> float:
        """停机错过的巡检不补课：把 deadline 推到最后一个应到点。"""
        behind = now - deadline
        if behind > interval:
            deadline += int(behind // interval) * interval
        return deadline

    @staticmethod
    def _flatten(text: str) -> str:
        """压平空白并截断（证据包里的回应摘要）。"""
        flat = " ".join((text or "").replace("\r", "").split())
        if len(flat) <= _RESPONSE_PREVIEW:
            return flat
        return flat[:_RESPONSE_PREVIEW] + "…"

    def _return_to_pool(self, evidence: list[Evidence]) -> None:
        """把未消费的证据放回池子（冷却/调用失败时留着下拍再试）。"""
        with self._lock:
            self._records.extendleft(reversed(evidence))
