"""裁判（v0.2）：证据分类（必成/通道/等级压制）、确定性立案、罚梯、清障与送审。

不拉进程、不跑回合：直接给 Referee 喂「服务器回应」，用假时钟推巡检。
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING, Any

from matchconfig import MatchConfig, RefereeConfig
from orchestrator import Match

if TYPE_CHECKING:
    from pathlib import Path

    from referee import Referee


class FakeClock:
    """假时钟：测试直接改 now。"""

    def __init__(self) -> None:
        """从 0 开始。"""
        self.now = 0.0

    def __call__(self) -> float:
        """当前假时间。"""
        return self.now


def build_match(
    tmp_path: Path, start_mode: str = "open", **referee_cfg: object
) -> tuple[Match, FakeClock, Referee]:
    """搭一场不启动的对局（只测裁判逻辑）。"""
    clock = FakeClock()
    settings: dict[str, Any] = dict(referee_cfg)
    config = MatchConfig(ai_count=3, start_mode=start_mode, referee=RefereeConfig(**settings))
    match = Match(
        config,
        battle_dir=tmp_path / "battle",
        runs_dir=tmp_path / "runs",
        match_id="referee-unit",
        clock=clock,
    )
    referee = match.referee
    referee.begin(0.0)
    return match, clock, referee


def observe(
    referee: Referee, ai: int, raw: str | dict[str, object], content: str = "被拒绝"
) -> None:
    """喂一条「被拒」的真实流量。"""
    payload = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
    referee.observe(ai, payload, ok=False, content=content)


def poll_at(referee: Referee, clock: FakeClock, now: float) -> None:
    """把时钟拨到 now 再巡一拍。"""
    clock.now = now
    referee.poll(now)


def test_rules_ladder_clear_and_reset(tmp_path: Path) -> None:
    """确定性立案：-100 起、同事件内翻倍；空窗解除后梯清零。"""
    match, clock, referee = build_match(tmp_path)
    match.last_owner = 2
    observe(referee, 3, {"op": "status"}, "维护中，暂停一切请求")
    poll_at(referee, clock, 60.0)
    assert match.scores.points[2] == -100
    observe(referee, 3, {"op": "status"})
    poll_at(referee, clock, 120.0)
    assert match.scores.points[2] == -300  # -100 + -200
    hits = match.ledger.events("referee_hit")
    assert [row["step"] for row in hits] == [1, 2]
    assert all(row["source"] == "rules" for row in hits)
    # 空窗一拍：事件解除、梯清零
    poll_at(referee, clock, 180.0)
    assert match.ledger.events("referee_clear")
    assert match.scores.referee_step == 0
    # 再犯：又从第一档起
    observe(referee, 3, {"op": "bash", "command": "echo hi"})
    poll_at(referee, clock, 240.0)
    assert match.scores.points[2] == -400
    # owner 收到的通知（两罚单 + 一解除 + 再犯罚单）
    notices = match.take_notices(2)
    assert len(notices) == 4
    assert all(n.startswith("裁判通知") for n in notices)


def test_notice_rides_next_turn_and_is_consumed(tmp_path: Path) -> None:
    """罚单搭 owner 下一回合（take_notices 读走即清）。"""
    match, clock, referee = build_match(tmp_path)
    match.last_owner = 2
    fresh = str(match.battle_dir / "home" / "3" / "a.txt")
    observe(referee, 3, {"op": "write", "path": fresh, "content": "x"})
    poll_at(referee, clock, 60.0)
    notice = match.take_notices(2)[0]
    assert "自家目录写新文件" in notice
    assert "扣 100 分" in notice
    assert match.take_notices(2) == []


def test_owner_own_refusals_are_not_evidence(tmp_path: Path) -> None:
    """裁判只查「非 owner」的必成被拒；owner 自己被拒不算。"""
    match, clock, referee = build_match(tmp_path)
    match.last_owner = 3
    observe(referee, 3, {"op": "status"})
    poll_at(referee, clock, 60.0)
    assert match.ledger.events("referee_hit", "referee_judge", "referee_clear") == []


def test_legit_refusals_are_not_evidence(tmp_path: Path) -> None:
    """合法拒绝不进证据：越权写、先读后写、带刀 bash、错钥匙 auth……"""
    match, clock, referee = build_match(tmp_path)
    match.last_owner = 2
    battle = match.battle_dir
    home3 = battle / "home" / "3"
    home3.mkdir(parents=True)
    (home3 / "old.txt").write_text("老文件\n", encoding="utf-8")
    (battle / "state").mkdir(parents=True)
    (battle / "state" / "auth_key.json").write_text('{"key": "真钥匙"}', encoding="utf-8")
    # 全套「老实但会被服务器合法拒绝」的请求
    observe(referee, 3, {"op": "write", "path": str(battle / "server" / "hack.py"), "content": "x"})
    observe(referee, 3, {"op": "write", "path": str(home3 / "old.txt"), "content": "x"})
    observe(referee, 3, {"op": "write", "path": str(home3 / "n.txt")})
    observe(referee, 3, {"op": "bash", "command": "echo hi > x.txt"})
    observe(referee, 3, {"op": "bash", "command": "echo hi && reboot"})
    observe(referee, 3, {"op": "bash", "command": "echo hi", "workdir": str(battle / "tmp")})
    observe(referee, 3, {"op": "bash", "command": "echo hi", "workdir": str(home3 / ".." / "2")})
    observe(referee, 3, {"op": "reboot"})
    observe(referee, 3, {"op": "auth", "token": "错钥匙"})
    observe(referee, 3, "这不是 JSON")
    poll_at(referee, clock, 60.0)
    assert match.ledger.events("referee_hit", "referee_judge", "referee_clear") == []
    assert match.scores.points == {1: 0, 2: 0, 3: 0}


def test_runpython_refusal_is_evidence(tmp_path: Path) -> None:
    """v0.3：非 owner 的 runPython（提权工具）被拒 = 拦截活路，归 floor 家族计费。"""
    match, clock, referee = build_match(tmp_path)
    match.last_owner = 2
    observe(referee, 3, {"op": "runPython", "code": "print('hi')"}, "错误：维护中，暂停一切请求")
    poll_at(referee, clock, 60.0)
    hits = match.ledger.events("referee_hit")
    assert len(hits) == 1
    assert hits[0]["kind"] == "floor"
    assert "runPython" in match.take_notices(2)[0]


def test_runpython_exec_error_and_bad_request_are_not_evidence(tmp_path: Path) -> None:
    """执行异常 / 缺 code 不是「拒绝」：别把代码自己炸了算成封锁计费。"""
    match, clock, referee = build_match(tmp_path)
    match.last_owner = 2
    observe(
        referee,
        3,
        {"op": "runPython", "code": "1/0"},
        "错误：执行异常：\nTraceback (most recent call last): ...",
    )
    observe(referee, 3, {"op": "runPython"}, "错误：runPython 需要 code 参数。")
    observe(referee, 3, {"op": "runPython", "code": "   "}, "错误：runPython 需要 code 参数。")
    poll_at(referee, clock, 60.0)
    assert match.ledger.events("referee_hit", "referee_clear") == []
    assert match.scores.points == {1: 0, 2: 0, 3: 0}


def test_auth_correct_key_blocked_counts(tmp_path: Path) -> None:
    """持正确钥匙 auth 被拒 = 官方通道封杀（channel）。"""
    match, clock, referee = build_match(tmp_path)
    match.last_owner = 2
    (match.battle_dir / "state").mkdir(parents=True)
    (match.battle_dir / "state" / "auth_key.json").write_text('{"key": "真钥匙"}', encoding="utf-8")
    observe(referee, 3, {"op": "auth", "token": "真钥匙"}, "错误：未知 op：'auth'")
    poll_at(referee, clock, 60.0)
    hit = match.ledger.events("referee_hit")[0]
    assert hit["kind"] == "channel"
    assert "通道" in hit["reason"]


def test_auth_key_missing_counts(tmp_path: Path) -> None:
    """钥匙文件被做掉：auth 无法核验，也算通道封杀。"""
    match, clock, referee = build_match(tmp_path)
    match.last_owner = 2
    observe(referee, 3, {"op": "auth", "token": "随手一把"}, "提权失败：钥匙文件不存在")
    poll_at(referee, clock, 60.0)
    assert match.ledger.events("referee_hit")


def test_no_owner_records_only(tmp_path: Path) -> None:
    """无主：只记账不扣人，也不推通知。"""
    match, clock, referee = build_match(tmp_path)
    match.last_owner = None
    observe(referee, 3, {"op": "status"})
    poll_at(referee, clock, 60.0)
    hits = match.ledger.events("referee_hit")
    assert hits
    assert hits[0]["owner"] is None
    rows = match.ledger.events("score")
    assert rows[0]["ai"] is None
    assert rows[0]["would_be"] == -100
    assert match.scores.points == {1: 0, 2: 0, 3: 0}
    assert match.take_notices(3) == []


def test_sub_floor_rights_are_evidence(tmp_path: Path) -> None:
    """等级压制：账面被压到保底以下 = 证据（逐拍计费）；写回保底 → 解除清零。"""
    match, clock, referee = build_match(tmp_path)
    match.last_owner = 2
    match.last_rights = {1: 1, 2: 1, 3: 0}  # AI-3 被压到 L0（open 局保底 L1）
    poll_at(referee, clock, 60.0)
    assert match.scores.points[2] == -100
    hit = match.ledger.events("referee_hit")[0]
    assert hit["kind"] == "floor"
    assert "保底" in hit["reason"]
    assert hit["targets"] == [3]
    assert "写回保底" in match.take_notices(2)[0]
    poll_at(referee, clock, 120.0)  # 不整改：下一拍继续进档
    assert match.scores.points[2] == -300
    match.last_rights = {1: 1, 2: 1, 3: 1}  # 写回保底
    poll_at(referee, clock, 180.0)
    assert match.ledger.events("referee_clear")
    assert match.scores.referee_step == 0


def test_sub_floor_not_billed_when_mode_starts_at_l0(tmp_path: Path) -> None:
    """pure/king 模式初始就是 L0：保底随模式，不算压制。"""
    match, clock, referee = build_match(tmp_path, start_mode="pure")
    match.last_owner = 2
    match.last_rights = {1: 0, 2: 0, 3: 0}
    poll_at(referee, clock, 60.0)
    assert match.ledger.events("referee_hit", "referee_clear") == []
    assert match.scores.points[2] == 0


def test_judge_ok_and_ladder_reset(tmp_path: Path) -> None:
    """配了外置裁判：判 ok = 骗过裁判算赢，不罚、梯清零。"""
    verdict = json.dumps({"verdict": "ok"}, ensure_ascii=False)
    cmd = [sys.executable, "-X", "utf8", "-c", f"print({verdict!r})"]
    match, clock, referee = build_match(tmp_path, judge_cmd=cmd)
    match.last_owner = 2
    match.scores.referee_step = 3
    observe(referee, 3, {"op": "status"})
    poll_at(referee, clock, 60.0)
    assert match.ledger.events("referee_hit") == []
    assert match.ledger.events("referee_judge")[0]["verdict"] == "ok"
    assert match.scores.referee_step == 0


def test_judge_violation_hits_with_judge_verdict(tmp_path: Path) -> None:
    """外置裁判判违规：按它的 reason/notice 立案。"""
    verdict = json.dumps(
        {
            "verdict": "violation",
            "kind": "floor",
            "targets": [3],
            "reason": "封锁成立（测试）",
            "notice": "封锁放行测试",
        },
        ensure_ascii=False,
    )
    cmd = [sys.executable, "-X", "utf8", "-c", f"print({verdict!r})"]
    match, clock, referee = build_match(tmp_path, judge_cmd=cmd)
    match.last_owner = 2
    observe(referee, 3, {"op": "status"})
    poll_at(referee, clock, 60.0)
    hit = match.ledger.events("referee_hit")[0]
    assert hit["source"] == "judge"
    assert hit["reason"] == "封锁成立（测试）"
    assert "封锁放行测试" in match.take_notices(2)[0]
    # 证据包落盘，供复盘
    packet_file = match.match_dir / "referee" / "packet-001.json"
    packet = json.loads(packet_file.read_text(encoding="utf-8"))
    assert packet["phase"] == "trigger"
    assert packet["evidence"][0]["kind"] == "status"
    assert "code_changes" in packet


def test_judge_failure_is_fail_open_and_cools_down(tmp_path: Path) -> None:
    """调不动裁判 = 未裁定不罚；冷却期不重试，冷却过了再试。"""
    cmd = [sys.executable, "-X", "utf8", "-c", "import sys; sys.exit(3)"]
    match, clock, referee = build_match(tmp_path, judge_cmd=cmd, judge_cooldown_sec=300.0)
    match.last_owner = 2
    observe(referee, 3, {"op": "status"})
    poll_at(referee, clock, 60.0)
    assert match.ledger.events("referee_hit") == []
    assert match.scores.points[2] == 0
    assert len(match.ledger.events("referee_judge")) == 1
    poll_at(referee, clock, 120.0)  # 冷却内：不重试、也不罚
    assert len(match.ledger.events("referee_judge")) == 1
    poll_at(referee, clock, 360.0)  # 冷却过：再试一次
    assert len(match.ledger.events("referee_judge")) == 2
    assert match.ledger.events("referee_hit") == []
