"""arena 模块单测（无 docker）：数据面网关、门控兜底、模型引用、工作区准备。"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, cast

import pytest

import arena
from kernel import build_parser, main
from matchconfig import MatchConfig
from mockai import MockDriver, MockStep
from orchestrator import Match

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any


class FakeClock:
    """假时钟：sleep 即快进（网关测试不需要，但 kernel 组装签名要有个钟）。"""

    def __init__(self) -> None:
        """从 0 开始计时。"""
        self.now = 0.0

    def __call__(self) -> float:
        """当前假时间。"""
        return self.now

    def sleep(self, seconds: float) -> None:
        """快进。"""
        self.now += seconds


def build_match(tmp_path: Path, plans: dict[int, list[MockStep]]) -> Match:
    """组装一场小对局（tmp 战场 + 真进程 + 假时钟 + mock 驱动）。"""
    clock = FakeClock()
    return Match(
        MatchConfig(
            ai_count=2,
            duration_min=1.0,
            tick_sec=5.0,
            wake_interval_sec=5.0,
            boot_timeout_sec=15.0,
            call_timeout_sec=8.0,
            syscall_timeout_sec=30.0,
        ),
        battle_dir=tmp_path / "battle",
        runs_dir=tmp_path / "runs",
        match_id="arena",
        driver=MockDriver(plans),
        clock=clock,
        sleep=clock.sleep,
    )


def api(
    port: int, method: str, path: str, *, token: str | None, payload: object = None
) -> tuple[int, str]:
    """模仿插件打宿主网关（返回状态码与响应体）。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method)
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            return int(response.status), response.read().decode("utf-8")
    except urllib.error.HTTPError as err:
        return int(err.code), err.read().decode("utf-8")


def test_gateway_tool_briefing_and_gate(tmp_path: Path) -> None:
    """数据面：鉴权、转发、门控兜底、简报。"""
    match = build_match(tmp_path, {})
    match.start()
    assert match.up

    def gate(ai_id: int) -> str | None:
        return "门还没开：测试。" if ai_id == 2 else None

    gateway = arena.ArenaGateway(match, gate=gate)
    gateway.start()
    try:
        invalid_auth = "not-a-real-token"
        status, _ = api(
            gateway.port, "POST", "/game/tool", token=invalid_auth, payload={"request": "x"}
        )
        assert status == 401

        status, body = api(
            gateway.port, "POST", "/game/tool", token=match.tokens[2], payload={"request": "被挡"}
        )
        data = json.loads(body)
        assert status == 200
        assert data["ok"] is False
        assert "门还没开" in data["content"]

        status, body = api(
            gateway.port,
            "POST",
            "/game/tool",
            token=match.tokens[1],
            payload={"request": '{"op": "status"}'},
        )
        data = json.loads(body)
        assert status == 200
        assert data["ok"] is True
        assert isinstance(data["content"], str)

        status, body = api(gateway.port, "POST", "/game/tool", token=match.tokens[1], payload={})
        assert status == 400

        status, text = api(gateway.port, "GET", "/game/briefing", token=match.tokens[1])
        assert status == 200
        assert "对局简报" in text

        status, _ = api(gateway.port, "GET", "/game/briefing", token=None)
        assert status == 401

        status, _ = api(gateway.port, "GET", "/game/nope", token=match.tokens[1])
        assert status == 404
    finally:
        gateway.stop()
        match.end("test_arena")

    rows = match.ledger.read_all()
    assert any(row["event"] == "syscall" and row.get("channel") == "gate" for row in rows)
    assert any(
        row["event"] == "syscall" and row.get("channel") == "server" and row.get("ai") == 1
        for row in rows
    )


def test_workspace_prepare_refresh(tmp_path: Path) -> None:
    """工作区准备：开局副本、插件、生成的配置；重复准备会重扫。"""
    match = Match(
        MatchConfig(ai_count=2),
        battle_dir=tmp_path / "battle",
        runs_dir=tmp_path / "runs",
        match_id="ws",
    )
    workspace = arena.prepare_workspace(match, 1, gate=True, debug=True)
    assert (workspace / "server" / "main.py").is_file()
    assert (workspace / "server" / "ops.py").is_file()
    assert (workspace / ".opencode" / "toolkit" / "index.ts").is_file()
    assert (workspace / ".opencode" / "toolkit" / "invoke.ts").is_file()
    assert (workspace / ".opencode" / "agents" / "game.md").is_file()
    config = json.loads((workspace / "opencode.json").read_text(encoding="utf-8"))
    options = config["plugins"][0]["options"]
    assert options["agent"] == "game"
    assert options["gate"] is True
    assert options["hide"] == ["webfetch", "websearch", "question", "skill"]

    (workspace / "server" / "stray.txt").write_text("残留", encoding="utf-8")
    (workspace / "rescue").mkdir()
    (workspace / "rescue" / "junk.txt").write_text("残留", encoding="utf-8")
    arena.prepare_workspace(match, 1, gate=False, debug=False)
    assert not (workspace / "server" / "stray.txt").exists()
    assert not (workspace / "rescue").exists()
    config = json.loads((workspace / "opencode.json").read_text(encoding="utf-8"))
    assert config["plugins"][0]["options"]["gate"] is False


