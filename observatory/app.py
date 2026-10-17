"""赛博夺权战 · 观测台：纯观战的 PySide6 桌面界面。

跟着对局目录 `runs/<id>/` 自动刷新，只负责展示、不负责开局或终局：

- 顶部：当前 owner（醒目）、状态、比分与权限；
- 中部：事件流（含 AI 每次 syscall 的原文与结果）；
- 下部：第 0 页「直播」看正在跑的回合（多 AI 并行时用下拉切换），
  之后每个 AI 一页收工转写（工具调用与思考原文）。

左列对局列表支持多选 + 右键删除（连 runs/<id>/ 一起删，不可逆）。

对局由你在终端里跑（如 `uv run python src/controler/kernel.py --arena ...`），
界面会自动追上最新写入的一场。

用法（在仓库根目录）：

    uv run python -m observatory.app [--runs runs] [--refresh-ms 1000]
"""

from __future__ import annotations

import argparse
import html
import sys
import time
from pathlib import Path

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QAction, QFont, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from observatory.data import (
    LedgerTail,
    MatchInfo,
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

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_REFRESH_MS = 1000
WINDOW_WIDTH = 1240
WINDOW_HEIGHT = 780
LIST_WIDTH = 240
FEED_MAX_BLOCKS = 800
FEED_BOTTOM_SLACK = 2
LIVE_MAX_BLOCKS = 4000
# 直播文件多少秒内还有新行，就当作“还在跑”（老账本没有 wake_start 时的兜底）
LIVE_FRESH_SEC = 25.0
FONT_FAMILY = "Consolas"
FONT_SIZE = 10
NO_TRANSCRIPT_TEXT = "（暂无回合文本；回合结束后会出现在这里）"
LIVE_IDLE_TEXT = "当前没有正在进行的回合（下面保留上一回合的末尾）"
LIVE_EMPTY_TEXT = "（还没有直播内容）"
SEPARATOR = "　｜　"

AI_COLORS = ("#4fc3f7", "#ffb74d", "#81c784", "#f06292", "#ba68c8")
KIND_COLORS = {
    "ok": "#d6dde6",
    "good": "#7ddc7d",
    "bad": "#ff7a7a",
    "warn": "#ffcf6b",
    "accent": "#ffd479",
    "score": "#9fd3a8",
    "muted": "#8d99a8",
    "info": "#c9d1d9",
}
STATUS_COLORS = {"ok": "#7ddc7d", "warn": "#ffcf6b", "bad": "#ff7a7a", "info": "#9aa7b8"}
DEFAULT_TEXT_COLOR = "#c9d1d9"
NEUTRAL_COLOR = "#9aa7b8"


def _ai_color(ai_id: int) -> str:
    """按 AI 编号取一个稳定的强调色。"""
    return AI_COLORS[(ai_id - 1) % len(AI_COLORS)]


class MainWindow(QMainWindow):
    """观测台主窗口：左列对局，右侧 owner / 事件流 / 回合转写。"""

    def __init__(self, runs_dir: Path, *, refresh_ms: int = DEFAULT_REFRESH_MS) -> None:
        """绑定账本目录并拉起界面（拉起后立即刷新一帧）。"""
        super().__init__()
        self.runs_dir = runs_dir
        self._states: dict[str, MatchState] = {}
        self._tails: dict[str, LedgerTail] = {}
        self._rendered: dict[str, int] = {}
        self._infos: dict[str, MatchInfo] = {}
        self._item_by_id: dict[str, QListWidgetItem] = {}
        self._current = ""
        self._suppress_hook = False
        self._tab_ais: list[int] = []
        self._tabs_match = ""
        self._turn_views: dict[int, QPlainTextEdit] = {}
        self.live_view: QPlainTextEdit | None = None
        self.live_label: QLabel | None = None
        self.live_combo: QComboBox | None = None
        self._live_selected: int | None = None
        self._live_shown = ""
        self.setWindowTitle("赛博夺权战 · 观测台")
        self.resize(WINDOW_WIDTH, WINDOW_HEIGHT)
        self._build_ui()
        self._timer = QTimer(self)
        self._timer.setInterval(refresh_ms)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self.refresh()

    # ------------------------------------------------------------ 搭界面

    def _build_ui(self) -> None:
        """搭出工具栏、左列对局与右侧观战区。"""
        bar = self.addToolBar("观测")
        bar.setMovable(False)
        self.chk_refresh = QCheckBox("自动刷新")
        self.chk_refresh.setChecked(True)
        self.chk_refresh.toggled.connect(self._on_autorefresh)
        bar.addWidget(self.chk_refresh)
        act_refresh = QAction("立即刷新", self)
        act_refresh.triggered.connect(self.refresh)
        bar.addAction(act_refresh)
        bar.addSeparator()
        self.chk_follow = QCheckBox("跟随最新对局")
        self.chk_follow.setChecked(True)
        self.chk_follow.setToolTip("自动追到最新有写入的一场；手动点选其他对局后自动关闭")
        bar.addWidget(self.chk_follow)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.match_list = QListWidget()
        self.match_list.setMaximumWidth(LIST_WIDTH)
        self.match_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.match_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.match_list.setToolTip("多选（Ctrl/Shift）；右键可删除选中的对局")
        self.match_list.currentItemChanged.connect(self._on_item_changed)
        self.match_list.customContextMenuRequested.connect(self._on_list_menu)
        splitter.addWidget(self.match_list)

        right = QWidget()
        layout = QVBoxLayout(right)
        self.score_label = QLabel("（还没有对局）")
        self.score_label.setTextFormat(Qt.TextFormat.RichText)
        self.score_label.setWordWrap(True)
        layout.addWidget(self.score_label)
        self.feed = QPlainTextEdit()
        self.feed.setReadOnly(True)
        self.feed.setMaximumBlockCount(FEED_MAX_BLOCKS)
        self.feed.setFont(QFont(FONT_FAMILY, FONT_SIZE))
        self.feed.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.feed.setPlaceholderText("还没有事件……")
        layout.addWidget(self.feed, stretch=3)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, stretch=2)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        self.setCentralWidget(splitter)

    # ------------------------------------------------------------ 主循环

    def refresh(self) -> None:
        """重扫 runs/：同步列表、跟进选中对局、刷新界面。"""
        matches = discover_matches(self.runs_dir)
        self._infos = {m.match_id: m for m in matches}
        self._sync_list(matches)
        target = self._pick_target(matches)
        if target is None:
            self.score_label.setText("（runs/ 下还没有对局——去终端里开一局吧）")
            self.statusBar().showMessage(f"账本目录：{self.runs_dir} ｜ 未发现对局")
            return
        self._load_match(target)
        stamp = time.strftime("%H:%M:%S")
        self.statusBar().showMessage(
            f"账本目录：{self.runs_dir} ｜ 共 {len(matches)} 场 ｜ 已刷新 {stamp}"
        )

    def _pick_target(self, matches: list[MatchInfo]) -> MatchInfo | None:
        """挑要展示的对局：跟随模式下永远是最新写入的一场。"""
        if not matches:
            return None
        if self.chk_follow.isChecked():
            return matches[0]
        item = self.match_list.currentItem()
        mid = str(item.data(Qt.ItemDataRole.UserRole)) if item is not None else ""
        for match in matches:
            if match.match_id == mid:
                return match
        return matches[0]

    def _sync_list(self, matches: list[MatchInfo]) -> None:
        """把 runs/ 的现状同步进左列（顺序稳定，新增的置顶）。"""
        alive = {m.match_id for m in matches}
        for gone in [mid for mid in self._item_by_id if mid not in alive]:
            row = self.match_list.row(self._item_by_id.pop(gone))
            if row >= 0:
                self.match_list.takeItem(row)
        fresh = [m for m in matches if m.match_id not in self._item_by_id]
        if not self._item_by_id and fresh:  # 首轮：按序整批放入
            for match in fresh:
                self.match_list.addItem(self._make_item(match))
        else:
            for match in reversed(fresh):  # 后出现的更新，倒着插保证最新在顶
                self.match_list.insertItem(0, self._make_item(match))
        for match in matches:
            item = self._item_by_id.get(match.match_id)
            if item is not None:
                item.setText(self._item_text(match))

    def _make_item(self, match: MatchInfo) -> QListWidgetItem:
        """建一条对局列表项（记住 match_id）。"""
        item = QListWidgetItem(self._item_text(match))
        item.setData(Qt.ItemDataRole.UserRole, match.match_id)
        self._item_by_id[match.match_id] = item
        return item

    def _item_text(self, match: MatchInfo) -> str:
        """列表项文案：对局名 + 一行状态。"""
        state = self._states.get(match.match_id)
        if state is None:
            stamp = time.strftime("%H:%M:%S", time.localtime(match.mtime))
            return f"{match.match_id}\n· 未查看 · {stamp}"
        mark = "✔" if state.ended else "●"
        return f"{match.match_id}\n{mark} {state.status}"

    def _on_item_changed(
        self, current: QListWidgetItem | None, _previous: QListWidgetItem | None
    ) -> None:
        """手动换台 = 放弃跟随（程序化切换不触发）。"""
        if self._suppress_hook or current is None:
            return
        mid = str(current.data(Qt.ItemDataRole.UserRole))
        if mid != self._current:
            self.chk_follow.setChecked(False)

    def _select(self, match_id: str) -> None:
        """程序化选中某场对局（不触发“放弃跟随”）。"""
        item = self._item_by_id.get(match_id)
        if item is None or self.match_list.currentItem() is item:
            return
        self._suppress_hook = True
        try:
            self.match_list.setCurrentItem(item)
        finally:
            self._suppress_hook = False

    # ------------------------------------------------------------ 对局数据

    def _load_match(self, match: MatchInfo) -> None:
        """跟进选中对局：读账本新行，刷新事件流 / 状态栏 / 回合页。"""
        state = self._states.get(match.match_id)
        if state is None or match.match_id not in self._tails:
            state = MatchState()
            self._states[match.match_id] = state
            self._tails[match.match_id] = LedgerTail(match.ledger_path)
        for row in self._tails[match.match_id].read_new():
            state.apply(row)
        switching = self._current != match.match_id
        self._current = match.match_id
        self._select(match.match_id)
        if switching:
            self._rebuild_feed(match.match_id, state)
        else:
            self._flush_feed(match.match_id, state)
        self._render_scoreboard(match, state)
        self._sync_tabs(match, state)
        self._sync_live(match, state)
        item = self._item_by_id.get(match.match_id)
        if item is not None:
            item.setText(self._item_text(match))

    def _flush_feed(self, match_id: str, state: MatchState) -> None:
        """把新到的账本行追加进事件流（守住“用户滚上去就别拉回来”）。"""
        rendered = self._rendered.get(match_id, 0)
        pending = state.row_count - rendered
        if pending <= 0:
            return
        if pending > len(state.rows):  # 中段已被裁掉，只能全量重建
            self._rebuild_feed(match_id, state)
            return
        sb = self.feed.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - FEED_BOTTOM_SLACK
        for row in list(state.rows)[-pending:]:
            self._append_row(row)
        self._rendered[match_id] = state.row_count
        if at_bottom:
            self._scroll_feed_to_end()

    def _rebuild_feed(self, match_id: str, state: MatchState) -> None:
        """换台或裁行后整体重画事件流。"""
        self.feed.clear()
        for row in state.rows:
            self._append_row(row)
        self._rendered[match_id] = state.row_count
        self._scroll_feed_to_end()

    def _append_row(self, row: dict[str, object]) -> None:
        """按语义色追加一行事件（空格换成不换行空格，保持等宽对齐）。"""
        line = event_line(row)
        if not line:
            return
        color = KIND_COLORS.get(kind_of(row), DEFAULT_TEXT_COLOR)
        body = html.escape(line).replace(" ", "\u00a0")
        self.feed.appendHtml(f'<span style="color:{color};white-space:pre">{body}</span>')

    def _scroll_feed_to_end(self) -> None:
        """事件流滚到底。"""
        self.feed.moveCursor(QTextCursor.MoveOperation.End)
        sb = self.feed.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _render_scoreboard(self, match: MatchInfo, state: MatchState) -> None:
        """刷新顶部：对局名、计时、状态、当前 owner 与比分。"""
        status_color = STATUS_COLORS.get(state.status_kind, NEUTRAL_COLOR)
        if state.owner is None:
            owner_html = f'<b style="color:{NEUTRAL_COLOR};font-size:13pt">无主</b>'
        else:
            owner_html = (
                f'<b style="color:{_ai_color(state.owner)};font-size:13pt">AI-{state.owner}</b>'
            )
        head = SEPARATOR.join(
            [
                f'<span style="color:{NEUTRAL_COLOR}">{html.escape(match.match_id)}</span>',
                f'<b style="font-size:13pt">{fmt_rel(state.rel)}</b>',
                f'<b style="color:{status_color}">{html.escape(state.status)}</b>',
                f"owner：{owner_html}",
            ]
        )
        self.score_label.setText(f"{head}<br>{self._scores_html(state)}")

    @staticmethod
    def _scores_html(state: MatchState) -> str:
        """比分与权限：每个 AI 一行内并排。"""
        ids = sorted(set(state.ai_ids) | set(state.scores))
        cells: list[str] = []
        for ai in ids:
            level = state.rights.get(ai)
            level_txt = f"（L{level}）" if level else ""
            points = state.scores.get(ai, 0)
            cells.append(
                f'<span style="color:{_ai_color(ai)}">AI-{ai} <b>{points:+d}</b>{level_txt}</span>'
            )
        return SEPARATOR.join(cells) if cells else "（比分未开）"

    # ------------------------------------------------------------ 直播与转写

    def _sync_tabs(self, match: MatchInfo, state: MatchState) -> None:
        """每个 AI 一个转写页签（第 0 页永远是直播）。"""
        ais = list(state.ai_ids) or session_ai_ids(match.dir)
        if ais != self._tab_ais or match.match_id != self._tabs_match:
            self._rebuild_tabs(match, ais)
        for ai, view in self._turn_views.items():
            transcript = latest_transcript(match.dir, ai)
            if transcript is None:
                if view.toPlainText() != NO_TRANSCRIPT_TEXT:
                    view.setPlainText(NO_TRANSCRIPT_TEXT)
                continue
            if view.toPlainText() == transcript.text:
                continue
            view.setPlainText(transcript.text)
            view.moveCursor(QTextCursor.MoveOperation.Start)
            title = f"AI-{ai} · {turn_label(transcript.name)}"
            self.tabs.setTabText(self.tabs.indexOf(view), title)

    def _rebuild_tabs(self, match: MatchInfo, ais: list[int]) -> None:
        """换台后重建页签集合（先直播页，再每个 AI 一页）。"""
        self.tabs.clear()
        self._turn_views = {}
        self._live_selected = None
        self._live_shown = ""
        self._build_live_tab()
        for ai in ais:
            view = QPlainTextEdit()
            view.setReadOnly(True)
            view.setFont(QFont(FONT_FAMILY, FONT_SIZE))
            view.setPlainText(NO_TRANSCRIPT_TEXT)
            self.tabs.addTab(view, f"AI-{ai}")
            self._turn_views[ai] = view
        self._tab_ais = list(ais)
        self._tabs_match = match.match_id

    def _build_live_tab(self) -> None:
        """搭直播页：一行状态（谁在跑 + 选择器）+ 一块滚动直播文本。"""
        pane = QWidget()
        layout = QVBoxLayout(pane)
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        self.live_label = QLabel(LIVE_EMPTY_TEXT)
        self.live_combo = QComboBox()
        self.live_combo.currentIndexChanged.connect(self._on_live_pick)
        row_layout.addWidget(self.live_label, stretch=1)
        row_layout.addWidget(self.live_combo)
        layout.addWidget(row)
        self.live_view = QPlainTextEdit()
        self.live_view.setReadOnly(True)
        self.live_view.setFont(QFont(FONT_FAMILY, FONT_SIZE))
        self.live_view.setMaximumBlockCount(LIVE_MAX_BLOCKS)
        self.live_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.live_view.setPlaceholderText(LIVE_EMPTY_TEXT)
        layout.addWidget(self.live_view, stretch=1)
        self.tabs.addTab(pane, "直播")

    def _sync_live(self, match: MatchInfo, state: MatchState) -> None:
        """刷新直播页：谁在跑（账本为准，文件新鲜度兜底）+ 选中 AI 的直播尾巴。"""
        combo = self.live_combo
        if combo is None:
            return
        ais = list(state.ai_ids) or session_ai_ids(match.dir)
        running = sorted(ai for ai, turn in state.turns.items() if turn.get("running"))
        now = time.time()
        fresh: dict[int, bool] = {}
        tails: dict[int, str] = {}
        for ai in ais:
            text, mtime = live_tail(match.dir, ai)
            tails[ai] = text
            fresh[ai] = mtime > 0 and (now - mtime) <= LIVE_FRESH_SEC and not state.ended
        active = [ai for ai in ais if ai in running or fresh[ai]]
        combo.blockSignals(True)  # noqa: FBT003 —— Qt 的老接口，只能位置传参
        try:
            combo.clear()
            for ai in ais:
                combo.addItem(f"AI-{ai}{' ●' if ai in active else ''}", ai)
            if self._live_selected not in ais:
                self._live_selected = active[-1] if active else (ais[-1] if ais else None)
            index = combo.findData(self._live_selected)
            if index >= 0:
                combo.setCurrentIndex(index)
        finally:
            combo.blockSignals(False)  # noqa: FBT003 —— 同上
        self.tabs.setTabText(0, "● 直播" if active else "直播")
        if self.live_label is not None:
            if active:
                who = "、".join(f"AI-{ai}" for ai in active)
                tail = f"（本页看 AI-{self._live_selected}）" if len(active) > 1 else ""
                self.live_label.setText(f"正在直播：{who}{tail}")
            elif self._live_selected is not None:
                self.live_label.setText(LIVE_IDLE_TEXT)
            else:
                self.live_label.setText(LIVE_EMPTY_TEXT)
        if self._live_selected is not None:
            self._show_live_text(tails.get(self._live_selected, ""))

    def _show_live_text(self, text: str) -> None:
        """把直播尾巴贴进直播页（守住“用户滚上去就别拉回来”）。"""
        view = self.live_view
        if view is None:
            return
        body = text or LIVE_EMPTY_TEXT
        if body == self._live_shown:
            return
        self._live_shown = body
        sb = view.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - FEED_BOTTOM_SLACK
        pos = sb.value()
        view.setPlainText(body)
        if at_bottom:
            view.moveCursor(QTextCursor.MoveOperation.End)
            sb.setValue(sb.maximum())
        else:
            sb.setValue(min(pos, sb.maximum()))

    def _on_live_pick(self, _index: int) -> None:
        """用户在下拉里选了别的 AI：记住选择（下一拍刷新时展示）。"""
        combo = self.live_combo
        if combo is None:
            return
        data = combo.currentData()
        if isinstance(data, int):
            self._live_selected = data

    # ------------------------------------------------------------ 删除对局

    def _on_list_menu(self, pos: QPoint) -> None:
        """左列右键：删除选中的对局（多选有效）。"""
        item = self.match_list.itemAt(pos)
        if item is not None and not item.isSelected():
            self._suppress_hook = True
            try:
                self.match_list.clearSelection()
                item.setSelected(True)
            finally:
                self._suppress_hook = False
        match_ids = self._selected_ids()
        if not match_ids:
            return
        menu = QMenu(self)
        action = menu.addAction(f"删除选中的对局（{len(match_ids)} 场）…")
        chosen = menu.exec(self.match_list.viewport().mapToGlobal(pos))
        if chosen is action:
            self._confirm_delete(match_ids)

    def _selected_ids(self) -> list[str]:
        """当前选中的对局 id（按列表出现顺序）。"""
        return [
            str(item.data(Qt.ItemDataRole.UserRole)) for item in self.match_list.selectedItems()
        ]

    def _confirm_delete(self, match_ids: list[str]) -> None:
        """删除确认 → 执行删除 → 忘了它们的内存档；失败的报告出来。"""
        infos = [self._infos[mid] for mid in match_ids if mid in self._infos]
        if not infos:
            return
        risky: list[str] = []
        for info in infos:
            state = self._states.get(info.match_id)
            ended = state.ended if state is not None else looks_ended(info.dir)
            if ended is not True:
                risky.append(info.match_id)
        if not self._ask_delete_confirmation(infos, risky):
            return
        failed: list[str] = []
        for info in infos:
            try:
                delete_match(info.dir)
            except OSError:
                failed.append(info.match_id)
            else:
                self._forget_match(info.match_id)
        self.refresh()
        if failed:
            QMessageBox.warning(
                self, "删除未完成", "这些对局没能删掉（文件可能被占用）：\n" + "、".join(failed)
            )

    def _ask_delete_confirmation(self, infos: list[MatchInfo], risky: list[str]) -> bool:
        """弹确认框：列出对局（还在跑的给警告），返回是否点了「删除」。"""
        lines = [f"将删除 {len(infos)} 场对局（账本、回合卷宗、直播文件一并删除）："]
        for info in infos:
            mark = "  ⚠ 看起来还在跑" if info.match_id in risky else ""
            lines.append(f"　· {info.match_id}{mark}")
        lines.append("")
        lines.append(f"目录：{self.runs_dir}")
        lines.append("此操作不可撤销。")
        box = QMessageBox(self)
        box.setWindowTitle("删除对局")
        box.setIcon(QMessageBox.Icon.Warning if risky else QMessageBox.Icon.Question)
        box.setText("\n".join(lines))
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        yes = box.button(QMessageBox.StandardButton.Yes)
        if yes is not None:
            yes.setText("删除")
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        return box.exec() == QMessageBox.StandardButton.Yes

    def _forget_match(self, match_id: str) -> None:
        """清掉一场对局的界面内存档（runs/ 的删除由 delete_match 负责）。"""
        self._states.pop(match_id, None)
        self._tails.pop(match_id, None)
        self._rendered.pop(match_id, None)
        self._infos.pop(match_id, None)
        item = self._item_by_id.pop(match_id, None)
        if item is not None:
            row = self.match_list.row(item)
            if row >= 0:
                self.match_list.takeItem(row)
        if self._current == match_id:
            self._current = ""
            self._tab_ais = []
            self._tabs_match = ""

    # ------------------------------------------------------------ 杂项

    def _on_autorefresh(self, checked: bool) -> None:
        """自动刷新开关。"""
        if checked:
            self._timer.start()
        else:
            self._timer.stop()


def main(argv: list[str] | None = None) -> int:
    """命令行入口：解析参数、起窗口、进入事件循环。"""
    parser = argparse.ArgumentParser(
        prog="observatory", description="赛博夺权战 · 观测台（纯观战）"
    )
    parser.add_argument(
        "--runs", type=Path, default=ROOT / "runs", help="对局目录（默认仓库 runs/）"
    )
    parser.add_argument(
        "--refresh-ms", type=int, default=DEFAULT_REFRESH_MS, help="自动刷新间隔（毫秒）"
    )
    parser.add_argument("--selftest", action="store_true", help="跑 2 秒后自动退出（无头自检）")
    args = parser.parse_args(argv)
    app = QApplication(sys.argv[:1])
    window = MainWindow(args.runs.resolve(), refresh_ms=args.refresh_ms)
    window.show()
    if args.selftest:
        QTimer.singleShot(2000, app.quit)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
