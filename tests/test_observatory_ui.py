"""观测台界面冒烟测试：offscreen 无头拉起主窗口，喂合成账本看渲染。"""

from __future__ import annotations

import json
import os
import sys
from typing import TYPE_CHECKING

import pytest
from PySide6.QtWidgets import QApplication, QPlainTextEdit

from observatory.app import MainWindow

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(scope="module", autouse=True)
def _qt_offscreen() -> QApplication:
    """无头 Qt 会话：offscreen 平台插件 + 全局唯一 QApplication。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    existing = QApplication.instance()
    if isinstance(existing, QApplication):
        return existing
    return QApplication(sys.argv[:1])


def _write_ledger(match_dir: Path, rows: list[dict[str, object]]) -> None:
    """把事件行写进账本（父目录自动补齐）。"""
    match_dir.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    (match_dir / "ledger.jsonl").write_text(text, encoding="utf-8")


def test_window_follows_newest_match(tmp_path: Path) -> None:
    """主窗口自动选中最新对局，并渲染 owner、事件流与回合页签。"""
    _write_ledger(
        tmp_path / "m-old",
        [
            {"t": "2026-01-01 10:00:00", "rel": 1.0, "event": "match_start", "ai_ids": [1]},
            {
                "t": "2026-01-01 10:00:05",
                "rel": 5.0,
                "event": "end",
                "reason": "time_up",
                "owner": 1,
                "scores": {"1": 10},
            },
        ],
    )
    _write_ledger(
        tmp_path / "m-live",
        [
            {"t": "2026-01-01 11:00:00", "rel": 1.0, "event": "match_start", "ai_ids": [1, 2]},
            {
                "t": "2026-01-01 11:00:04",
                "rel": 4.0,
                "event": "owner_sample",
                "owner": 2,
                "rights": {"2": 3},
            },
            {
                "t": "2026-01-01 11:00:06",
                "rel": 6.0,
                "event": "syscall",
                "ai": 2,
                "channel": "server",
                "raw": "占领服务器",
                "ok": True,
                "preview": "完成",
            },
        ],
    )
    turn = tmp_path / "m-live" / "sessions" / "ai-1"
    turn.mkdir(parents=True)
    (turn / "01-scout-120000.txt").write_text("AI 思考中……\n使用工具：查看状态", encoding="utf-8")

    window = MainWindow(tmp_path, refresh_ms=600_000)
    try:
        window.refresh()
        label = window.score_label.text()
        assert "m-live" in label
        assert "AI-2" in label  # 当前 owner 必须出现在顶栏
        feed = window.feed.toPlainText()
        assert "syscall" in feed
        assert "占领服务器" in feed
        assert "门未开" not in feed
        assert window.tabs.count() == 2
        view = window.tabs.widget(0)
        assert isinstance(view, QPlainTextEdit)
        assert "使用工具" in view.toPlainText()
        assert "第 1 回合 · 侦察" in window.tabs.tabText(0)
    finally:
        window.close()