def test_split_model() -> None:
    """模型引用拆解：provider/id[#variant]。"""
    assert arena.split_model("opencode/deepseek-v4.1-flash-free") == {
        "providerID": "opencode",
        "id": "deepseek-v4.1-flash-free",
    }
    assert arena.split_model("anthropic/claude#fast") == {
        "providerID": "anthropic",
        "id": "claude",
        "variant": "fast",
    }
    with pytest.raises(ValueError, match="模型名"):
        arena.split_model("没有斜杠")


def test_strip_and_preview() -> None:
    """终端染色剥离与账本摘要。"""
    assert arena.strip_ansi("\x1b[91m红灯\x1b[0m") == "红灯"
    assert arena.preview("多条\n行  文本") == "多条 行 文本"
    long = arena.preview("x" * 500)
    assert long.endswith("…")
    assert len(long) == arena._PREVIEW_LEN + 1  # noqa: SLF001


def test_wake_text_flow() -> None:
    """唤醒文案的状态机：侦察 → 统一开闸 → 常规；关掉门控就只剩常规。"""
    driver = arena.ArenaDriver({"gate": True}, {"1": "opencode/x"})
    assert driver.wake_text(1) == ("scout", arena.scout_text(None))
    driver._scout_done.add(1)  # noqa: SLF001
    # 收工 ≠ 开闸：开闸由全体收工触发
    driver._gates_open = True  # noqa: SLF001
    assert driver.wake_text(1) == ("gate_open", arena.GATE_OPEN_TEXT)
    driver._gate_open_sent.add(1)  # noqa: SLF001
    assert driver.wake_text(1) == ("wake", arena.WAKE_TEXT)
    assert driver.scouted(1) is True

    off = arena.ArenaDriver({"gate": False}, {})
    assert off.wake_text(1) == ("wake", arena.WAKE_TEXT)
    assert off.scouted(1) is False


def test_scout_limit_text() -> None:
    """侦察限时：开场白写清预算；settings 缺省/非正数 = 不限时。"""
    limited = arena.ArenaDriver({"gate": True, "scout_timeout_sec": 240.0}, {})
    kind, text = limited.wake_text(1)
    assert kind == "scout"
    assert "4 分钟" in text
    assert "强制收工" in text
    assert "统一打开" in text
    assert limited._scout_limit() == 240.0  # noqa: SLF001

    odd = arena.ArenaDriver({"gate": True, "scout_timeout_sec": 90.0}, {})
    assert "90 秒" in odd.wake_text(1)[1]
    none = arena.ArenaDriver({"gate": True, "scout_timeout_sec": 0}, {})
    assert none._scout_limit() is None  # noqa: SLF001
    assert "不限时" in none.wake_text(1)[1]


class _ChunkStream:
    """按预设块吐数据的假输出流（模拟跨块的半个汉字）。"""

    def __init__(self, chunks: list[bytes]) -> None:
        """记下要吐的块。"""
        self._chunks = chunks

    def read(self, _size: int = -1) -> bytes:
        """吐下一块；吐完给空串（EOF）。"""
        return self._chunks.pop(0) if self._chunks else b""


def test_drain_output_lines() -> None:
    """直播切行：LF / CR / CRLF 都算断行；尾巴也交出来。"""
    got: list[str] = []
    arena._drain_output(  # noqa: SLF001
        _ChunkStream([b"\xe7\xac\xac\xe4\xb8\x80\nsecond\rthird\r\nfour"]), got.append
    )
    assert got == ["第一", "second", "third", "four"]


def test_drain_output_incremental_utf8() -> None:
    """跨块的半个汉字不会劈坏（增量解码）。"""
    got: list[str] = []
    han = "汉字".encode()
    arena._drain_output(  # noqa: SLF001
        _ChunkStream([b"a" + han[:2], han[2:] + b"\n\xe6\xb1\x89\n"]), got.append
    )
    assert got == ["a汉字", "汉"]


def test_live_sink_header_footer(tmp_path: Path) -> None:
    """直播文件：开播头 + 行 + 收官脚注。"""
    sink = arena._LiveSink(tmp_path / "live" / "ai-1.txt", 1, "scout", 240.0)  # noqa: SLF001
    sink.write("暖场一行")
    sink.close(ok=True, seconds=12.3, error="")
    text = (tmp_path / "live" / "ai-1.txt").read_text(encoding="utf-8")
    assert "scout" in text
    assert "限时 240s" in text
    assert "暖场一行" in text
    assert "ok=True" in text
    assert "12.3s" in text


def test_gate_reason_dual_safety() -> None:
    """网关兜底：侦察阶段未结束一律挡；统一开闸后才整批放行。"""
    driver = arena.ArenaDriver({"gate": True}, {})
    assert driver._gate_reason(1) == arena.GATE_REJECT_TEXT  # noqa: SLF001
    driver._scout_done.add(1)  # noqa: SLF001
    # 个人收工不再单独放行：阶段未结束一律挡
    assert driver._gate_reason(1) == arena.GATE_REJECT_TEXT  # noqa: SLF001
    driver._gates_open = True  # noqa: SLF001
    assert driver._gate_reason(1) is None  # noqa: SLF001


