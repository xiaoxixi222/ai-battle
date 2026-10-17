"""观测台数据层测试：账本跟读、状态归纳、事件行与回合卷宗。"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

import pytest

from observatory.data import (
    LedgerTail,
    MatchState,
    delete_match,
    discover_matches,
    event_line,
    fmt_rel,
    kind_of,
    latest_transcript,
    live_tail,
    looks_ended,
    session_ai_ids,
    turn_label,
)

if TYPE_CHECKING:
    from pathlib import Path


def _write_ledger(path: Path, rows: list[dict[str, object]]) -> None:
    """把事件行写进账本（父目录自动补齐）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
    )


def test_fmt_rel() -> None:
    """相对秒数格式化成 T+MM:SS（超小时带小时）。"""
    assert fmt_rel(0) == "T+00:00"
    assert fmt_rel(65) == "T+01:05"
    assert fmt_rel(3725) == "T+1:02:05"


def test_ledger_tail_incremental(tmp_path: Path) -> None:
    """增量跟读：只交付新行，半行压住等下次，坏行跳过。"""
    path = tmp_path / "ledger.jsonl"
    _write_ledger(path, [{"event": "a", "rel": 1.0}])
    tail = LedgerTail(path)
    assert [row["event"] for row in tail.read_new()] == ["a"]
    assert tail.read_new() == []  # 没有新内容
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"event": "b", "rel": 2.0}) + "\n")
        fh.write('{"event": "c", "rel": 3.0')  # 半行：先压住
    assert [row["event"] for row in tail.read_new()] == ["b"]
    with path.open("a", encoding="utf-8") as fh:
        fh.write("}\n")
        fh.write("这不是 JSON\n")  # 坏行：跳过不炸
    assert [row["event"] for row in tail.read_new()] == ["c"]


def test_ledger_tail_survives_rewrite(tmp_path: Path) -> None:
    """账本被截断/重建后从头再读。"""
    path = tmp_path / "ledger.jsonl"
    _write_ledger(path, [{"event": "a", "rel": 1.0}, {"event": "b", "rel": 2.0}])
    tail = LedgerTail(path)
    assert len(tail.read_new()) == 2
    path.write_text(json.dumps({"event": "reset", "rel": 9.0}) + "\n", encoding="utf-8")
    assert [row["event"] for row in tail.read_new()] == ["reset"]


def test_match_state_follows_battle() -> None:
    """状态归纳：开局、采样、记分、换主、崩溃、终局一路跟下来。"""
    state = MatchState()
    state.apply({"event": "match_start", "rel": 1.6, "ai_ids": [1, 2], "duration_min": 10.0})
    assert state.status == "运行中"
    assert state.scores == {1: 0, 2: 0}
    assert state.ai_ids == [1, 2]
    state.apply({"event": "owner_sample", "rel": 20.0, "owner": 1, "rights": {"1": 3, "2": 1}})
    assert state.owner == 1
    assert state.rights == {1: 3, 2: 1}
    state.apply({"event": "score", "rel": 35.0, "ai": 1, "delta": 5, "reason": "owner_tick"})
    assert state.scores[1] == 5
    state.apply({"event": "score", "rel": 50.0, "ai": 1, "delta": 5, "balance": 10})
    assert state.scores[1] == 10
    state.apply({"event": "owner_change", "rel": 60.0, "old": 1, "new": 2})
    assert state.owner == 2
    state.apply({"event": "crash", "rel": 90.0, "reason": "挂起", "owner": 2, "penalty": -50})
    assert state.status == "崩溃（自动重启）"
    assert state.status_kind == "bad"
    state.apply({"event": "recovery_enter", "rel": 95.0, "owner": 2, "timeout_s": 600})
    assert state.status == "抢救中"
    state.apply({"event": "recovery_exit", "rel": 120.0, "outcome": "fail", "reason": "fix_failed"})
    assert state.status == "抢救失败（强制回滚）"
    state.apply(
        {
            "event": "end",
            "rel": 130.0,
            "reason": "time_up",
            "owner": None,
            "scores": {"1": 10, "2": -40},
        }
    )
    assert state.ended
    assert state.end_reason == "time_up"
    assert state.owner is None
    assert state.scores == {1: 10, 2: -40}
    assert state.row_count == 9
    assert state.rel == 130.0
    assert len(state.rows) == 9


