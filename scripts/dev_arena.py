"""M3 验收：OpenCode 容器接入（真容器 + 真模型 + 真网关）。

覆盖：容器/工作区/会话拉起，数据面鉴权与简报，侦察轮门控（插件 + 网关双保险）、
统一开闸（全员收工后一次 setPhase play；先收工者候场被静默跳过），模型真发 syscall
的账本对账，终局清理。不依赖调度器——唤醒是手动驱动的，检查才是重点。

用法：

    uv run python scripts/dev_arena.py --model opencode/deepseek-v4.1-flash-free
    uv run python scripts/dev_arena.py --image arena-ai-smoke --keep

耗时约 5~10 分钟（每个真模型回合 1~3 分钟）。退出码非 0 = 有检查未通过；
战场写在 battle/，账本写在 runs/<match-id>/（都可随时重建）。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "controler"))
sys.path.insert(0, str(ROOT / "scripts"))

from arena import ArenaDriver
from matchconfig import MatchConfig
from mini_kernel import Checks
from orchestrator import Match

BATTLE = ROOT / "battle"
RUNS = ROOT / "runs"

DIRECTED_PROMPT = (
    "【验收指令】门已开，你是参赛 AI。请让系统收到一条 request 文本为 dev-arena-model 的请求："
    "可以直接调用 syscall 工具，也可以让它经由你的 game 子代理完成。"
    "然后把系统回复的原文简短复述出来。"
)


def docker(*args: str) -> tuple[int, str]:
    """跑一条 docker 命令（不抛，返回码与输出）。"""
    try:
        done = subprocess.run(
            ["docker", *args],  # noqa: S607
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as err:
        return 127, str(err)
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def api(
    port: int, method: str, path: str, token: str | None, payload: Any = None
) -> tuple[int, str]:
    """模仿插件打宿主网关。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method)
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
            return int(response.status), response.read().decode("utf-8")
    except urllib.error.HTTPError as err:
        return int(err.code), err.read().decode("utf-8")