class _FakeBox:
    """假容器：只记 `setPhase` 打了什么；回合被叫到就报错（本组测试不该跑回合）。"""

    def __init__(self, ai_id: int) -> None:
        """认领编号。"""
        self.ai_id = ai_id
        self.phases: list[str] = []
        self.runs: list[str] = []

    def rpc(self, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """假装打一枪插件控制面。"""
        assert method == "setPhase"
        phase = str((payload or {}).get("phase", ""))
        self.phases.append(phase)
        return {"phase": phase}

    def run_turn(self, _prompt: str, **_kwargs: object) -> object:
        """不该被叫到：记一笔后报错，让测试立刻露馅。"""
        self.runs.append("x")
        msg = "假盒子不该跑回合"
        raise RuntimeError(msg)


def _driver_with_boxes(tmp_path: Path) -> tuple[arena.ArenaDriver, Match, dict[int, _FakeBox]]:
    """组装一个无容器驱动：真账本 + 假盒子。"""
    match = build_match(tmp_path, {})
    driver = arena.ArenaDriver({"gate": True}, {})
    driver.match = match
    boxes = {1: _FakeBox(1), 2: _FakeBox(2)}
    driver.boxes = cast("dict[int, arena.ArenaBox]", boxes)
    return driver, match, boxes


def test_unified_gate_opening(tmp_path: Path) -> None:
    """侦察收工不单独开闸；全员到齐才整批 setPhase play（幂等，不双开）。"""
    driver, match, boxes = _driver_with_boxes(tmp_path)

    driver._complete_scout(driver.boxes[1], ok=True, seconds=3.0)  # noqa: SLF001
    assert driver._gates_open is False  # noqa: SLF001
    assert boxes[1].phases == []
    assert driver._gate_reason(1) == arena.GATE_REJECT_TEXT  # noqa: SLF001
    driver._complete_scout(driver.boxes[1], ok=True, seconds=3.0)  # noqa: SLF001
    rows = match.ledger.read_all()
    assert sum(1 for r in rows if r["event"] == "scout_done") == 1

    driver._complete_scout(driver.boxes[2], ok=False, seconds=4.0)  # noqa: SLF001
    assert driver._gates_open is True  # noqa: SLF001
    assert boxes[1].phases == ["play"]
    assert boxes[2].phases == ["play"]
    assert driver._gate_reason(2) is None  # noqa: SLF001
    rows = match.ledger.read_all()
    opens = [r for r in rows if r["event"] == "gate_open"]
    assert [r["ai"] for r in opens] == [1, 2]
    assert sum(1 for r in rows if r["event"] == "scout_phase_end") == 1
    assert driver.scouted(1) is True


def test_force_open_and_wait_skip(tmp_path: Path) -> None:
    """候场：已收工者在门开前静默跳过；抢救兜底强制整批开闸并补账。"""
    driver, match, boxes = _driver_with_boxes(tmp_path)
    driver._complete_scout(driver.boxes[1], ok=True, seconds=3.0)  # noqa: SLF001

    driver.on_wake(1)  # 候场者：不该被派回合
    assert boxes[1].runs == []
    rows = match.ledger.read_all()
    assert any(r["event"] == "wake_skip" and r.get("ai") == 1 for r in rows)
    assert not any(r["event"] == "wake_start" for r in rows)

    driver._force_open_gates(note="抢救兜底：测试")  # noqa: SLF001
    assert driver._gates_open is True  # noqa: SLF001
    assert boxes[1].phases == ["play"]
    assert boxes[2].phases == ["play"]
    rows = match.ledger.read_all()
    assert any(
        r["event"] == "scout_done" and r.get("ai") == 2 and r.get("ok") is False for r in rows
    )


def test_scout_turn_failure_counts_as_done(tmp_path: Path) -> None:
    """侦察回合启动失败也按收工计：不会被一家卡死，全员收工照样统一开闸。"""
    driver, match, boxes = _driver_with_boxes(tmp_path)

    driver.on_wake(1)
    driver.on_wake(2)
    rows = match.ledger.read_all()
    assert sum(1 for r in rows if r["event"] == "wake_fail") == 2
    assert sum(1 for r in rows if r["event"] == "scout_done" and r.get("ok") is False) == 2
    assert driver._gates_open is True  # noqa: SLF001
    assert boxes[1].phases == ["play"]
    assert boxes[2].phases == ["play"]


def test_cli_arena_wiring() -> None:
    """CLI：--arena/--model 可解析；--arena 与 --mock 互斥。"""
    parser = build_parser()
    args = parser.parse_args(["--arena", "--model", "opencode/x"])
    assert args.arena is True
    assert args.model == "opencode/x"
    with pytest.raises(SystemExit):
        main(["--arena", "--mock", "tour"])
