"""内核并行回合：真进程 + 假时钟 + 会卡的假驱动，验证“谁也不等谁”。"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

from matchconfig import MatchConfig
from orchestrator import Match

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


class FakeClock:
    """假时钟：手动拨表（并行测试要精确控制节拍）。"""

    def __init__(self) -> None:
        """从 0 开始计时。"""
        self.now = 0.0

    def __call__(self) -> float:
        """当前假时间。"""
        return self.now

    def sleep(self, seconds: float) -> None:
        """快进。"""
        self.now += seconds


class BlockingDriver:
    """并行驱动替身：on_wake 卡在闸门上，直到测试放行。"""

    parallel_turns = True

    def __init__(self) -> None:
        """备好闸门、计数与重叠探测。"""
        self.release = threading.Event()
        self.overlap = threading.Event()  # 出现过两个 AI 同时在跑
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.starts: dict[int, int] = {}

    def attach(self, _match: Match) -> None:
        """占位：本替身不需要对局引用。"""

    def on_wake(self, ai_id: int) -> None:
        """卡住直到放行；同时统计并发度与开跑次数（两个 AI 必须能同时在跑）。"""
        with self._lock:
            self.starts[ai_id] = self.starts.get(ai_id, 0) + 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            if self.active >= 2:
                self.overlap.set()
        self.release.wait(timeout=10.0)
        with self._lock:
            self.active -= 1

    def detach(self) -> None:
        """占位：没有真资源要收。"""


def _build(tmp_path: Path, driver: BlockingDriver) -> tuple[Match, FakeClock]:
    """组装一场并行小对局（真进程 + 假时钟 + 阻塞驱动）。"""
    clock = FakeClock()
    config = MatchConfig(
        ai_count=2,
        duration_min=5.0,
        tick_sec=1.0,
        wake_interval_sec=1.0,
        boot_timeout_sec=15.0,
        call_timeout_sec=8.0,
        syscall_timeout_sec=30.0,
    )
    match = Match(
        config,
        battle_dir=tmp_path / "battle",
        runs_dir=tmp_path / "runs",
        match_id="parallel",
        driver=driver,
        clock=clock,
        sleep=clock.sleep,
    )
    return match, clock


def _wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    """等到条件成立或超时。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


def test_parallel_wakes_block_nothing(tmp_path: Path) -> None:
    """同一拍两个 AI 同时在跑；回合卡着也照样心跳采样；错过节拍不补课。"""
    driver = BlockingDriver()
    match, clock = _build(tmp_path, driver)
    match.start()
    try:
        clock.now = 1.0
        assert match.step()  # 同一拍派发两个回合
        assert driver.overlap.wait(timeout=5.0), "两个 AI 没有同时在跑"

        # 回合卡着不动：主循环照常心跳采样（链路没被卡住的回合占住）
        clock.now = 2.5
        assert match.step()
        state_now: str = match.state
        assert state_now == "running"
        rows = match.ledger.read_all()
        assert any(row["event"] == "owner_sample" for row in rows)

        # 放行：收工时这一拍已经跳过去了（grid=3.0 > now=2.5），不会立刻重开
        driver.release.set()
        deadline = time.monotonic() + 5.0
        while match._wake_threads and time.monotonic() < deadline:  # noqa: SLF001
            match.step()
            time.sleep(0.02)
        assert not match._wake_threads  # noqa: SLF001
        assert driver.max_active == 2
        assert driver.starts == {1: 1, 2: 1}

        # 到下一拍：两个 AI 各再开一次（错过的那几拍不补课）
        clock.now = 3.0
        assert match.step()
        assert _wait_until(lambda: driver.starts.get(1, 0) == 2 and driver.starts.get(2, 0) == 2)

        # 到点终局：在途/已收工的回合都被安置，进程干净退出
        clock.now = match.end_time
        assert not match.step()
        assert match.state == "ended"
        rows = match.ledger.read_all()
        start_row = next(row for row in rows if row["event"] == "match_start")
        assert start_row["parallel_turns"] is True
        assert match.summary()["state"] == "ended"
    finally:
        driver.release.set()
        if match.state != "ended":
            match.end("test_over")
