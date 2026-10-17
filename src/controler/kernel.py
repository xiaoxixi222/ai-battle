"""内核入口：加载配置、组装对局、跑主循环，最后打印摘要。

用法示例：

    uv run python src/controler/kernel.py --mock tour --duration-min 2
    uv run python src/controler/kernel.py --config match.json --runner docker
    uv run python src/controler/kernel.py --arena --model opencode/deepseek-v4.1-flash-free

诊断输出走 stderr，stdout 只留终局摘要（JSON）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import matchconfig
import mockai
from arena import ArenaDriver
from matchconfig import MatchConfig
from orchestrator import Driver, Match


def build_parser() -> argparse.ArgumentParser:
    """命令行参数表。"""
    parser = argparse.ArgumentParser(description="赛博夺权战 · 内核")
    parser.add_argument("--config", help="match.json 路径（缺省用内置默认值）")
    scenarios = ", ".join(sorted(mockai.SCENARIOS))
    parser.add_argument("--mock", help=f"mock 情景名（可选：{scenarios}）")
    parser.add_argument(
        "--arena", action="store_true", help="用 OpenCode 容器驱动 AI（M3；需要 docker）"
    )
    parser.add_argument("--model", help="覆盖所有 AI 的模型（provider/id[#variant]）")
    parser.add_argument("--match-id", help="对局编号（缺省为时间戳）")
    parser.add_argument("--battle", help="战场目录（缺省 battle/)")
    parser.add_argument("--runs", help="账本目录（缺省 runs/）")
    parser.add_argument("--runner", choices=["local", "docker"], help="覆盖配置里的运行器")
    parser.add_argument("--duration-min", type=float, help="覆盖对局时长（分钟）")
    parser.add_argument("--tick-sec", type=float, help="覆盖心跳间隔（秒）")
    parser.add_argument("--wake-sec", type=float, help="覆盖唤醒间隔（秒）")
    return parser


def main(argv: list[str] | None = None) -> int:
    """组装并跑完一场对局。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    config = matchconfig.load(args.config) if args.config else MatchConfig()
    if args.runner:
        config.runner = args.runner
    if args.duration_min:
        config.duration_min = args.duration_min
    if args.tick_sec:
        config.tick_sec = args.tick_sec
    if args.wake_sec:
        config.wake_interval_sec = args.wake_sec
    if args.model:
        config.models = {str(ai): args.model for ai in config.ai_ids()}
    config.validate()
    battle = Path(args.battle) if args.battle else matchconfig.DEFAULT_BATTLE
    runs = Path(args.runs) if args.runs else matchconfig.DEFAULT_RUNS
    match_id = args.match_id or time.strftime("%Y%m%d-%H%M%S")
    driver: Driver | None = None
    if args.arena and args.mock:
        parser.error("--arena 与 --mock 不能同时用")
    if args.arena:
        driver = ArenaDriver(config.arena_settings(), dict(config.models))
    elif args.mock:
        driver = mockai.make_driver(args.mock)
    match = Match(
        config,
        battle_dir=battle,
        runs_dir=runs,
        match_id=match_id,
        driver=driver,
        log=lambda msg: print(msg, file=sys.stderr),
    )
    match.start()
    match.run()
    print(json.dumps(match.summary(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
