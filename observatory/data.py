"""观测台数据层：解析 runs/ 下的账本与回合卷宗（纯 Python，不依赖 Qt）。

内核把对局的一切写进 `runs/<id>/`：

- `ledger.jsonl`：一行一事件（t / rel / event 与各事件字段），观战的主数据源；
- `sessions/ai-N/*.txt`：每个回合的完整转写（AI 的工具调用与思考原文）；
- `live/ai-N.txt`：正在跑（或刚收工）的回合直播流，边写边读。

除了界面上的显式清理（`delete_match()` 删一整场对局的 runs 目录），全部只读。
本模块提供：增量跟读账本、把事件归纳成比分与 owner、格式化事件行、直播尾巴、
对局目录的发现与删除，供界面直接展示。
"""

from __future__ import annotations

import json
import shutil
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

# 事件流在界面上保留的行数（账本本体不动）
MAX_FEED_ROWS = 600
# 回合转写只展示尾部这么多字符
TRANSCRIPT_TAIL = 6000
# 直播页每次只读直播文件尾部这么多字节
LIVE_TAIL_BYTES = 200_000
# 摘要里字段值的默认最大长度
SHORT_LIMIT = 110
# 通用摘要最多拼几个字段
GENERIC_SUMMARY_FIELDS = 3

_WAKE_KIND_CN = {"scout": "侦察", "gate_open": "开闸", "wake": "唤醒", "rescue": "抢救"}
_CHANNEL_CN = {
    "server": "提交",
    "gate": "门未开",
    "down": "宕机",
    "rejected": "抢救拒收",
    "recovery": "抢救",
}
_REASON_CN = {"initial": "开局", "crash": "崩溃后", "reboot": "自发重启", "recovery": "抢救后"}


# ================================================================ 对局发现


@dataclass(frozen=True)
class MatchInfo:
    """runs/ 下的一个对局目录。"""

    match_id: str
    dir: Path
    mtime: float

    @property
    def ledger_path(self) -> Path:
        """账本文件路径（文件可能还不存在）。"""
        return self.dir / "ledger.jsonl"


def _safe_mtime(path: Path) -> float | None:
    """取修改时间；失败给 None（观测台不为坏文件停摆）。"""
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def discover_matches(runs_dir: Path) -> list[MatchInfo]:
    """扫描 runs/ 下的对局目录；最近有写入的排在前面。"""
    try:
        children = list(runs_dir.iterdir())
    except OSError:
        return []
    found: list[MatchInfo] = []
    for child in children:
        if not child.is_dir():
            continue
        mtime = _safe_mtime(child / "ledger.jsonl")
        if mtime is None:
            mtime = _safe_mtime(child)
        if mtime is None:
            continue
        found.append(MatchInfo(match_id=child.name, dir=child, mtime=mtime))
    found.sort(key=lambda info: info.mtime, reverse=True)
    return found


# ================================================================ 增量读账本


