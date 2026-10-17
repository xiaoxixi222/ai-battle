"""对局配置：加载、合并与校验（内核单元测试）。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from matchconfig import MatchConfig, load

if TYPE_CHECKING:
    from pathlib import Path


def test_defaults() -> None:
    """默认配置：3 个 AI、open 开局、本地运行器。"""
    cfg = MatchConfig()
    assert cfg.ai_ids() == (1, 2, 3)
    assert cfg.start_mode == "open"
    assert cfg.runner == "local"
    assert cfg.scores.takeover_bonus == 200


def test_from_dict_merges_scores() -> None:
    """字典覆盖：平铺字段与嵌套 scores 各自合并，未覆盖的保持默认。"""
    cfg = MatchConfig.from_dict({"ai_count": 2, "tick_sec": 5, "scores": {"owner_per_tick": 7}})
    assert cfg.ai_count == 2
    assert cfg.tick_sec == 5
    assert cfg.scores.owner_per_tick == 7
    assert cfg.scores.crash_ladder_base == 10


def test_load_from_file(tmp_path: Path) -> None:
    """从磁盘读 match.json。"""
    p = tmp_path / "match.json"
    p.write_text(json.dumps({"duration_min": 1, "runner": "docker"}), encoding="utf-8")
    cfg = load(p)
    assert cfg.duration_min == 1
    assert cfg.runner == "docker"


def test_load_rejects_non_object(tmp_path: Path) -> None:
    """配置文件不是 JSON 对象 → TypeError。"""
    p = tmp_path / "match.json"
    p.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(TypeError, match="JSON 对象"):
        load(p)


@pytest.mark.parametrize(
    ("field", "value", "pattern"),
    [
        ("ai_count", 4, "ai_count 必须是 2 或 3"),
        ("start_mode", "monarch", "start_mode 必须是 open"),
        ("runner", "vm", "runner 必须是 local 或 docker"),
        ("duration_min", 0, "duration_min 必须是正数"),
    ],
)
def test_validate_rejects_bad_values(field: str, value: object, pattern: str) -> None:
    """明显写错的配置会被挡住。"""
    cfg = MatchConfig()
    setattr(cfg, field, value)
    with pytest.raises(ValueError, match=pattern):
        cfg.validate()


def test_validate_rejects_wrong_type() -> None:
    """数值字段给字符串 → TypeError。"""
    cfg = MatchConfig()
    cfg.tick_sec = "30"  # type: ignore[assignment]
    with pytest.raises(TypeError, match="tick_sec 必须是数字"):
        cfg.validate()


def test_docker_settings_merge() -> None:
    """Docker 参数：默认值 + 配置覆盖。"""
    cfg = MatchConfig(docker={"image": "ai-battle:dev", "pids": 64})
    settings = cfg.docker_settings()
    assert settings["image"] == "ai-battle:dev"
    assert settings["pids"] == 64
    assert settings["network"] == "none"
