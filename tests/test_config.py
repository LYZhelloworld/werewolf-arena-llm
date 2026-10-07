"""配置加载与启动校验。

核心约定：缺配置就报错退出，且错误信息带可复制的修复命令；绝不静默用默认值。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from config import (
    CONFIG_DIR,
    BoardConfig,
    ConfigError,
    GameConfig,
    ModelsConfig,
    load_board,
    load_game,
    load_models,
    resolve_thinking,
)

EXAMPLE_NAMES = ("board", "game", "models")


@pytest.fixture
def prepared(tmp_path: Path) -> Path:
    """把样例配置复制成「用户自己的配置」。"""
    for name in EXAMPLE_NAMES:
        shutil.copy(CONFIG_DIR / f"{name}.example.yaml", tmp_path / f"{name}.yaml")
    return tmp_path


@pytest.fixture
def env_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """把样例里出现的所有 api_key_env 都设上，避免加载时报错。"""
    for key in (
        "DEEPSEEK_API_KEY",
        "MOONSHOT_API_KEY",
        "DASHSCOPE_API_KEY",
        "OPENAI_API_KEY",
        "LOCAL_API_KEY",
    ):
        monkeypatch.setenv(key, "test-key")


def test_examples_are_loadable(prepared: Path, env_keys: None) -> None:
    assert isinstance(load_board(prepared), BoardConfig)
    assert isinstance(load_game(prepared), GameConfig)
    assert isinstance(load_models(prepared), ModelsConfig)


def test_missing_file_reports_cp_command(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc:
        load_board(tmp_path)
    message = exc.value.message
    assert "缺少配置文件" in message
    assert "cp" in message
    assert "board.example.yaml" in message


def test_invalid_value_reports_field(tmp_path: Path) -> None:
    (tmp_path / "board.yaml").write_text("roles:\n  werewolf: 三\n", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_board(tmp_path)
    assert "roles.werewolf" in exc.value.message


def test_missing_api_key_env_reports_export(
    prepared: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(ConfigError) as exc:
        load_models(prepared)
    assert "DEEPSEEK_API_KEY" in exc.value.message
    assert "export" in exc.value.message


def test_invalid_replay_on_error_fails_before_network(tmp_path: Path) -> None:
    """非法推理配置要在启动阶段就炸，不能等跑起来才炸。"""
    (tmp_path / "models.yaml").write_text(
        "providers:\n"
        "  x:\n"
        "    base_url: http://localhost:11434/v1\n"
        "    api_key_env: X_API_KEY\n"
        "    models:\n"
        "      - name: m\n"
        "        thinking: { replay_on_error: maybe }\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as exc:
        load_models(tmp_path)
    assert "replay_on_error" in exc.value.message


def test_thinking_merges_defaults_provider_model(prepared: Path, env_keys: None) -> None:
    cfg = load_models(prepared)
    provider = cfg.providers["qwen"]
    model = next(m for m in provider.models if m.name == "qwen3.8-max")
    thinking = resolve_thinking(cfg.defaults, provider, model)
    assert thinking.enabled is True
    assert thinking.replay is True
    assert thinking.extra_body == {"preserve_thinking": True}
    # 未显式配置的字段继承 defaults
    assert thinking.field == "auto"

    plain = next(m for m in cfg.providers["openai"].models if m.name == "gpt-4o-mini")
    assert resolve_thinking(cfg.defaults, cfg.providers["openai"], plain).enabled is False
