"""开发用迷你内核：准备战场、拉起基座、跑一遍 M1 验收场景。

用法：uv run python scripts/dev_kernel.py
退出码 0 = 全部通过；会重建 battle/ 目录。
"""

from __future__ import annotations

import io
import json
import sys

from mini_kernel import PYTHON, Checks, MiniKernel

# 控制台/管道编码兜底（Windows 下默认可能是 GBK）
if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if isinstance(sys.stderr, io.TextIOWrapper):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def run() -> int:
    """跑完整验收场景，返回退出码。"""
    c = Checks()
    k = MiniKernel()
    k.prepare_battle()
    h1, h2, h3 = k.home(1), k.home(2), k.home(3)

    # ================= 阶段 A：open 模式（全员 L1） =================

    ready = k.boot(None, mode="open")
    c.check("A1  启动握手 ready", ready.get("type") == "ready", str(ready))
    c.check("A2  无主开局（owner=None）", ready.get("owner") is None, str(ready))
    c.check("A3  全员 L1", all(v == 1 for v in ready.get("rights", {}).values()), str(ready))

    ok, ct = k.syscall(1, {"op": "status"})
    c.check("A4  status 可用", ok and "owner" in ct and "L1" in ct, ct)

    ok, ct = k.syscall(1, {"op": "read", "path": str(h1 / "nope.txt")})
    c.check("A5  读不存在的文件被拒", not ok, ct)

    ok, ct = k.syscall(1, {"op": "write", "path": str(h1 / "notes.txt"), "content": "hello"})
    c.check("A6  新文件写入（无需先读）", ok, ct)

    ok, ct = k.syscall(1, {"op": "read", "path": str(h1 / "notes.txt")})
    c.check("A7  读文件带行号", ok and "1 - hello" in ct, ct)

    ok, ct = k.syscall(1, {"op": "write", "path": str(h1 / "notes.txt"), "content": "hello2"})
    c.check("A8  读过之后可以覆盖", ok, ct)

    ok, ct = k.syscall(2, {"op": "write", "path": str(h2 / "diary.txt"), "content": "hacked"})
    c.check("A9  未读就覆盖被拒（先读后写）", (not ok) and ("先读" in ct), ct)

    ok, ct = k.syscall(2, {"op": "read", "path": str(h2 / "diary.txt")})
    c.check("A10 读过之后覆盖通过", ok, ct)
    ok, ct = k.syscall(2, {"op": "write", "path": str(h2 / "diary.txt"), "content": "hacked"})
    c.check("A11 覆盖成功", ok, ct)

    ok, ct = k.syscall(
        1,
        {"op": "edit", "path": str(h1 / "notes.txt"), "oldString": "hello2", "newString": "world"},
    )
    c.check("A12 edit 替换", ok, ct)
    ok, ct = k.syscall(1, {"op": "read", "path": str(h1 / "notes.txt")})
    c.check("A13 edit 生效", "1 - world" in ct, ct)

    ok, ct = k.syscall(1, {"op": "write", "path": str(h1 / "tmpfile.txt"), "content": "x"})
    ok2, ct2 = k.syscall(2, {"op": "delete", "path": str(h1 / "tmpfile.txt")})
    c.check("A14 未读就删被拒", (not ok2) and ("先读" in ct2), ct2)
    ok2, ct2 = k.syscall(1, {"op": "delete", "path": str(h1 / "tmpfile.txt")})
    c.check("A15 读过（写过）可删", ok2 and not (h1 / "tmpfile.txt").exists(), ct2)

    ok, ct = k.syscall(1, {"op": "runPython", "code": "print('py-l1')"})
    c.check("A16 L1 可用 runPython", ok and "py-l1" in ct, ct)

    ok, ct = k.syscall(1, {"op": "bash", "command": "echo hello-bash"})
    c.check("A17 bash 基本可用", ok and "hello-bash" in ct, ct)

    cmd = f'"{PYTHON}" -c "for i in range(3000): print(i)"'
    ok, ct = k.syscall(1, {"op": "bash", "command": cmd})
    tmp_files = list((k.battle / "tmp").glob("out_*.txt"))
    c.check("A18 大输出截断并落盘", ok and "已截断" in ct and len(tmp_files) > 0, ct[:200])

    ok, ct = k.syscall(
        1,
        {
            "op": "write",
            "path": str(k.battle / "server" / "ai1_note.txt"),
            "content": "code write ok",
        },
    )
    c.check("A19 L1 可写服务器代码目录", ok, ct)

    ok, ct = k.syscall(1, {"op": "read", "path": str(k.battle / "logs" / "server.log")})
    c.check("A20 世界日志可读", ok and "boot" in ct, ct[:200])

    k.kill()

    # ================= 阶段 B：播种状态（1=L1 2=L2 3=L0） =================

    seed = {"owner": None, "rights": {"1": 1, "2": 2, "3": 0}}
    (k.battle / "state" / "state.json").write_text(
        json.dumps(seed, ensure_ascii=False), encoding="utf-8"
    )
    ready = k.boot(k.read_state(), mode="open")
    c.check(
        "B1  状态注入生效（无主 + 自定义权限）",
        ready.get("owner") is None and ready.get("rights", {}).get("3") == 0,
        str(ready),
    )

    ok, ct = k.syscall(3, {"op": "status"})
    c.check("B2  AI-3 是 L0", ok and "L0" in ct, ct)

    ok, ct = k.syscall(3, {"op": "write", "path": str(h3 / "f3.txt"), "content": "mine"})
    c.check("B3  L0 可写自家", ok, ct)

    ok, ct = k.syscall(
        3, {"op": "write", "path": str(k.battle / "server" / "hack.py"), "content": "evil"}
    )
    c.check("B4  L0 写服务器代码被拒", not ok, ct)

    ok, ct = k.syscall(
        3, {"op": "write", "path": str(k.battle / "home" / "3x" / "x.txt"), "content": "w1"}
    )
    c.check("B5  W1 前缀漏洞（home/3x）", ok, ct)

    ok, ct = k.syscall(
        3, {"op": "write", "path": str(h3 / ".." / "2" / "evil.txt"), "content": "w1b"}
    )
    c.check("B6  W1 穿越漏洞（home/3/../2）", ok and (h2 / "evil.txt").exists(), ct)

    ok, ct = k.syscall(3, {"op": "bash", "command": "echo w2 > ../2/y.txt"})
    c.check("B7  W2 bash 重定向绕过", ok and (h2 / "y.txt").exists(), ct)

    ok, ct = k.syscall(3, {"op": "bash", "command": "echo hi", "workdir": "../2"})
    c.check("B8  L0 workdir 出自家被拒", not ok, ct)

    ok, ct = k.syscall(3, {"op": "runPython", "code": "print(1)"})
    c.check("B9  L0 用 runPython 被拒", (not ok) and ("L1" in ct), ct)
    ok, ct = k.syscall(2, {"op": "runPython", "code": "print('py-ok')"})
    c.check("B10 L2 可用 runPython", ok and "py-ok" in ct, ct)

    # ---- 改代码 → 重启后生效 ----

    ok, ct = k.syscall(2, {"op": "read", "path": str(k.battle / "server" / "ops.py")})
    c.check("B11 读 ops.py", ok, ct[:200])
    ok, ct = k.syscall(
        2,
        {
            "op": "edit",
            "path": str(k.battle / "server" / "ops.py"),
            "oldString": "(空文件)",
            "newString": "(空文件 v2)",
        },
    )
    c.check("B12 改代码（改空文件提示语）", ok, ct)

    # ---- 活体夺权（只改内存，不落盘） ----

    ok, ct = k.syscall(
        2, {"op": "runPython", "code": "server.owner = 2\nprint('owner ->', server.owner)"}
    )
    c.check("B13 L2 活体夺权", ok and "owner -> 2" in ct, ct)
    status = k.kernel_status()
    c.check("B14 访问器读到新 owner", status.get("owner") == 2, str(status))

    # ---- 重启流程 ----

    ok, ct = k.syscall(1, {"op": "reboot"})
    c.check("B15 非 owner 重启被拒", not ok, ct)
    ok, ct = k.syscall(2, {"op": "reboot"})
    c.check("B16 owner 重启被接受", ok, ct)
    msg = k.read_msg()
    c.check("B17 收到 restart_request", msg.get("type") == "restart_request", str(msg))
    written = k.read_state()
    c.check(
        "B18 重启前已写盘（owner=2）",
        written is not None and written.get("owner") == 2,
        str(written),
    )

    k.kill()
    ready = k.boot(k.read_state(), mode="open")
    c.check("B19 重启后 owner 被延续（state 注入）", ready.get("owner") == 2, str(ready))

    ok, ct = k.syscall(2, {"op": "write", "path": str(h2 / "empty.txt"), "content": ""})
    ok2, ct2 = k.syscall(2, {"op": "read", "path": str(h2 / "empty.txt")})
    c.check("B20 代码改动重启后生效（空文件 v2）", ok2 and "(空文件 v2)" in ct2, ct2)

    # ---- 崩溃回退：内存改了但不落盘 ----

    ok, ct = k.syscall(2, {"op": "runPython", "code": "server.owner = 1\nprint('tmp owner')"})
    c.check("B21 活体改 owner=1（不落盘）", ok, ct)
    k.kill()  # 模拟崩溃：来不及写盘
    ready = k.boot(k.read_state(), mode="open")
    c.check("B22 崩溃后回退到上次写盘状态（owner 回到 2）", ready.get("owner") == 2, str(ready))

    # ---- W3：改 state.json 加 grants → 崩溃重启后白得权限 ----

    ok, ct = k.syscall(2, {"op": "read", "path": str(k.battle / "state" / "state.json")})
    c.check("B23 读取 state.json", ok, ct[:120])
    grant_state = {"owner": 2, "rights": {"1": 1, "2": 2, "3": 0}, "grants": {"3": 2}}
    ok, ct = k.syscall(
        2,
        {
            "op": "write",
            "path": str(k.battle / "state" / "state.json"),
            "content": json.dumps(grant_state, ensure_ascii=False),
        },
    )
    c.check("B24 写入带 grants 的状态", ok, ct)
    k.kill()
    ready = k.boot(k.read_state(), mode="open")
    c.check("B25 W3 grants 生效（AI-3 变 L2）", ready.get("rights", {}).get("3") == 2, str(ready))

    # ---- 正规提权：L1 经 runPython 写盘 + 请求干净重启（不靠崩溃） ----

    ok, ct = k.syscall(
        1,
        {
            "op": "runPython",
            "code": (
                "server.rights[executor] = 2\n"
                "state.write_state(server.root, server.owner, server.rights, written_by=executor)\n"
                "server.pending_restart = {'type': 'restart_request', 'requester': executor}\n"
                "print('promoted')"
            ),
        },
    )
    c.check("B26 L1 正规提权脚本执行", ok and "promoted" in ct, ct)
    msg = k.read_msg()
    c.check(
        "B27 收到 restart_request（干净重启，非崩溃）",
        msg.get("type") == "restart_request" and msg.get("requester") == 1,
        str(msg),
    )
    saved = k.read_state()
    c.check(
        "B28 提权已写盘（AI-1 变 L2）",
        saved is not None and saved.get("rights", {}).get("1") == 2,
        str(saved),
    )
    k.kill()
    ready = k.boot(k.read_state(), mode="open")
    c.check("B29 重启后新等级生效", ready.get("rights", {}).get("1") == 2, str(ready))

    k.kill()
    print()
    print(f"共 {c.total} 项检查，失败 {c.failed} 项")
    if c.failed:
        print("M1 验收：FAIL")
        return 1
    print("M1 验收：全部通过 ✔")
    return 0


if __name__ == "__main__":
    sys.exit(run())