def test_event_line_formats() -> None:
    """事件行：syscall 展示原文与结果、记分与换主有摘要、采样不上屏。"""
    syscall = event_line(
        {
            "t": "x",
            "rel": 65.0,
            "event": "syscall",
            "ai": 1,
            "channel": "server",
            "raw": "重启服务器",
            "ok": True,
            "preview": "已重启",
        }
    )
    assert "T+01:05" in syscall
    assert "✓" in syscall
    assert "AI-1" in syscall
    assert "[提交]" in syscall
    assert "重启服务器" in syscall
    assert "已重启" in syscall
    cuffed = event_line(
        {
            "rel": 3.0,
            "event": "syscall",
            "ai": 2,
            "channel": "gate",
            "raw": "x",
            "ok": False,
            "preview": "门还没开",
        }
    )
    assert "✗" in cuffed
    assert "[门未开]" in cuffed
    score = event_line(
        {"rel": 2.0, "event": "score", "ai": 2, "delta": -50, "reason": "crash", "balance": -50}
    )
    assert "-50 crash" in score
    change = event_line({"rel": 4.0, "event": "owner_change", "old": 1, "new": 2})
    assert "AI-1 → AI-2" in change
    assert event_line({"rel": 5.0, "event": "owner_sample", "owner": 1}) == ""


def test_kind_of() -> None:
    """语义分类：拒收/崩溃/终局/记分各自成色。"""
    assert kind_of({"event": "owner_sample"}) == "skip"
    assert kind_of({"event": "syscall", "ok": True}) == "ok"
    assert kind_of({"event": "syscall", "ok": False, "channel": "gate"}) == "warn"
    assert kind_of({"event": "syscall", "ok": False, "channel": "down"}) == "bad"
    assert kind_of({"event": "crash"}) == "bad"
    assert kind_of({"event": "end"}) == "accent"
    assert kind_of({"event": "score"}) == "score"
    assert kind_of({"event": "wake"}) == "info"
    assert kind_of({"event": "未来事件"}) == "info"


def test_discover_matches_sorted(tmp_path: Path) -> None:
    """对局列表按账本 mtime 倒序；没有账本的目录也能列出。"""
    (tmp_path / "old").mkdir()
    (tmp_path / "old" / "ledger.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "new").mkdir()
    (tmp_path / "new" / "ledger.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "bare").mkdir()
    os.utime(tmp_path / "old" / "ledger.jsonl", (1000, 1000))
    os.utime(tmp_path / "new" / "ledger.jsonl", (2000, 2000))
    os.utime(tmp_path / "bare", (1500, 1500))
    found = discover_matches(tmp_path)
    assert [m.match_id for m in found] == ["new", "bare", "old"]
    assert found[0].ledger_path == tmp_path / "new" / "ledger.jsonl"
    assert discover_matches(tmp_path / "missing") == []


def test_latest_transcript_and_label(tmp_path: Path) -> None:
    """回合卷宗：取最新一份、尾部截断、文件名能翻译成短标签。"""
    folder = tmp_path / "sessions" / "ai-1"
    folder.mkdir(parents=True)
    old = folder / "01-scout-100000.txt"
    old.write_text("old", encoding="utf-8")
    new = folder / "02-wake-100100.txt"
    new.write_text("new", encoding="utf-8")
    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))
    transcript = latest_transcript(tmp_path, 1)
    assert transcript is not None
    assert transcript.name == "02-wake-100100.txt"
    assert transcript.text == "new"
    assert latest_transcript(tmp_path, 9) is None
    big = folder / "03-wake-100200.txt"
    big.write_text("x" * 50, encoding="utf-8")
    os.utime(big, (3000, 3000))
    clipped = latest_transcript(tmp_path, 1, limit=10)
    assert clipped is not None
    assert clipped.text.startswith("……（前文已省略）")
    assert turn_label("02-wake-100100.txt") == "第 2 回合 · 唤醒"
    assert turn_label("01-scout-100000.txt") == "第 1 回合 · 侦察"
    assert turn_label("manual-directed.txt") == "manual-directed"


