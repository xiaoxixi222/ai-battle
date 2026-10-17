"""代码快照：用独立 git 仓库对 battle/server 做提交与硬回滚。

- 仓库在 runs/<id>/snapshot.git（裸仓库），工作树指向 battle/server——**只覆盖代码**；
- 开局提交 initial；每次启动成功提交一次并移动 stable 标签；
- 回滚 = reset --hard stable（没有 stable 时退回 initial）+ clean -fd；
- __pycache__ 等运行时垃圾走 info/exclude，不参与提交。

state/、home/、logs/ 不在快照范围内——回滚只动代码，这是规则。
"""

from __future__ import annotations

import contextlib
import shutil
import stat
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

_EXCLUDE = "__pycache__/\n*.pyc\n"

# core.autocrlf=false：让快照逐字节还原，不被平台换行策略改写
_BASE = ("-c", "core.autocrlf=false", "-c", "core.safecrlf=false")
_IDENTITY = ("-c", "user.name=ai-battle-kernel", "-c", "user.email=kernel@ai-battle.local")


def force_rmtree(path: Path) -> None:
    """整树删除：只读文件先解除只读再删（git 对象默认只读，Windows 会拒删）。"""

    def _on_error(func: Callable[..., object], target: str, _exc: BaseException) -> None:
        with contextlib.suppress(OSError):
            Path(target).chmod(stat.S_IWRITE)
        func(target)

    shutil.rmtree(path, onexc=_on_error)


class Snapshot:
    """一个对局专用的小型版本库。"""

    def __init__(self, git_dir: Path, work_tree: Path) -> None:
        """绑定快照仓库与工作树（battle/server）。"""
        self.git_dir = Path(git_dir)
        self.work_tree = Path(work_tree)

    def _git(
        self,
        *args: str,
        check: bool = True,
        work_tree: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """执行一条 git 命令（独立 git-dir / work-tree）。"""
        cmd = ["git", f"--git-dir={self.git_dir}"]
        if work_tree:
            cmd.append(f"--work-tree={self.work_tree}")
        cmd += [*_BASE, *args]
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if check and result.returncode != 0:
            msg = f"git 命令失败（{result.returncode}）：{' '.join(args)}\n{result.stderr.strip()}"
            raise RuntimeError(msg)
        return result

    def reinit(self) -> str:
        """清掉旧仓库并重新初始化，提交一次 initial，返回提交号。

        bare init 之后把 core.bare 关掉，才允许配 --work-tree 使用。
        """
        if self.git_dir.exists():
            force_rmtree(self.git_dir)
        self.git_dir.mkdir(parents=True, exist_ok=True)
        self._git("init", "--bare", "-q", work_tree=False)
        self._git("config", "core.bare", "false", work_tree=False)
        info = self.git_dir / "info"
        info.mkdir(exist_ok=True)
        (info / "exclude").write_text(_EXCLUDE, encoding="utf-8")
        return self.commit("initial", tag="initial")

    def commit(self, message: str, *, tag: str | None = None) -> str:
        """把工作树当前状态全部提交；tag 非空时强制移动到该标签。"""
        self._git("add", "-A")
        self._git(*_IDENTITY, "commit", "-m", message, "--allow-empty", "-q")
        if tag is not None:
            self._git("tag", "-f", tag)
        return self.head()

    def rollback(self) -> tuple[str, str]:
        """硬回滚到 stable（缺失时 initial），清理未跟踪文件，返回 (目标, 提交号)。"""
        ref = "stable" if self.has("stable") else "initial"
        self._git("reset", "--hard", ref, "-q")
        self._git("clean", "-fd", "-q")
        return ref, self.head()

    def has(self, ref: str) -> bool:
        """标签是否存在。"""
        result = self._git("rev-parse", "-q", "--verify", f"refs/tags/{ref}", check=False)
        return result.returncode == 0

    def head(self) -> str:
        """当前 HEAD 的短提交号。"""
        return self._git("rev-parse", "--short", "HEAD").stdout.strip()