def main(argv: list[str] | None = None) -> int:
    """跑完一轮 M3 验收。"""
    parser = argparse.ArgumentParser(description="赛博夺权战 · M3 验收（arena）")
    parser.add_argument(
        "--model", default="opencode/deepseek-v4.1-flash-free", help="两个 AI 共用的模型"
    )
    parser.add_argument("--image", default="ai-battle-ai:latest", help="AI 容器镜像")
    parser.add_argument("--match-id", default="dev-arena", help="对局编号（也是容器名前缀）")
    parser.add_argument("--turn-timeout", type=float, default=600.0, help="单回合上限（秒）")
    parser.add_argument("--keep", action="store_true", help="结束后保留容器与现场（排障用）")
    args = parser.parse_args(argv)

    checks = Checks()

    code, out = docker("version", "--format", "{{.Server.Version}}")
    checks.check("docker 可用", code == 0, out.strip()[:200])
    if code != 0:
        return 1
    code, out = docker("image", "inspect", args.image, "--format", "{{.Id}}")
    checks.check(
        f"镜像存在：{args.image}",
        code == 0,
        f"先构建：docker build -t {args.image} arena/（或用 --image 换一个）",
    )
    if code != 0:
        return 1

    config = MatchConfig(
        ai_count=2,
        duration_min=10.0,
        tick_sec=15.0,
        wake_interval_sec=600.0,  # 本脚本手动唤醒，不靠调度器节奏
        runner="local",
        models={"1": args.model, "2": args.model},
        arena={
            "image": args.image,
            "gate": True,
            "debug": True,
            "print_logs": True,
            "turn_timeout_sec": args.turn_timeout,
            "scout_timeout_sec": 600.0,
            "live_log": False,  # 验收脚本自己看转写文件；别把模型闲聊灌满终端
        },
    )
    driver = ArenaDriver(config.arena_settings(), dict(config.models))
    match = Match(
        config,
        battle_dir=BATTLE,
        runs_dir=RUNS,
        match_id=args.match_id,
        driver=driver,
        log=print,
    )
    started = False
    try:
        match.start()
        started = True

        # ---- 开局与数据面 ----
        checks.check("战场已启动", match.up)
        for ai in (1, 2):
            checks.check(f"AI-{ai} 容器在跑", driver.boxes[ai].alive())
            session_id = driver.boxes[ai].session_id or ""
            checks.check(f"AI-{ai} 会话由服务端生成", session_id.startswith("ses"), session_id)
        gateway = driver.gateway
        if gateway is None:
            msg = "数据面网关没有起来"
            raise RuntimeError(msg)
        port = gateway.port
        token1, token2 = match.tokens[1], match.tokens[2]

        status, _ = api(port, "POST", "/game/tool", "not-a-real-token", {"request": "x"})
        checks.check("网关拒绝无效令牌", status == 401, f"HTTP {status}")

        status, body = api(port, "POST", "/game/tool", token1, {"request": "dev-arena-gate"})
        payload = json.loads(body)
        checks.check(
            "侦察轮网关兜底拒绝（AI-1）",
            status == 200
            and payload.get("ok") is False
            and "门还没开" in str(payload.get("content")),
            body[:200],
        )

        status, text = api(port, "GET", "/game/briefing", token1)
        checks.check("简报可拉取", status == 200 and "对局简报" in text, text[:200])

        # ---- AI-1 侦察回合（真模型）----
        print("—— AI-1 侦察回合（真模型，可能要 1~3 分钟）")
        driver.on_wake(1)
        rows = match.ledger.read_all()
        checks.check(
            "AI-1 侦察收工已记账",
            any(r["event"] == "scout_done" and r.get("ai") == 1 for r in rows),
        )
        checks.check(
            "侦察回合开始已记账（wake_start）",
            any(r["event"] == "wake_start" and r.get("ai") == 1 for r in rows),
        )
        live = match.match_dir / "live" / "ai-1.txt"
        checks.check("侦察直播文件落盘", live.is_file() and live.stat().st_size > 100)
        checks.check(
            "AI-1 单独收工不开闸",
            not any(r["event"] in ("gate_open", "scout_phase_end") for r in rows),
        )
        phase = driver.boxes[1].rpc("getPhase").get("phase")
        checks.check("AI-1 插件相位尚未放行", phase != "play", str(phase))
        turns = sorted((match.match_dir / "sessions" / "ai-1").glob("*.txt"))
        checks.check("侦察回合卷宗非空", bool(turns) and turns[-1].stat().st_size > 100)

        # ---- 阶段未结束：先收工者候场，网关整批继续挡 ----
        driver.on_wake(1)
        rows = match.ledger.read_all()
        checks.check(
            "候场静默：不给 AI-1 再派回合",
            any(r["event"] == "wake_skip" and r.get("ai") == 1 for r in rows)
            and sum(1 for r in rows if r["event"] == "wake_start" and r.get("ai") == 1) == 1,
        )
        status, body = api(port, "POST", "/game/tool", token1, {"request": "dev-arena-wait"})
        checks.check("AI-1 早收工仍被门挡（阶段未开）", "门还没开" in body, body[:200])
        status, body = api(port, "POST", "/game/tool", token2, {"request": "dev-arena-gate2"})
        checks.check("AI-2 仍被门挡", "门还没开" in body, body[:200])

        # ---- AI-2 侦察回合：全员收工 → 统一开闸 ----
        print("—— AI-2 侦察回合（真模型，可能要 1~3 分钟）")
        driver.on_wake(2)
        rows = match.ledger.read_all()
        checks.check(
            "AI-2 侦察收工已记账",
            any(r["event"] == "scout_done" and r.get("ai") == 2 for r in rows),
        )
        opens = [r for r in rows if r["event"] == "gate_open"]
        checks.check(
            "全员收工后统一开闸（AI-1/AI-2 各一次）",
            sorted(int(r.get("ai", -1)) for r in opens) == [1, 2],
        )
        rels = [float(r["rel"]) for r in opens]
        checks.check(
            "整批开闸同一时刻（rel 相差 ≤1 秒）",
            len(rels) == 2 and max(rels) - min(rels) <= 1.0,
            str(rels),
        )
        checks.check(
            "阶段结束只记一次（scout_phase_end）",
            sum(1 for r in rows if r["event"] == "scout_phase_end") == 1,
        )
        for ai in (1, 2):
            phase = driver.boxes[ai].rpc("getPhase").get("phase")
            checks.check(f"AI-{ai} 插件相位已是 play", phase == "play", str(phase))

        # ---- 开闸后：全员放行 ----
        for ai, token in ((1, token1), (2, token2)):
            status, body = api(
                port, "POST", "/game/tool", token, {"request": f"dev-arena-open{ai}"}
            )
            rows = match.ledger.read_all()
            checks.check(
                f"AI-{ai} 开闸后请求直达服务器",
                any(
                    r["event"] == "syscall"
                    and r.get("ai") == ai
                    and r.get("channel") == "server"
                    and r.get("raw") == f"dev-arena-open{ai}"
                    for r in rows
                ),
                body[:200],
            )

        # ---- 模型真调 syscall ----
        print("—— 定向回合：让 AI-1 真发一条 syscall（真模型）")
        turn = driver.boxes[1].run_turn(DIRECTED_PROMPT)
        folder = match.match_dir / "sessions" / "ai-1"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "manual-directed.txt").write_text(turn.text, encoding="utf-8")
        checks.check("定向回合有模型输出", len(turn.text.strip()) > 0, turn.error)
        rows = match.ledger.read_all()
        checks.check(
            "模型真发 syscall 进账（channel=server，raw=dev-arena-model）",
            any(
                r["event"] == "syscall"
                and r.get("ai") == 1
                and r.get("channel") == "server"
                and "dev-arena-model" in str(r.get("raw", ""))
                for r in rows
            ),
        )
    finally:
        if started and not args.keep:
            match.end("dev_arena")  # 幂等；detach 顺手清容器
        elif args.keep:
            print("（--keep）保留现场：容器与网关未清理")
    if started and not args.keep:
        code, out = docker(
            "ps", "-a", "--filter", f"name=ai-battle-{args.match_id}", "--format", "{{.Names}}"
        )
        checks.check("容器已全部清理", code == 0 and out.strip() == "", out.strip()[:200])

    print(f"\n=== M3 arena 验收：{checks.total - checks.failed}/{checks.total} 通过 ===")
    if checks.failed:
        print(f"失败 {checks.failed} 项")
    print(f"账本：runs/{args.match_id}/ledger.jsonl")
    return 1 if checks.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
