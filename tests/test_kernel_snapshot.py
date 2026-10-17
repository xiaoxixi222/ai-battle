"""快照仓库：初始化、提交打标、硬回滚与只读清理（内核单元测试）。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from snapshot import Snapshot

if TYPE_CHECKING:
    from pathlib import Path


def make_snapshot(tmp_path: Path) -> tuple[Snapshot, Path]:
    """搭一个工作树（含 a.py）与快照仓库。"""
    work = tmp_path / "work"
    work.mkdir()
    (work / "a.py").write_text("x = 1\n", encoding="utf-8")
    return Snapshot(tmp_path / "snap.git", work), work


def test_reinit_commit_and_has(tmp_path: Path) -> None:
    """开局提交 initial；启动成功再打 stable。"""
    snap, _ = make_snapshot(tmp_path)
    head = snap.reinit()
    assert head
    assert snap.has("initial")
    assert not snap.has("stable")
    snap.commit("boot ok", tag="stable")
    assert snap.has("stable")


def test_rollback_restores_files(tmp_path: Path) -> None:
    """坏改动回滚：内容逐字节还原、新文件清掉。"""
    snap, work = make_snapshot(tmp_path)
    snap.reinit()
    snap.commit("good", tag="stable")
    (work / "a.py").write_text("x = 2\n", encoding="utf-8")
    (work / "bomb.py").write_text("boom\n", encoding="utf-8")
    ref, commit = snap.rollback()
    assert ref == "stable"
    assert commit
    assert (work / "a.py").read_text(encoding="utf-8") == "x = 1\n"
    assert not (work / "bomb.py").exists()


def test_rollback_without_stable_uses_initial(tmp_path: Path) -> None:
    """还没打 stable 就退回 initial。"""
    snap, work = make_snapshot(tmp_path)
    snap.reinit()
    (work / "a.py").write_text("x = 3\n", encoding="utf-8")
    ref, _ = snap.rollback()
    assert ref == "initial"
    assert (work / "a.py").read_text(encoding="utf-8") == "x = 1\n"


def test_pycache_not_tracked(tmp_path: Path) -> None:
    """__pycache__ 不进快照：回滚不会把它带回来。"""
    snap, work = make_snapshot(tmp_path)
    snap.reinit()
    (work / "__pycache__").mkdir()
    (work / "__pycache__" / "junk.pyc").write_bytes(b"junk")
    snap.commit("good", tag="stable")
    (work / "__pycache__" / "junk.pyc").unlink()
    (work / "__pycache__").rmdir()
    snap.rollback()
    assert not (work / "__pycache__").exists()


def test_reinit_clears_readonly_objects(tmp_path: Path) -> None:
    """重复初始化要能删掉带只读对象文件的旧仓库（Windows 上的老问题）。"""
    snap, _ = make_snapshot(tmp_path)
    snap.reinit()
    snap.commit("more")
    head = snap.reinit()
    assert head
