"""docker 运行器：真实对局默认进容器 + 持久化绑定（服务器战场 / AI 工作区）。"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING, cast

import arena
from battlelink import BattleLink
from kernel import _resolve_runner
from matchconfig import MatchConfig

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from orchestrator import Match


def test_arena_defaults_to_docker() -> None:
    """真实对局（arena）缺省把服务器放进 Docker；显式 --runner 优先，mock 仍走本地。"""
    cfg = MatchConfig()
    _resolve_runner(cfg, argparse.Namespace(runner=None, arena=True))
    assert cfg.runner == "docker"

    explicit = MatchConfig()
    _resolve_runner(explicit, argparse.Namespace(runner="local", arena=True))
    assert explicit.runner == "local"

    mock = MatchConfig()
    _resolve_runner(mock, argparse.Namespace(runner=None, arena=False))
    assert mock.runner == "local"


def test_server_command_binds_battle_dir(tmp_path: Path) -> None:
    """服务器命令把本地 battle/ 绑进 /battle（bind mount，不是 named volume）。"""
    battle = tmp_path / "battle"
    link = BattleLink(battle, runner="docker", docker={"image": "python:latest"})
    cmd = link._command()  # noqa: SLF001
    mount = f"{battle.resolve().as_posix()}:/battle"
    assert cmd[cmd.index("-v") + 1] == mount
    assert cmd[cmd.index("-w") + 1] == "/battle"
    assert "BATTLE_ROOT=/battle" in cmd


class _FakeMatch:
    """假对局：ArenaBox 只碰 match_id / tokens / workspace_dir 三样。"""

    def __init__(self, root: Path) -> None:
        """认领根目录与一份假令牌表。"""
        self.match_id = "m1"
        self.tokens = {1: "token-1"}
        self._root = root

    def workspace_dir(self, ai_id: int) -> Path:
        """按内核约定给出工作区路径。"""
        return self._root / "runs" / "m1" / "workspaces" / f"ai-{ai_id}"


def test_ai_container_binds_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AI 容器把 runs/<id>/workspaces/ai-n 绑进 /workspace（bind mount）。"""
    calls: list[list[str]] = []

    def fake_docker(arguments: list[str]) -> tuple[int, str]:
        calls.append(arguments)
        return 0, ""

    monkeypatch.setattr(arena, "_docker", fake_docker)
    monkeypatch.setattr(arena.ArenaBox, "wait_ready", lambda _self: None)
    box = arena.ArenaBox(
        match=cast("Match", _FakeMatch(tmp_path)),
        ai_id=1,
        settings={
            "image": "ai-battle-ai:latest",
            "opencode_bin": "/root/.opencode/bin/opencode",
            "serve_port": 4096,
            "cpus": "1.5",
            "memory": "1g",
            "pids": 512,
        },
        gateway_url="http://127.0.0.1:1",
        model="opencode/x",
    )
    box.start()
    run_args = calls[1]  # calls[0] 是清残骸的 docker rm -f
    mount = f"{box.workspace.resolve().as_posix()}:/workspace"
    assert run_args[run_args.index("-v") + 1] == mount
    assert run_args[run_args.index("-w") + 1] == "/workspace"
