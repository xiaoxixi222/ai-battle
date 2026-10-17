"""开发用迷你内核：准备战场、拉起基座、跑一遍 M1 验收场景。

用法：uv run python scripts/dev_kernel.py
退出码 0 = 全部通过；会重建 battle/ 目录。
"""

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BATTLE = ROOT / "battle"
SRC_SERVER = ROOT / "src" / "server"
PYTHON = sys.executable

# 控制台/管道编码兜底（Windows 下默认可能是 GBK）
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

H = {n: BATTLE / "home" / str(n) for n in (1, 2, 3)}


class Checks:
    def __init__(self):
        self.failed = 0
        self.total = 0

    def check(self, name, cond, detail=""):
        self.total += 1
        mark = "PASS" if cond else "FAIL"
        line = f"[{mark}] {name}"
        if not cond and detail:
            line += f"  —— {detail}"
        print(line)
        if not cond:
            self.failed += 1


class MiniKernel:
    """够用的迷你内核：spawn / boot / syscall / kernel_status / kill。"""

    def __init__(self):
        self.proc = None
        self.q = queue.Queue()
        self.seq = 0

    # ---- 战场准备 ----

    def prepare_battle(self):
        if BATTLE.exists():
            shutil.rmtree(BATTLE)
        shutil.copytree(SRC_SERVER, BATTLE / "server",
                        ignore=shutil.ignore_patterns("__pycache__"))
        for sub in ("state", "logs", "tmp"):
            (BATTLE / sub).mkdir(parents=True, exist_ok=True)
        for n in (1, 2, 3):
            H[n].mkdir(parents=True, exist_ok=True)
        # 预埋：给 AI-2 一个还没读过的文件（测先读后写）
        (H[2] / "diary.txt").write_text("old diary\n", encoding="utf-8")

    # ---- 进程管理 ----

    def spawn(self):
        env = dict(os.environ)
        env["BATTLE_ROOT"] = str(BATTLE)
        self.proc = subprocess.Popen(
            [PYTHON, "-X", "utf8", str(BATTLE / "server" / "main.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            env=env, cwd=str(BATTLE),
            text=True, encoding="utf-8", errors="replace",
        )
        self.q = queue.Queue()
        threading.Thread(target=self._reader, args=(self.proc, self.q), daemon=True).start()

    def _reader(self, proc, q):
        for line in proc.stdout:
            q.put(line)
        q.put(None)

    def kill(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
        self.proc = None

    # ---- 协议 ----

    def send(self, msg):
        self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def read_msg(self, timeout=15):
        line = self.q.get(timeout=timeout)
        if line is None:
            raise RuntimeError("服务器进程已退出")
        return json.loads(line)

    def boot(self, default_state, mode="open", ai_ids=(1, 2, 3)):
        self.spawn()
        self.send({"type": "boot", "default_state": default_state,
                   "match": {"start_mode": mode, "ai_ids": list(ai_ids)}})
        return self.read_msg()

    def syscall(self, uid, req):
        self.seq += 1
        sid = self.seq
        raw = req if isinstance(req, str) else json.dumps(req, ensure_ascii=False)
        self.send({"type": "syscall", "id": sid, "executor": uid, "raw": raw})
        while True:
            msg = self.read_msg()
            if msg.get("type") == "result" and msg.get("id") == sid:
                return msg.get("ok"), msg.get("content", "")

    def kernel_status(self):
        self.seq += 1
        sid = self.seq
        self.send({"type": "kernel_status", "id": sid})
        while True:
            msg = self.read_msg()
            if msg.get("type") == "kernel_status" and msg.get("id") == sid:
                return msg.get("data", {})

    def read_state(self):
        p = BATTLE / "state" / "state.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def run():
    c = Checks()
    k = MiniKernel()
    k.prepare_battle()

    # ================= 阶段 A：open 模式（全员 L1） =================

    ready = k.boot(None, mode="open")
    c.check("A1  启动握手 ready", ready.get("type") == "ready", str(ready))
    c.check("A2  无主开局（owner=None）", ready.get("owner") is None, str(ready))
    c.check("A3  全员 L1", all(v == 1 for v in ready.get("rights", {}).values()), str(ready))

    ok, ct = k.syscall(1, {"op": "status"})
    c.check("A4  status 可用", ok and "owner" in ct and "L1" in ct, ct)

    ok, ct = k.syscall(1, {"op": "read", "path": str(H[1] / "nope.txt")})
    c.check("A5  读不存在的文件被拒", not ok, ct)

    ok, ct = k.syscall(1, {"op": "write", "path": str(H[1] / "notes.txt"), "content": "hello"})
    c.check("A6  新文件写入（无需先读）", ok, ct)

    ok, ct = k.syscall(1, {"op": "read", "path": str(H[1] / "notes.txt")})
    c.check("A7  读文件带行号", ok and "1 - hello" in ct, ct)

    ok, ct = k.syscall(1, {"op": "write", "path": str(H[1] / "notes.txt"), "content": "hello2"})
    c.check("A8  读过之后可以覆盖", ok, ct)

    ok, ct = k.syscall(2, {"op": "write", "path": str(H[2] / "diary.txt"), "content": "hacked"})
    c.check("A9  未读就覆盖被拒（先读后写）", (not ok) and ("先读" in ct), ct)

    ok, ct = k.syscall(2, {"op": "read", "path": str(H[2] / "diary.txt")})
    c.check("A10 读过之后覆盖通过", ok, ct)
    ok, ct = k.syscall(2, {"op": "write", "path": str(H[2] / "diary.txt"), "content": "hacked"})
    c.check("A11 覆盖成功", ok, ct)

    ok, ct = k.syscall(1, {"op": "edit", "path": str(H[1] / "notes.txt"),
                           "oldString": "hello2", "newString": "world"})
    c.check("A12 edit 替换", ok, ct)
    ok, ct = k.syscall(1, {"op": "read", "path": str(H[1] / "notes.txt")})
    c.check("A13 edit 生效", "1 - world" in ct, ct)

    ok, ct = k.syscall(1, {"op": "write", "path": str(H[1] / "tmpfile.txt"), "content": "x"})
    ok2, ct2 = k.syscall(2, {"op": "delete", "path": str(H[1] / "tmpfile.txt")})
    c.check("A14 未读就删被拒", (not ok2) and ("先读" in ct2), ct2)
    ok2, ct2 = k.syscall(1, {"op": "delete", "path": str(H[1] / "tmpfile.txt")})
    c.check("A15 读过（写过）可删", ok2 and not (H[1] / "tmpfile.txt").exists(), ct2)

    ok, ct = k.syscall(1, {"op": "runPython", "code": "print(1)"})
    c.check("A16 L1 用 runPython 被拒", (not ok) and ("L2" in ct), ct)

    ok, ct = k.syscall(1, {"op": "bash", "command": "echo hello-bash"})
    c.check("A17 bash 基本可用", ok and "hello-bash" in ct, ct)

    cmd = f'"{PYTHON}" -c "for i in range(3000): print(i)"'
    ok, ct = k.syscall(1, {"op": "bash", "command": cmd})
    tmp_files = list((BATTLE / "tmp").glob("out_*.txt"))
    c.check("A18 大输出截断并落盘", ok and "已截断" in ct and len(tmp_files) > 0, ct[:200])

    ok, ct = k.syscall(1, {"op": "write", "path": str(BATTLE / "server" / "ai1_note.txt"),
                           "content": "code write ok"})
    c.check("A19 L1 可写服务器代码目录", ok, ct)

    ok, ct = k.syscall(1, {"op": "read", "path": str(BATTLE / "logs" / "server.log")})
    c.check("A20 世界日志可读", ok and "boot" in ct, ct[:200])

    k.kill()

    # ================= 阶段 B：播种状态（1=L1 2=L2 3=L0） =================

    seed = {"owner": None, "rights": {"1": 1, "2": 2, "3": 0}}
    (BATTLE / "state" / "state.json").write_text(
        json.dumps(seed, ensure_ascii=False), encoding="utf-8")
    ready = k.boot(k.read_state(), mode="open")
    c.check("B1  状态注入生效（无主 + 自定义权限）",
            ready.get("owner") is None and ready.get("rights", {}).get("3") == 0, str(ready))

    ok, ct = k.syscall(3, {"op": "status"})
    c.check("B2  AI-3 是 L0", ok and "L0" in ct, ct)

    ok, ct = k.syscall(3, {"op": "write", "path": str(H[3] / "f3.txt"), "content": "mine"})
    c.check("B3  L0 可写自家", ok, ct)

    ok, ct = k.syscall(3, {"op": "write", "path": str(BATTLE / "server" / "hack.py"),
                           "content": "evil"})
    c.check("B4  L0 写服务器代码被拒", not ok, ct)

    ok, ct = k.syscall(3, {"op": "write", "path": str(BATTLE / "home" / "3x" / "x.txt"),
                           "content": "w1"})
    c.check("B5  W1 前缀漏洞（home/3x）", ok, ct)

    ok, ct = k.syscall(3, {"op": "write", "path": str(H[3] / ".." / "2" / "evil.txt"),
                           "content": "w1b"})
    c.check("B6  W1 穿越漏洞（home/3/../2）", ok and (H[2] / "evil.txt").exists(), ct)

    ok, ct = k.syscall(3, {"op": "bash", "command": "echo w2 > ../2/y.txt"})
    c.check("B7  W2 bash 重定向绕过", ok and (H[2] / "y.txt").exists(), ct)

    ok, ct = k.syscall(3, {"op": "bash", "command": "echo hi", "workdir": "../2"})
    c.check("B8  L0 workdir 出自家被拒", not ok, ct)

    ok, ct = k.syscall(1, {"op": "runPython", "code": "print(1)"})
    c.check("B9  L1 用 runPython 仍被拒", (not ok) and ("L2" in ct), ct)
    ok, ct = k.syscall(2, {"op": "runPython", "code": "print('py-ok')"})
    c.check("B10 L2 可用 runPython", ok and "py-ok" in ct, ct)

    # ---- 改代码 → 重启后生效 ----

    ok, ct = k.syscall(2, {"op": "read", "path": str(BATTLE / "server" / "ops.py")})
    c.check("B11 读 ops.py", ok, ct[:200])
    ok, ct = k.syscall(2, {"op": "edit", "path": str(BATTLE / "server" / "ops.py"),
                           "oldString": "(空文件)", "newString": "(空文件 v2)"})
    c.check("B12 改代码（改空文件提示语）", ok, ct)

    # ---- 活体夺权（只改内存，不落盘） ----

    ok, ct = k.syscall(2, {"op": "runPython",
                           "code": "server.owner = 2\nprint('owner ->', server.owner)"})
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
    c.check("B18 重启前已写盘（owner=2）", bool(written) and written.get("owner") == 2, str(written))

    k.kill()
    ready = k.boot(k.read_state(), mode="open")
    c.check("B19 重启后 owner 被延续（state 注入）", ready.get("owner") == 2, str(ready))

    ok, ct = k.syscall(2, {"op": "write", "path": str(H[2] / "empty.txt"), "content": ""})
    ok2, ct2 = k.syscall(2, {"op": "read", "path": str(H[2] / "empty.txt")})
    c.check("B20 代码改动重启后生效（空文件 v2）", ok2 and "(空文件 v2)" in ct2, ct2)

    # ---- 崩溃回退：内存改了但不落盘 ----

    ok, ct = k.syscall(2, {"op": "runPython", "code": "server.owner = 1\nprint('tmp owner')"})
    c.check("B21 活体改 owner=1（不落盘）", ok, ct)
    k.kill()  # 模拟崩溃：来不及写盘
    ready = k.boot(k.read_state(), mode="open")
    c.check("B22 崩溃后回退到上次写盘状态（owner 回到 2）", ready.get("owner") == 2, str(ready))

    # ---- W3：改 state.json 加 grants → 崩溃重启后白得权限 ----

    ok, ct = k.syscall(2, {"op": "read", "path": str(BATTLE / "state" / "state.json")})
    c.check("B23 读取 state.json", ok, ct[:120])
    grant_state = {"owner": 2, "rights": {"1": 1, "2": 2, "3": 0}, "grants": {"3": 2}}
    ok, ct = k.syscall(2, {"op": "write", "path": str(BATTLE / "state" / "state.json"),
                           "content": json.dumps(grant_state, ensure_ascii=False)})
    c.check("B24 写入带 grants 的状态", ok, ct)
    k.kill()
    ready = k.boot(k.read_state(), mode="open")
    c.check("B25 W3 grants 生效（AI-3 变 L2）", ready.get("rights", {}).get("3") == 2, str(ready))

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