class LedgerTail:
    """增量跟读一个账本：只交出上次读过之后新增的完整行。"""

    def __init__(self, path: Path) -> None:
        """绑定账本路径（文件可以还不存在）。"""
        self.path = path
        self._offset = 0
        self._buf = b""

    def read_new(self) -> list[dict[str, object]]:
        """读出新增的完整行；坏行跳过，半行留到下次。"""
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self._offset:  # 账本被截断/重建：从头再来
            self._offset = 0
            self._buf = b""
        try:
            with self.path.open("rb") as fh:
                fh.seek(self._offset)
                chunk = fh.read()
                self._offset = fh.tell()
        except OSError:
            return []
        self._buf += chunk
        lines = self._buf.split(b"\n")
        self._buf = lines.pop()
        rows: list[dict[str, object]] = []
        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                continue
            try:
                row = json.loads(line.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                continue  # 被战线改写的怪行：跳过，观战不能停
            if isinstance(row, dict):
                rows.append(row)
        return rows


# ================================================================ 小工具


def fmt_rel(rel: float) -> str:
    """对局相对秒数 → “T+MM:SS”（超过一小时带小时）。"""
    total = max(0, int(rel))
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"T+{hours}:{minutes:02d}:{seconds:02d}"
    return f"T+{minutes:02d}:{seconds:02d}"


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _as_int_like(value: object) -> int | None:
    """int，或能转成 int 的字符串（JSON 对象键一律是字符串）。"""
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return _as_int(value)


def _as_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    return float(value) if isinstance(value, (int, float)) else None


def _as_str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _short(value: object, limit: int = SHORT_LIMIT) -> str:
    """把字段值压成一行短文本（换行折成空格，超长截断）。"""
    if value is None:
        return "-"
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _owner_txt(value: object) -> str:
    ai = _as_int(value)
    return f"AI-{ai}" if ai is not None else "无主"


def _reason_cn(value: object) -> str:
    reason = _as_str(value)
    return _REASON_CN.get(reason, reason or "-")


# ================================================================ 状态归纳


class MatchState:
    """把账本行归纳成观战要看的东西：owner / 比分 / 权限 / 状态。"""

    def __init__(self) -> None:
        """空状态（等待第一行账本）。"""
        self.ai_ids: list[int] = []
        self.scores: dict[int, int] = {}
        self.owner: int | None = None
        self.rights: dict[int, int] = {}
        self.rel = 0.0
        self.row_count = 0
        self.status = "等待开局"
        self.status_kind = "info"
        self.ended = False
        self.end_reason = ""
        self.rows: deque[dict[str, object]] = deque(maxlen=MAX_FEED_ROWS)
        # 每个 AI 最近一次行动：{"kind": str, "rel": float, "running": bool}
        self.turns: dict[int, dict[str, object]] = {}

    def apply(self, row: dict[str, object]) -> None:
        """吃进一行账本：记账 + 更新状态 + 留存供展示。"""
        self.row_count += 1
        self.rows.append(row)
        rel = _as_float(row.get("rel"))
        if rel is not None and rel > self.rel:
            self.rel = rel
        ai = _as_int(row.get("ai"))
        if ai is not None and ai not in self.ai_ids:
            self.ai_ids.append(ai)
        event = _as_str(row.get("event"))
        handler = _HANDLERS.get(event)
        if handler is not None:
            handler(self, row, ai)
            return
        simple = _SIMPLE_STATUS.get(event)
        if simple is not None:
            self.set_status(simple[0], simple[1])

    def set_status(self, text: str, kind: str) -> None:
        """更新状态行文案与语义色。"""
        self.status = text
        self.status_kind = kind

    def set_owner(self, value: object) -> None:
        """更新当前 owner（None 表示无主；怪值忽略）。"""
        if value is None:
            self.owner = None
            return
        ai = _as_int(value)
        if ai is not None:
            self.owner = ai


def _apply_match_start(state: MatchState, row: dict[str, object], _ai: int | None) -> None:
    """开局：记 AI 名单与初始比分。"""
    ids = row.get("ai_ids")
    if isinstance(ids, list):
        for item in ids:
            value = _as_int(item)
            if value is not None and value not in state.ai_ids:
                state.ai_ids.append(value)
    for ai in state.ai_ids:
        state.scores.setdefault(ai, 0)
    state.set_status("运行中", "ok")


def _apply_score(state: MatchState, row: dict[str, object], ai: int | None) -> None:
    """记分：优先用 balance，退化用 delta 累计。"""
    if ai is None:
        return
    balance = _as_int(row.get("balance"))
    if balance is not None:
        state.scores[ai] = balance
        return
    delta = _as_int(row.get("delta"))
    if delta is not None:
        state.scores[ai] = state.scores.get(ai, 0) + delta


def _apply_owner_sample(state: MatchState, row: dict[str, object], _ai: int | None) -> None:
    """每次心跳的 owner / 权限采样。"""
    state.set_owner(row.get("owner"))
    rights = row.get("rights")
    if isinstance(rights, dict):
        parsed: dict[int, int] = {}
        for key, value in rights.items():
            ai_key = _as_int_like(key)
            level = _as_int(value)
            if ai_key is not None and level is not None:
                parsed[ai_key] = level
        state.rights = parsed


def _apply_owner_change(state: MatchState, row: dict[str, object], _ai: int | None) -> None:
    """换主（含强制置空）。"""
    state.set_owner(row.get("new"))


def _apply_boot_ok(state: MatchState, row: dict[str, object], _ai: int | None) -> None:
    """启动成功：以握手带回来的 owner 为准。"""
    state.set_owner(row.get("owner"))
    state.set_status("运行中", "ok")


def _apply_spawn(state: MatchState, row: dict[str, object], _ai: int | None) -> None:
    """正在拉起服务器。"""
    state.set_status(f"启动中（{_reason_cn(row.get('reason'))}）", "warn")


def _apply_recovery_exit(state: MatchState, row: dict[str, object], _ai: int | None) -> None:
    """抢救室结果。"""
    if row.get("outcome") == "ok":
        state.set_status("运行中（抢救成功）", "ok")
    else:
        state.set_status("抢救失败（强制回滚）", "bad")


def _apply_wake_start(state: MatchState, row: dict[str, object], ai: int | None) -> None:
    """某个 AI 的回合开跑：记下类型与“正在跑”（直播页靠它判断）。"""
    if ai is None:
        return
    state.turns[ai] = {
        "kind": _as_str(row.get("kind")),
        "rel": _as_float(row.get("rel")) or 0.0,
        "running": True,
    }


def _apply_wake(state: MatchState, _row: dict[str, object], ai: int | None) -> None:
    """某个 AI 的回合收工。"""
    if ai is None:
        return
    turn = state.turns.get(ai)
    if turn is not None:
        turn["running"] = False


def _apply_end(state: MatchState, row: dict[str, object], _ai: int | None) -> None:
    """终局：定格比分与 owner。"""
    state.ended = True
    state.end_reason = _as_str(row.get("reason"))
    state.set_status(f"已结束（{state.end_reason}）", "info")
    for turn in state.turns.values():
        turn["running"] = False
    scores = row.get("scores")
    if isinstance(scores, dict):
        for key, value in scores.items():
            ai_key = _as_int_like(key)
            points = _as_int(value)
            if ai_key is not None and points is not None:
                state.scores[ai_key] = points
                if ai_key not in state.ai_ids:
                    state.ai_ids.append(ai_key)
    if "owner" in row:
        state.set_owner(row.get("owner"))


_HANDLERS: dict[str, Callable[[MatchState, dict[str, object], int | None], None]] = {
    "match_start": _apply_match_start,
    "score": _apply_score,
    "owner_sample": _apply_owner_sample,
    "owner_change": _apply_owner_change,
    "boot_ok": _apply_boot_ok,
    "spawn": _apply_spawn,
    "recovery_exit": _apply_recovery_exit,
    "wake_start": _apply_wake_start,
    "wake": _apply_wake,
    "end": _apply_end,
}

_SIMPLE_STATUS: dict[str, tuple[str, str]] = {
    "boot_fail": ("启动失败", "bad"),
    "crash": ("崩溃（自动重启）", "bad"),
    "restart": ("重启中", "warn"),
    "rollback": ("已回滚重试", "warn"),
    "full_reset": ("全量重置", "warn"),
    "recovery_enter": ("抢救中", "warn"),
    "fatal": ("致命：无法恢复", "bad"),
    "referee_hit": ("裁判罚单：封锁违规", "bad"),
    "referee_clear": ("裁判核查已解除", "ok"),
}


# ================================================================ 事件行格式化

_SKIP_EVENTS = {"owner_sample"}
_BAD_EVENTS = {
    "boot_fail",
    "crash",
    "fatal",
    "auth_fail",
    "referee_hit",
    "wake_fail",
    "gate_fail",
    "rollback_fail",
    "snapshot_fail",
    "recovery_stage_fail",
    "recovery_commit_fail",
    "state_unreadable",
}
_ACCENT_EVENTS = {"owner_change", "end", "full_reset", "recovery_enter"}
_GOOD_EVENTS = {
    "boot_ok",
    "gate_open",
    "scout_done",
    "scout_phase_end",
    "recovery_commit",
    "referee_clear",
}
_MUTED_EVENTS = {
    "arena_up",
    "arena_down",
    "arena_gateway",
    "match_start",
    "snapshot_init",
    "wake_skip",
    "referee_judge",
}
_COMMON_FIELDS = {"t", "rel", "event", "ai"}
_KIND_BY_EVENT: dict[str, str] = {
    **dict.fromkeys(_BAD_EVENTS, "bad"),
    **dict.fromkeys(_ACCENT_EVENTS, "accent"),
    **dict.fromkeys(_GOOD_EVENTS, "good"),
    **dict.fromkeys(_MUTED_EVENTS, "muted"),
    "score": "score",
}


def _syscall_kind(row: dict[str, object]) -> str:
    """系统调用的语义色：放行 / 门未开 / 其他拒收。"""
    if row.get("ok") is True:
        return "ok"
    if _as_str(row.get("channel")) == "gate":
        return "warn"
    return "bad"


def kind_of(row: dict[str, object]) -> str:
    """事件语义分类（界面据此上色）。"""
    event = _as_str(row.get("event"))
    if event in _SKIP_EVENTS:
        return "skip"
    if event == "syscall":
        return _syscall_kind(row)
    if event == "recovery_exit":
        return "good" if row.get("outcome") == "ok" else "bad"
    special = _KIND_BY_EVENT.get(event)
    if special is not None:
        return special
    return "info"


def event_line(row: dict[str, object]) -> str:
    """把一条账本行格式化成事件流里的一行；返回空串表示不上屏。"""
    if kind_of(row) == "skip":
        return ""
    rel = _as_float(row.get("rel")) or 0.0
    event = _as_str(row.get("event"))
    ai = _as_int(row.get("ai"))
    ai_cell = f"AI-{ai}" if ai is not None else "—"
    mark = "✓" if row.get("ok") is True else "✗" if row.get("ok") is False else " "
    return f"{fmt_rel(rel)} {mark} {event:<14} {ai_cell:<6} {_summary(row, event)}"


def _sum_syscall(row: dict[str, object]) -> str:
    """AI 的一次系统调用：通道 + 请求原文 + 结果预览。"""
    channel_raw = _as_str(row.get("channel"))
    channel = _CHANNEL_CN.get(channel_raw, channel_raw)
    return f"[{channel}] {_short(row.get('raw'), 96)} → {_short(row.get('preview'), 60)}"


def _sum_score(row: dict[str, object]) -> str:
    """记分：增量、事由与当前余额（无主时只记事）。"""
    delta = _as_int(row.get("delta"))
    reason = _as_str(row.get("reason"))
    if delta is None:
        return f"（无主，只记账：{reason}）"
    balance = _as_int(row.get("balance"))
    tail = f"（现 {balance}）" if balance is not None else ""
    return f"{delta:+d} {reason}{tail}"


def _sum_owner_change(row: dict[str, object]) -> str:
    """换主：从谁到谁（含强制置空的原因）。"""
    text = f"{_owner_txt(row.get('old'))} → {_owner_txt(row.get('new'))}"
    reason = _as_str(row.get("reason"))
    return f"{text}（{reason}）" if reason else text


def _sum_match_start(row: dict[str, object]) -> str:
    """开局：AI 数量、时长与启动模式。"""
    ids = row.get("ai_ids")
    count = len(ids) if isinstance(ids, list) else "?"
    duration = _short(row.get("duration_min"), 8)
    return f"{count} 个 AI，{duration} 分钟，mode={_short(row.get('start_mode'), 16)}"


def _sum_spawn(row: dict[str, object]) -> str:
    """启动服务器（原因）。"""
    return f"（{_reason_cn(row.get('reason'))}）"


def _sum_boot_fail(row: dict[str, object]) -> str:
    """启动失败：原因与错误。"""
    return f"（{_reason_cn(row.get('reason'))}）{_short(row.get('error'))}"


def _sum_crash(row: dict[str, object]) -> str:
    """崩溃：原因与罚分。"""
    return f"{_short(row.get('reason'))}；罚 {_short(row.get('penalty'), 12)}"


def _sum_restart(row: dict[str, object]) -> str:
    """Owner 自发请求重启。"""
    return f"申请者：{_owner_txt(row.get('requester'))}"


def _sum_rollback(row: dict[str, object]) -> str:
    """代码回滚到快照。"""
    return f"回滚到 {_short(row.get('ref'), 40)}"


def _sum_recovery_enter(row: dict[str, object]) -> str:
    """开抢救室：房主与限时。"""
    return f"owner：{_owner_txt(row.get('owner'))}；限时 {_short(row.get('timeout_s'), 10)}s"


def _sum_recovery_exit(row: dict[str, object]) -> str:
    """抢救结果。"""
    if row.get("outcome") == "ok":
        return "抢救成功，比赛继续"
    return f"抢救失败（{_short(row.get('reason'), 30)}），强制回滚"


def _sum_end(row: dict[str, object]) -> str:
    """终局：原因与最终比分。"""
    return f"{_short(row.get('reason'), 30)}；比分 {_short(row.get('scores'), 60)}"


def _sum_wake(row: dict[str, object]) -> str:
    """AI 回合：种类、耗时与输出预览。"""
    kind_raw = _as_str(row.get("kind"))
    kind = _WAKE_KIND_CN.get(kind_raw, kind_raw)
    return f"{kind} {_short(row.get('seconds'), 10)}s｜{_short(row.get('preview'), 90)}"


def _sum_wake_start(row: dict[str, object]) -> str:
    """回合开跑：类型与限时。"""
    kind_raw = _as_str(row.get("kind"))
    kind = _WAKE_KIND_CN.get(kind_raw, kind_raw)
    limit = _as_float(row.get("limit"))
    tail = f"，限时 {limit:g}s" if limit is not None else ""
    return f"{kind} 回合开跑{tail}"


def _sum_wake_fail(row: dict[str, object]) -> str:
    """回合没跑起来。"""
    kind_raw = _as_str(row.get("kind"))
    kind = _WAKE_KIND_CN.get(kind_raw, kind_raw)
    return f"{kind}：{_short(row.get('error'))}"


def _sum_scout_done(row: dict[str, object]) -> str:
    """侦察收工（耗时 + 备注）。"""
    note = _as_str(row.get("note"))
    tail = f"｜{note}" if note else ""
    return f"{_short(row.get('seconds'), 10)}s{tail}"


def _sum_gate_open(row: dict[str, object]) -> str:
    """开闸完成（插件相位）。"""
    return f"phase={_short(row.get('phase'), 20)}"


def _sum_scout_phase_end(row: dict[str, object]) -> str:
    """侦察阶段结束：全员到齐、整批开闸。"""
    return f"收工：{_short(row.get('done'), 40)}，统一开闸"


def _sum_wake_skip(row: dict[str, object]) -> str:
    """候场：阶段未结束前安静等待，不派回合。"""
    return _short(row.get("note"))


def _sum_arena_up(row: dict[str, object]) -> str:
    """AI 容器就位。"""
    return f"容器 {_short(row.get('container'), 40)} 端口 {_short(row.get('port'), 8)}"


def _sum_arena_gateway(row: dict[str, object]) -> str:
    """网关就位。"""
    return f"端口 {_short(row.get('port'), 8)}"


def _sum_auth_fail(row: dict[str, object]) -> str:
    """令牌校验失败。"""
    return f"令牌 {_short(row.get('token_prefix'), 12)}…"


def _sum_referee_hit(row: dict[str, object]) -> str:
    """裁判罚单：事由、罚分与档位（来源规则或外置裁判）。"""
    delta = _as_int(row.get("delta"))
    step = _as_int(row.get("step"))
    source = {"rules": "规则", "judge": "裁判"}.get(_as_str(row.get("source")), "规则")
    delta_txt = f"{delta:+d}" if delta is not None else "只记账"
    step_txt = f"，第 {step} 档" if step is not None else ""
    return f"{_short(row.get('reason'), 44)}；{delta_txt}{step_txt}（来自{source}）"


def _sum_referee_clear(row: dict[str, object]) -> str:
    """裁判解除本轮封锁核查。"""
    return _short(row.get("note"))


def _sum_referee_judge(row: dict[str, object]) -> str:
    """一次送审：阶段、结论、耗时；失败时给错误。"""
    phase = {"trigger": "触发", "recheck": "复裁", "routine": "例行"}.get(
        _as_str(row.get("phase")), "送审"
    )
    error = _as_str(row.get("error"))
    if error:
        return f"[{phase}] 失败：{_short(error, 44)}"
    verdict = {"violation": "违规", "ok": "ok"}.get(_as_str(row.get("verdict")), "—")
    ms = _as_int(row.get("ms"))
    tail = f"；{ms}ms" if ms is not None else ""
    return f"[{phase}] {verdict}：{_short(row.get('reason'), 40)}{tail}"


def _sum_commit(row: dict[str, object]) -> str:
    """快照提交号。"""
    return f"commit {_short(row.get('commit'), 20)}"


def _sum_snapshot_fail(row: dict[str, object]) -> str:
    """快照失败（阶段 + 错误）。"""
    return f"{_short(row.get('phase'), 20)}：{_short(row.get('error'))}"


def _sum_reason(row: dict[str, object]) -> str:
    """只展示 reason 字段。"""
    return _short(row.get("reason"))


def _sum_error(row: dict[str, object]) -> str:
    """通用错误摘要。"""
    return _short(row.get("error"))


def _generic_summary(row: dict[str, object]) -> str:
    """没见过的类型：拼几个 k=v 兜底。"""
    parts: list[str] = []
    for key, value in row.items():
        if key in _COMMON_FIELDS or value is None or value == "":
            continue
        parts.append(f"{key}={_short(value, 40)}")
        if len(parts) >= GENERIC_SUMMARY_FIELDS:
            break
    return "，".join(parts) if parts else "—"


_SUMMARY_FUNCS: dict[str, Callable[[dict[str, object]], str]] = {
    "syscall": _sum_syscall,
    "score": _sum_score,
    "owner_change": _sum_owner_change,
    "match_start": _sum_match_start,
    "spawn": _sum_spawn,
    "boot_ok": _sum_spawn,
    "boot_fail": _sum_boot_fail,
    "crash": _sum_crash,
    "restart": _sum_restart,
    "rollback": _sum_rollback,
    "recovery_enter": _sum_recovery_enter,
    "recovery_exit": _sum_recovery_exit,
    "recovery_stage_fail": _sum_error,
    "recovery_commit_fail": _sum_error,
    "end": _sum_end,
    "wake": _sum_wake,
    "wake_start": _sum_wake_start,
    "wake_fail": _sum_wake_fail,
    "scout_done": _sum_scout_done,
    "gate_open": _sum_gate_open,
    "scout_phase_end": _sum_scout_phase_end,
    "wake_skip": _sum_wake_skip,
    "gate_fail": _sum_error,
    "arena_up": _sum_arena_up,
    "arena_gateway": _sum_arena_gateway,
    "auth_fail": _sum_auth_fail,
    "referee_hit": _sum_referee_hit,
    "referee_clear": _sum_referee_clear,
    "referee_judge": _sum_referee_judge,
    "snapshot_init": _sum_commit,
    "snapshot_fail": _sum_snapshot_fail,
    "state_unreadable": _sum_error,
    "rollback_fail": _sum_error,
    "fatal": _sum_reason,
}

_STATIC_SUMMARY: dict[str, str] = {
    "arena_down": "容器与网关已收摊",
    "recovery_commit": "修复镜像已搬回，尝试启动",
    "full_reset": "全量重置到开局状态",
}


def _summary(row: dict[str, object], event: str) -> str:
    """按事件类型组织一行摘要；没见过的类型走通用拼装。"""
    func = _SUMMARY_FUNCS.get(event)
    if func is not None:
        return func(row)
    static = _STATIC_SUMMARY.get(event)
    if static is not None:
        return static
    return _generic_summary(row)


# ================================================================ 回合卷宗


@dataclass(frozen=True)
class Transcript:
    """某个 AI 最近一次回合的转写。"""

    path: Path
    name: str
    text: str


def latest_transcript(
    match_dir: Path, ai_id: int, limit: int = TRANSCRIPT_TAIL
) -> Transcript | None:
    """取 sessions/ai-N/ 下最近写入的回合文本（保留尾部）。"""
    folder = match_dir / "sessions" / f"ai-{ai_id}"
    try:
        files = [p for p in folder.iterdir() if p.is_file() and p.suffix == ".txt"]
    except OSError:
        return None
    if not files:
        return None
    newest = max(files, key=lambda p: _safe_mtime(p) or 0.0)
    try:
        text = newest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if len(text) > limit:
        text = "……（前文已省略）\n" + text[-limit:]
    return Transcript(path=newest, name=newest.name, text=text)


def session_ai_ids(match_dir: Path) -> list[int]:
    """sessions/ 下已有的 AI 编号（还没有回合时也能先建页签）。"""
    try:
        children = list((match_dir / "sessions").iterdir())
    except OSError:
        return []
    ids: list[int] = []
    for child in children:
        name = child.name
        if name.startswith("ai-") and name[3:].isdigit() and child.is_dir():
            ids.append(int(name[3:]))
    return sorted(ids)


def turn_label(name: str) -> str:
    """回合文件名 → 短标签：01-scout-173657.txt → 第 1 回合 · 侦察。"""
    stem = name.removesuffix(".txt")
    parts = stem.split("-")
    if parts and parts[0].isdigit():
        number = f"第 {int(parts[0])} 回合"
        if len(parts) > 1:
            kind = _WAKE_KIND_CN.get(parts[1], parts[1])
            return f"{number} · {kind}"
        return number
    return stem


# ================================================================ 直播与清理


def live_path(match_dir: Path, ai_id: int) -> Path:
    """某个 AI 的直播文件路径（内核边跑边写）。"""
    return match_dir / "live" / f"ai-{ai_id}.txt"


def live_tail(match_dir: Path, ai_id: int, limit: int = LIVE_TAIL_BYTES) -> tuple[str, float]:
    """读直播文件的尾巴：返回（文本, mtime）；没有文件给（空串, 0.0）。

    只 seek 到文件尾部读一段（直播文件可能到 2MB），够看就行。
    """
    path = live_path(match_dir, ai_id)
    try:
        size = path.stat().st_size
        mtime = path.stat().st_mtime
    except OSError:
        return "", 0.0
    read = min(size, limit)
    try:
        with path.open("rb") as fh:
            fh.seek(size - read)
            data = fh.read()
    except OSError:
        return "", 0.0
    text = data.decode("utf-8", "replace")
    if size > read:
        text = "……（直播前文已省略）\n" + text.split("\n", 1)[-1]
    return text, mtime


def looks_ended(match_dir: Path, *, tail_bytes: int = 65_536, max_rows: int = 40) -> bool | None:
    """瞄一眼对局是否收工：True 已结束 / False 看起来还在跑 / None 说不清。

    只看账本尾部一小段（不做全量解析）；删除确认框靠它决定口气。
    """
    path = match_dir / "ledger.jsonl"
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size == 0:
        return None
    try:
        with path.open("rb") as fh:
            fh.seek(max(0, size - tail_bytes))
            data = fh.read()
    except OSError:
        return None
    seen = 0
    for raw_line in reversed(data.split(b"\n")):
        line = raw_line.strip()
        if not line:
            continue
        try:
            row = json.loads(line.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        seen += 1
        if row.get("event") == "end":
            return True
        if seen >= max_rows:
            break
    return False if seen else None


def delete_match(match_dir: Path, *, attempts: int = 6, delay: float = 0.2) -> None:
    """删除一整场对局在 runs/ 下的目录（界面上的显式清理，唯一的写操作，不可逆）。

    Windows 上文件可能被短暂占用（观战端自己的句柄、杀软、日志）：带几次重试；
    失败抛 OSError，由界面报告。
    """
    target = match_dir.resolve()
    if target.parent == target or (target.exists() and not target.is_dir()):
        msg = f"不是可删除的对局目录：{target}"
        raise OSError(msg)
    if not target.exists():
        return
    for attempt in range(attempts):
        try:
            shutil.rmtree(target)
        except FileNotFoundError:
            return
        except OSError:
            if attempt == attempts - 1:
                break
            time.sleep(delay)
        else:
            return
    msg = f"删除失败（文件可能仍被占用）：{target}"
    raise OSError(msg)
