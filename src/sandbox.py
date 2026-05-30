#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sandboxie Plus 完整控制模块
支持：创建/删除沙盒、启动沙盒内程序（带管道通信）、终止程序、管理多个沙盒实例
"""

import subprocess
import os
import time
import threading
from typing import Optional, Dict, List, Tuple, Any


class SandboxieController:
    """
    Sandboxie Plus 核心控制器
    封装 Start.exe 和 SbieIni.exe 的命令行接口
    """

    # 默认安装路径（根据你的实际安装位置修改）
    DEFAULT_START_EXE = r"D:\local_program\Sandboxie-Plus\Start.exe"
    DEFAULT_SBIEINI_EXE = r"D:\local_program\Sandboxie-Plus\SbieIni.exe"

    def __init__(self, start_exe_path: str = None, sbieini_exe_path: str = None, sandbox_name: str = "DefaultBox"):
        self.start_exe_path = start_exe_path or self.DEFAULT_START_EXE
        self.sbieini_exe_path = sbieini_exe_path or self.DEFAULT_SBIEINI_EXE
        self.sandbox_name = sandbox_name
        self._active_processes: Dict[int, Dict] = {}  # pid -> {process, sandbox, program}

        # 检查工具是否存在
        if not os.path.exists(self.start_exe_path):
            raise FileNotFoundError(f"Start.exe 未找到: {self.start_exe_path}")
        if not os.path.exists(self.sbieini_exe_path):
            raise FileNotFoundError(f"SbieIni.exe 未找到: {self.sbieini_exe_path}")

    # ---------- 沙盒管理 ----------
    def create_sandbox(self, name: str, settings: Dict[str, str] = None) -> bool:
        """创建新沙盒，若已存在则先删除后重建"""
        if self._sandbox_exists(name):
            print(f"沙盒 {name} 已存在，正在删除...")
            self.delete_sandbox(name, confirm=True)

        # 创建沙盒配置节
        cmd = [self.sbieini_exe_path, "set", name, "*", ""]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"创建沙盒失败: {result.stderr}")
            return False

        # 设置基本配置
        base = {"Enabled": "y"}
        if settings:
            base.update(settings)
        for key, val in base.items():
            subprocess.run([self.sbieini_exe_path, "set", name, key, val], capture_output=True)

        print(f"沙盒 {name} 创建成功")
        return True

    def delete_sandbox(self, name: str, confirm: bool = False) -> bool:
        """删除沙盒"""
        if not self._sandbox_exists(name):
            return False
        cmd = [self.sbieini_exe_path, "set", name, "*", ""]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"沙盒 {name} 已删除")
            return True
        else:
            print(f"删除沙盒失败: {result.stderr}")
            return False

    def _sandbox_exists(self, name: str) -> bool:
        """检查沙盒是否存在"""
        cmd = [self.sbieini_exe_path, "query", name, "Enabled"]
        result = subprocess.run(cmd, capture_output=True, text=True)
        return result.returncode == 0 and "No such section" not in result.stderr

    # ---------- 进程启动（支持管道通信） ----------
    def start_process(
        self,
        program_path: str,
        args: List[str] = None,
        sandbox_name: str = None,
        wait: bool = False,
        elevate: bool = False,
        silent: bool = False,
        hide_window: bool = False,
        capture_output: bool = True,
        env_vars: Dict[str, str] = None,
    ) -> Optional[subprocess.Popen]:
        """
        在沙盒中启动程序，并返回 Popen 对象（可用于管道通信）
        若 capture_output=True，则创建 stdin/stdout/stderr 管道
        """
        sandbox = sandbox_name or self.sandbox_name
        args = args or []

        cmd = [self.start_exe_path, f"/box:{sandbox}"]
        if silent:
            cmd.append("/silent")
        if elevate:
            cmd.append("/elevate")
        if hide_window:
            cmd.append("/hide_window")
        if wait:
            cmd.append("/wait")
        if env_vars:
            for k, v in env_vars.items():
                cmd.append(f"/env:{k}={v}")

        cmd.append(program_path)
        cmd.extend(args)

        try:
            if capture_output:
                # 创建管道，实现父子进程通信
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=1,
                    text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW if hide_window else 0
                )
            else:
                proc = subprocess.Popen(cmd)

            self._active_processes[proc.pid] = {
                "process": proc,
                "sandbox": sandbox,
                "program": program_path
            }
            print(f"[启动] PID={proc.pid}, 程序={program_path}, 沙盒={sandbox}")
            return proc
        except Exception as e:
            print(f"启动失败: {e}")
            return None

    # ---------- 进程通信 ----------
    def send_command(self, process: subprocess.Popen, command: str) -> bool:
        """向进程的标准输入发送一行命令（需已创建管道）"""
        if process is None or process.stdin is None:
            return False
        if process.poll() is not None:
            return False
        try:
            process.stdin.write(command + "\n")
            process.stdin.flush()
            return True
        except:
            return False

    def read_output(self, process: subprocess.Popen) -> Tuple[str, str]:
        """非阻塞读取进程的 stdout 和 stderr（已有内容）"""
        stdout = ""
        stderr = ""
        try:
            if process.stdout and process.stdout.readable():
                # 读取所有可读内容（非阻塞需要额外处理，这里简单读取一行）
                import select
                # Windows 下简单轮询
                while True:
                    line = process.stdout.readline()
                    if not line:
                        break
                    stdout += line
            if process.stderr and process.stderr.readable():
                while True:
                    line = process.stderr.readline()
                    if not line:
                        break
                    stderr += line
        except:
            pass
        return stdout, stderr

    def communicate_with_process(self, process: subprocess.Popen, input_text: str, timeout: int = 30) -> Tuple[str, str]:
        """与进程进行一次完整交互（发送输入，等待结束，返回全部输出）"""
        if process is None:
            return "", ""
        try:
            stdout, stderr = process.communicate(input=input_text, timeout=timeout)
            return stdout or "", stderr or ""
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            return stdout or "", stderr or ""
        except Exception as e:
            print(f"通信异常: {e}")
            return "", ""

    # ---------- 进程终止 ----------
    def terminate_process(self, process: subprocess.Popen, force: bool = False) -> bool:
        """终止进程，force=True 强制杀死"""
        if process is None or process.poll() is not None:
            return False
        pid = process.pid
        try:
            if force:
                process.kill()
            else:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
            if pid in self._active_processes:
                del self._active_processes[pid]
            print(f"[终止] PID={pid}")
            return True
        except Exception as e:
            print(f"终止失败: {e}")
            return False

    def terminate_all(self) -> int:
        """终止所有由本控制器启动的进程"""
        count = 0
        for pid, info in list(self._active_processes.items()):
            if self.terminate_process(info["process"], force=True):
                count += 1
        self._active_processes.clear()
        return count

    def get_active_processes(self) -> Dict[int, Dict]:
        """获取当前活动进程列表"""
        result = {}
        for pid, info in self._active_processes.items():
            p = info["process"]
            status = "running" if p.poll() is None else f"exited({p.poll()})"
            result[pid] = {
                "sandbox": info["sandbox"],
                "program": info["program"],
                "status": status
            }
        return result


class SandboxInstance:
    """单个沙盒实例，封装控制器和当前进程"""

    def __init__(self, name: str):
        self.name = name
        self.controller = SandboxieController(sandbox_name=name)
        self.process: Optional[subprocess.Popen] = None
        self.status = "idle"   # idle, running, stopped

    def create(self, settings: Dict[str, str] = None) -> bool:
        """创建沙盒（删除旧的）"""
        return self.controller.create_sandbox(self.name, settings)

    def delete(self) -> bool:
        """删除沙盒"""
        return self.controller.delete_sandbox(self.name)

    def start(self, program_path: str, args: List[str] = None, capture_output: bool = True) -> bool:
        """在沙盒中启动程序"""
        if self.process and self.process.poll() is None:
            print(f"沙盒 {self.name} 已有进程运行，请先终止")
            return False
        proc = self.controller.start_process(program_path, args, capture_output=capture_output)
        if proc:
            self.process = proc
            self.status = "running"
            return True
        return False

    def stop(self, force: bool = False) -> bool:
        """停止沙盒中的进程"""
        if self.process:
            ok = self.controller.terminate_process(self.process, force=force)
            self.process = None
            self.status = "stopped" if ok else "error"
            return ok
        return False

    def send_command(self, command: str) -> bool:
        """向运行中的进程发送命令"""
        if self.process and self.process.poll() is None:
            return self.controller.send_command(self.process, command)
        return False

    def read_output(self) -> Tuple[str, str]:
        """读取进程输出"""
        if self.process:
            return self.controller.read_output(self.process)
        return "", ""

    def communicate(self, input_text: str, timeout: int = 30) -> Tuple[str, str]:
        """一次性交互（等待进程结束）"""
        if self.process:
            return self.controller.communicate_with_process(self.process, input_text, timeout)
        return "", ""


class SandboxPool:
    """
    沙盒池：管理多个命名的沙盒实例（例如为每个游戏窗口或微信账号分配独立沙盒）
    """

    def __init__(self, base_name: str = "Sandbox", max_instances: int = 10):
        self.base_name = base_name
        self.max_instances = max_instances
        self._instances: Dict[str, SandboxInstance] = {}
        self._lock = threading.Lock()

    def _create_new_instance(self, identifier: str) -> SandboxInstance:
        """创建新的沙盒实例（不检查数量）"""
        name = f"{self.base_name}_{identifier}"
        inst = SandboxInstance(name)
        inst.create()   # 创建沙盒（自动删除旧的）
        return inst

    def acquire(self, identifier: str, auto_create: bool = True) -> Optional[SandboxInstance]:
        """
        获取或创建一个沙盒实例
        identifier: 唯一标识，例如 "wechat_001" 或 "game_player1"
        """
        with self._lock:
            if identifier in self._instances:
                return self._instances[identifier]
            if not auto_create:
                return None
            if len(self._instances) >= self.max_instances:
                raise RuntimeError(f"沙盒池已满（最大 {self.max_instances} 个），无法创建新实例")
            inst = self._create_new_instance(identifier)
            self._instances[identifier] = inst
            return inst

    def release(self, identifier: str, delete_sandbox: bool = False) -> bool:
        """
        释放沙盒实例（停止进程，可选是否删除沙盒）
        """
        with self._lock:
            if identifier not in self._instances:
                return False
            inst = self._instances[identifier]
            inst.stop()
            if delete_sandbox:
                inst.delete()
            del self._instances[identifier]
            return True

    def get_instance(self, identifier: str) -> Optional[SandboxInstance]:
        with self._lock:
            return self._instances.get(identifier)

    def get_all_instances(self) -> Dict[str, SandboxInstance]:
        with self._lock:
            return dict(self._instances)

    def stop_all(self, delete_sandbox: bool = False):
        """停止所有沙盒中的进程，可选删除沙盒"""
        for ident in list(self._instances.keys()):
            self.release(ident, delete_sandbox=delete_sandbox)


# ========== 使用示例 ==========
if __name__ == "__main__":
    # 1. 基本单沙盒控制示例（带管道通信）
    print("=== 单沙盒示例 ===")
    ctrl = SandboxieController(sandbox_name="DemoBox")
    ctrl.create_sandbox("DemoBox")

    # 启动一个 Python 脚本作为示例，该脚本从 stdin 读取并回显
    test_script = """
