"""直连正在跑的 AI 容器，实时跟读某位 AI 的会话输出（只读补救/调试工具）。

opencode v2 把会话消息（思考 / 正文 / 工具调用与结果）落进容器内 SQLite：

    /root/.local/share/opencode/opencode.db  （session_message 表）

`docker exec … opencode run` 的输出只流向对局内核进程，宿主机上的 docker 日志里
看不到；本工具绕开它，把一个只读轮询小程序喂给容器里的 `python3`，在本地终端
增量打印：

    uv run python scripts/watch_live.py ai-battle-20261017-184211-ai1

行为：

- 启动先折叠打印已有历史（工具调用带命令与结果摘要；思考只报字数）；
- 之后跟读增量：新工具调用实时出现，思考 / 正文按追加流式滚动；
- 纯只读（SQLite `mode=ro`），对局照跑不误；Ctrl+C 退出；
- `--once` 只打印历史后退出；`--seconds N` 跟读 N 秒后自动退出；
- `--no-thinking` 不打印思考增量。

容器名见账本 `runs/<match-id>/ledger.jsonl` 里的 `arena_up` 事件。
"""

from __future__ import annotations

import argparse
import subprocess
import sys

DB_PATH = "/root/.local/share/opencode/opencode.db"

PROBE = r'''
"""容器侧：只读轮询 session_message，把增量打印到 stdout。"""

import json
import sqlite3
import sys
import time

DB = @@DB@@
INTERVAL = @@INTERVAL@@
PRINT_THINKING = @@THINKING@@
ONCE = @@ONCE@@
MAX_SECONDS = @@MAX_SECONDS@@


def emit(text):
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def clip(text, limit):
    if len(text) <= limit:
        return text
    return text[:limit] + "…（+%d 字）" % (len(text) - limit)


def summarize_input(name, inp):
    if not isinstance(inp, dict) or not inp:
        return name
    if name in ("shell", "bash"):
        cmd = " ".join(str(inp.get("command", "")).split())
        return "$ " + clip(cmd, 240)
    path = inp.get("filePath") or inp.get("path") or inp.get("pattern") or ""
    if name == "write":
        return "write %s（%d 字符）" % (path, len(str(inp.get("content") or "")))
    if path:
        return "%s %s" % (name, path)
    try:
        raw = json.dumps(inp, ensure_ascii=False)
    except (TypeError, ValueError):
        raw = str(inp)
    return "%s %s" % (name, clip(raw, 160))


def summarize_output(state):
    parts = []
    content = state.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
    body = "\n".join(p for p in parts if p)
    if state.get("error"):
        body = (body + "\n" if body else "") + "[错误] " + str(state.get("error"))
    meta = state.get("metadata")
    if isinstance(meta, dict) and meta.get("truncated"):
        body += "\n（输出被截断）"
    return body


def stream_text(key, kind, body, active, first, seen):
    old = seen.get(key)
    if old is None:
        seen[key] = body
        if kind == "reasoning" and not PRINT_THINKING:
            return
        if kind == "text":
            emit("\n── AI ──")
        elif first and body and active:
            emit("\n── 思考（已 %d 字，继续跟）──" % len(body))
            return
        elif first and body:
            emit("\n── 思考（%d 字，折叠）──" % len(body))
            return
        else:
            emit("\n── 思考 ──")
        if body:
            sys.stdout.write(body)
            sys.stdout.flush()
        return
    if body == old:
        return
    if kind == "reasoning" and not PRINT_THINKING:
        seen[key] = body
        return
    if body.startswith(old):
        sys.stdout.write(body[len(old):])
        sys.stdout.flush()
    elif len(body) > 2000:
        emit("\n…（内容重写，共 %d 字，尾部预览）" % len(body))
        emit(body[-800:])
    else:
        emit("\n…（内容重写）")
        sys.stdout.write(body)
        sys.stdout.flush()
    seen[key] = body


def handle_assistant(seq, data, first, seen):
    time_info = data.get("time") if isinstance(data.get("time"), dict) else {}
    active = "completed" not in time_info
    for idx, item in enumerate(data.get("content") or []):
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind in ("reasoning", "text"):
            body = str(item.get("text") or "")
            stream_text((seq, idx, kind), kind, body, active, first, seen)
        elif kind == "tool":
            state = item.get("state") if isinstance(item.get("state"), dict) else {}
            inp = state.get("input") if isinstance(state.get("input"), dict) else {}
            status = str(state.get("status") or "")
            name = str(item.get("name") or "?")
            st = seen.get((seq, idx, "tool"))
            if not isinstance(st, dict):
                st = {"head": None, "done": False}
                seen[(seq, idx, "tool")] = st
            head = summarize_input(name, inp)
            if st["head"] is None and (inp or status not in ("", "pending", "running")):
                st["head"] = head
                emit("")
                emit("── 工具 · %s ──" % name)
                if head != name:
                    emit(head)
            if status not in ("", "pending", "running") and not st["done"]:
                st["done"] = True
                body = summarize_output(state)
                if body:
                    emit("  " + body.replace("\n", "\n  "))


def main():
    seen = {}
    first = True
    started = time.time()
    con = None
    while True:
        if con is None:
            try:
                con = sqlite3.connect("file:" + DB + "?mode=ro", uri=True)
                con.execute("pragma query_only = 1")
            except sqlite3.Error as exc:
                emit("[watch] 打开数据库失败：%s" % exc)
                return 2
        try:
            sql = "select seq, type, data from session_message order by seq"
            rows = con.execute(sql).fetchall()
        except sqlite3.Error:
            try:
                con.close()
            except sqlite3.Error:
                pass
            con = None
            rows = []
        for seq, typ, data in rows:
            if typ == "agent-switched":
                continue
            try:
                d = json.loads(data)
            except ValueError:
                continue
            if typ == "user":
                key = (seq, "user")
                if key not in seen:
                    seen[key] = True
                    emit("")
                    emit("── 用户 ──")
                    emit(clip(str(d.get("text") or ""), 500))
            elif typ == "assistant":
                handle_assistant(seq, d, first, seen)
        first = False
        if ONCE:
            return 0
        if MAX_SECONDS and time.time() - started >= MAX_SECONDS:
            return 0
        time.sleep(INTERVAL)


raise SystemExit(main())
'''