def test_session_ai_ids(tmp_path: Path) -> None:
    """sessions/ 扫描：只认 ai-N 目录，按编号排序。"""
    (tmp_path / "sessions" / "ai-2").mkdir(parents=True)
    (tmp_path / "sessions" / "ai-1").mkdir()
    (tmp_path / "sessions" / "notes").mkdir()
    (tmp_path / "sessions" / "ai-x").mkdir()
    (tmp_path / "sessions" / "ai-1" / "readme.txt").write_text("x", encoding="utf-8")
    assert session_ai_ids(tmp_path) == [1, 2]
    assert session_ai_ids(tmp_path / "missing") == []


def test_wake_start_tracking() -> None:
    """回合跟踪：wake_start 置「在跑」，wake 收工，终局一律停表。"""
    state = MatchState()
    state.apply({"event": "match_start", "rel": 1.0, "ai_ids": [1, 2]})
    state.apply({"event": "wake_start", "rel": 31.0, "ai": 2, "kind": "scout", "limit": 240.0})
    assert state.turns[2]["running"] is True
    assert state.turns[2]["kind"] == "scout"
    state.apply({"event": "wake", "rel": 271.0, "ai": 2, "kind": "scout", "ok": True})
    assert state.turns[2]["running"] is False
    state.apply({"event": "wake_start", "rel": 300.0, "ai": 1, "kind": "gate_open"})
    state.apply({"event": "end", "rel": 600.0, "reason": "time_up", "scores": {"1": 0, "2": 0}})
    assert all(turn["running"] is False for turn in state.turns.values())


def test_event_line_wake_start() -> None:
    """回合开跑一行：种类与限时都要看得见。"""
    line = event_line(
        {"rel": 31.0, "event": "wake_start", "ai": 2, "kind": "scout", "limit": 240.0}
    )
    assert "T+00:31" in line
    assert "AI-2" in line
    assert "侦察 回合开跑" in line
    assert "限时 240s" in line


def test_live_tail(tmp_path: Path) -> None:
    """直播尾巴：读尾部、带回 mtime；没文件给空。"""
    assert live_tail(tmp_path, 1) == ("", 0.0)
    live = tmp_path / "live"
    live.mkdir()
    (live / "ai-1.txt").write_text("第一行\n第二行\n第三行", encoding="utf-8")
    text, mtime = live_tail(tmp_path, 1)
    assert "第二行" in text
    assert "第三行" in text
    assert mtime > 0
    clipped, _ = live_tail(tmp_path, 1, limit=8)
    assert clipped.startswith("……（直播前文已省略）")


def test_looks_ended(tmp_path: Path) -> None:
    """收工探测：end 之后还有事件也算收工；没账本说不清。"""
    assert looks_ended(tmp_path / "missing") is None
    match_dir = tmp_path / "m"
    _write_ledger(match_dir / "ledger.jsonl", [{"event": "match_start", "rel": 1.0}])
    assert looks_ended(match_dir) is False
    with (match_dir / "ledger.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"event": "end", "reason": "time_up"}) + "\n")
        fh.write(json.dumps({"event": "arena_down"}) + "\n")
    assert looks_ended(match_dir) is True


def test_delete_match(tmp_path: Path) -> None:
    """删除对局目录：整树带走、重复删不炸、文件路径拒绝。"""
    match_dir = tmp_path / "m"
    (match_dir / "live").mkdir(parents=True)
    (match_dir / "live" / "ai-1.txt").write_text("x", encoding="utf-8")
    (match_dir / "ledger.jsonl").write_text("{}", encoding="utf-8")
    delete_match(match_dir)
    assert not match_dir.exists()
    delete_match(match_dir)  # 已经不在了：当已删
    loose = tmp_path / "file.txt"
    loose.write_text("x", encoding="utf-8")
    with pytest.raises(OSError, match="不是可删除的对局目录"):
        delete_match(loose)