import sys
for line in sys.stdin:
    if line.strip().lower() == 'exit':
        break
    sys.stdout.write(f"Echo: {line}")
    sys.stdout.flush()
"""
    # 写入临时文件
    with open("test_echo.py", "w", encoding="utf-8") as f:
        f.write(test_script)

    proc = ctrl.start_process("python", args=["-u", "test_echo.py"], capture_output=True)
    if proc:
        # 发送命令并读取响应（通过 communicate 一次性交互）
        out, err = ctrl.communicate_with_process(proc, "hello\nworld\nexit\n")
        print("输出:", out)
        print("错误:", err)
    ctrl.terminate_all()
    os.remove("test_echo.py")

    # 2. 沙盒池示例（多开游戏/微信）
    print("\n=== 沙盒池示例 ===")
    pool = SandboxPool(base_name="GameBox", max_instances=3)

    # 为三个玩家创建独立沙盒，启动游戏（假设 game.exe 支持命令行参数）
    # 这里用 notepad.exe 作为演示（实际替换为你的游戏路径）
    for i in range(1, 4):
        ident = f"player{i}"
        inst = pool.acquire(ident)
        # 启动程序（不捕获输出，因为游戏通常是 GUI）
        inst.start("notepad.exe", args=[f"player_{i}_log.txt"], capture_output=False)
        print(f"玩家 {i} 的沙盒 {inst.name} 已启动")

    time.sleep(3)  # 让游戏运行一会儿

    # 停止所有沙盒中的进程并删除沙盒
    pool.stop_all(delete_sandbox=True)
    print("所有沙盒已停止并清理")