def build_probe(*, interval: float, thinking: bool, once: bool, max_seconds: float) -> str:
    """把参数注入容器侧轮询程序。"""
    return (
        PROBE.replace("@@DB@@", repr(DB_PATH))
        .replace("@@INTERVAL@@", repr(interval))
        .replace("@@THINKING@@", repr(thinking))
        .replace("@@ONCE@@", repr(once))
        .replace("@@MAX_SECONDS@@", repr(max_seconds))
    )


def force_utf8() -> None:
    """把标准输出/错误固定为 UTF-8（控制台与重定向行为一致）。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。"""
    force_utf8()
    parser = argparse.ArgumentParser(
        description="只读跟读 AI 容器里的会话输出（思考/正文/工具调用）。"
    )
    parser.add_argument("container", help="AI 容器名，如 ai-battle-20261017-184211-ai1")
    parser.add_argument("--interval", type=float, default=1.0, help="轮询间隔秒数（默认 1.0）")
    parser.add_argument("--once", action="store_true", help="只打印已有历史后退出")
    parser.add_argument(
        "--seconds", type=float, default=0.0, help="跟读指定秒数后退出（默认一直跟）"
    )
    parser.add_argument("--no-thinking", action="store_true", help="不打印思考增量")
    args = parser.parse_args(argv)

    probe = build_probe(
        interval=args.interval,
        thinking=not args.no_thinking,
        once=args.once,
        max_seconds=args.seconds,
    )
    cmd = ["docker", "exec", "-i", "-e", "PYTHONUNBUFFERED=1", args.container, "python3", "-"]
    print(f"# 直播 · {args.container}")
    print("# 只读跟随容器内会话" + ("（打印历史后退出）" if args.once else "；Ctrl+C 退出"))
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        print("命令未找到：docker", file=sys.stderr)
        return 2

    interrupted = False
    try:
        assert proc.stdin is not None
        assert proc.stdout is not None
        proc.stdin.write(probe)
        proc.stdin.close()
        try:
            for line in proc.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
        except KeyboardInterrupt:
            interrupted = True
            print("\n# 已退出。")
        except BrokenPipeError:
            interrupted = True
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    code = proc.returncode
    if not interrupted and code:
        print(f"# docker exec 退出码 {code}（容器名对吗？）", file=sys.stderr)
        return code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
