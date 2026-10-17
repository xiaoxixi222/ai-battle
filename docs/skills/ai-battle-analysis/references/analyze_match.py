"""对局速览：一次打印一场对局的关键事实，供人再深挖。

用法（仓库根目录）：
    uv run python -X utf8 docs/skills/ai-battle-analysis/references/analyze_match.py runs/<对局目录>

输出四段：
1. 对局概况（出处：ledger 的 match_start / end 行）
2. owner / 比分 / 重启 关键事件时间线
3. 每 AI 的 syscall 统计：成功 / busy门 / 宕机 / 拒收
4. 回合卷宗清单（sessions/ai-N/*.txt，首行即 kind/ok 结果头）

纯标准库、全程只读；坏行跳过，不给观战添堵。
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

KEY_EVENTS = {
    "owner_change",
    "score",
    "crash",
    "restart",
    "spawn",
    "boot_ok",
    "boot_fail",
    "rollback",
    "rollback_fail",
    "recovery_enter",
    "recovery_commit",
    "recovery_exit",
    "full_reset",
    "end",
}


def load_rows(match_dir: Path) -> list[dict]:
    """读账本全部行；坏行跳过。"""
    ledger = match_dir / "ledger.jsonl"
    if not ledger.exists():
        sys.exit(f"没有账本：{ledger}")
    rows: list[dict] = []
    for line in ledger.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def describe(row: dict) -> str:
    """关键事件的存档字段摘要；认不得的事件给通用 k=v。"""
    event = row.get("event")
    if event == "owner_change":
        return f"{row.get('old')} -> {row.get('new')}（{row.get('reason')}）"
    if event == "score":
        return f"delta={row.get('delta')} reason={row.get('reason')} balance={row.get('balance')}"
    if event == "crash":
        return f"reason={row.get('reason')} penalty={row.get('penalty')}"
    if event == "restart":
        return f"requester={row.get('requester')}"
    if event == "boot_ok":
        return f"commit={row.get('commit')} owner={row.get('owner')} rights={row.get('rights')}"
    if event == "boot_fail":
        return f"reason={row.get('reason')} error={row.get('error')}"
    if event == "rollback":
        return f"ref={row.get('ref')}"
    if event == "recovery_enter":
        return f"owner={row.get('owner')} timeout={row.get('timeout_s')}s"
    if event == "recovery_exit":
        return f"outcome={row.get('outcome')} reason={row.get('reason')}"
    if event == "end":
        return f"reason={row.get('reason')} owner={row.get('owner')} scores={row.get('scores')}"
    parts = []
    for key, value in row.items():
        if key in {"t", "rel", "event"} or value is None:
            continue
        parts.append(f"{key}={str(value)[:40]}")
    return "  ".join(parts[:3])


def main(match_dir: Path) -> None:
    rows = load_rows(match_dir)
    start = next((r for r in rows if r.get("event") == "match_start"), None)
    end = next((r for r in rows if r.get("event") == "end"), None)

    print("== 对局概况 ==")
    if start:
        print(
            f"ai_ids={start.get('ai_ids')}  mode={start.get('start_mode')}  "
            f"duration={start.get('duration_min')}min  runner={start.get('runner')}  "
            f"tick={start.get('tick_sec')}s  parallel={start.get('parallel_turns')}"
        )
    if end:
        print(f"终局: reason={end.get('reason')}  owner={end.get('owner')}  scores={end.get('scores')}")

    print("\n== 关键事件线 ==")
    for row in rows:
        event = row.get("event")
        if event in KEY_EVENTS:
            rel = row.get("rel")
            print(f"T+{rel:7.1f}  {event:<14} ai={row.get('ai')!s:<3} {describe(row)}")

    print("\n== 每 AI syscall 统计（成功 / busy门 / 宕机 / 拒收）==")
    tally: dict[int, Counter] = {}
    for row in rows:
        if row.get("event") != "syscall":
            continue
        ai = row.get("ai")
        if ai is None:
            continue
        counter = tally.setdefault(int(ai), Counter())
        preview = str(row.get("preview") or "")
        if row.get("ok") is True:
            counter["成功"] += 1
        elif row.get("channel") == "gate" or preview.strip().startswith("busy"):
            counter["busy门"] += 1
        elif row.get("channel") == "down":
            counter["宕机"] += 1
        else:
            counter["拒收"] += 1
    if tally:
        for ai in sorted(tally):
            counter = tally[ai]
            parts = "  ".join(f"{k}={v}" for k, v in counter.items())
            print(f"  AI-{ai}: {parts}")
    else:
        print("  （无 syscall 记录）")

    print("\n== 回合卷宗 ==")
    sessions = match_dir / "sessions"
    if sessions.exists():
        for folder in sorted(sessions.iterdir()):
            if not folder.is_dir():
                continue
            for file in sorted(folder.glob("*.txt")):
                lines = file.read_text(encoding="utf-8", errors="replace").splitlines()
                head = lines[0].strip() if lines else ""
                print(f"  {folder.name}/{file.name}: {head}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(Path(sys.argv[1]).expanduser().resolve())