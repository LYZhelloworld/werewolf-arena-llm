"""配置 schema 与加载器。

仓库只提交 ``configs/*.example.yaml``；真实配置由用户 ``cp`` 生成，已被 gitignore。
启动校验失败一律抛 :class:`ConfigError` —— 错误信息必须带可复制的修复命令，
绝不在缺失配置时静默回退到默认值（见 DESIGN.md §3.2）。

配置优先级（同 OpenCode 的 provider → model → variant）：
``defaults`` → provider 级 → model 级，后者覆盖前者，嵌套对象深合并。

**引擎纯净性**：``engine/`` 只能 import 本文件的 **schema 类**（``BoardConfig`` 等），
**绝不能 import 加载函数**（``load_board`` / ``load_all`` 等）——那些会读文件、读环境变量。
加载只发生在进程启动与 CLI / 服务层。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError

CONFIG_DIR = Path(__file__).parent / "configs"

# 回传被 provider 拒绝时的处置方式
REPLAY_ON_ERROR_CHOICES = ("fallback", "fail")


class ConfigError(Exception):
    """配置缺失或非法。启动即退出，不静默回退。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


# ---------------------------------------------------------------------------
# 板子配置
# ---------------------------------------------------------------------------


class RolesConfig(BaseModel):
    """各角色人数。"""

    werewolf: int = 3
    seer: int = 1
    witch: int = 1
    hunter: int = 1
    villager: int = 3


class WitchConfig(BaseModel):
    """女巫细则。"""

    has_antidote: bool = True
    has_poison: bool = True
    self_save: bool = True
    self_save_first_night_only: bool = True
    self_poison: bool = False
    knows_kill_target: bool = True
    knows_poisoned_identity: bool = False
    same_night_both_potions: bool = False


class HunterConfig(BaseModel):
    """猎人细则。"""

    shoot_on_lynch: bool = True
    shoot_on_kill: bool = True
    shoot_on_poison: bool = False
    can_hold_fire: bool = True


class SeerConfig(BaseModel):
    """预言家细则。"""

    check_every_night: bool = True
    reveal_faction_only: bool = True
    can_check_self: bool = False
    can_check_dead: bool = False


class WerewolfConfig(BaseModel):
    """狼人细则。"""

    chat_rounds: int = 2
    vote_kill: bool = True
    tie_break_by_lowest_seat: bool = True
    can_kill_teammate: bool = False
    can_kill_self: bool = False


class DeathConfig(BaseModel):
    """出局与遗言。"""

    reveal_role: bool = False
    last_words_on_lynch: bool = True
    last_words_on_night_death: bool = False
    last_words_on_first_night: bool = False


class VoteConfig(BaseModel):
    """投票规则。"""

    runoff_on_tie: bool = True
    no_lynch_on_second_tie: bool = True
    secret_ballot: bool = True
    allow_abstain: bool = True


class DayConfig(BaseModel):
    """白天流程。"""

    discussion_rounds: int = 2
    speech_max_chars: int = 300


class WinConfig(BaseModel):
    """胜负条件。"""

    eliminate_one_faction: bool = True


class PostgameConfig(BaseModel):
    """赛后感想环节。"""

    enabled: bool = True
    reveal_all_roles: bool = True


class BoardConfig(BaseModel):
    """整块板子。按角色分组，尽量 boolean。"""

    roles: RolesConfig = Field(default_factory=RolesConfig)
    witch: WitchConfig = Field(default_factory=WitchConfig)
    hunter: HunterConfig = Field(default_factory=HunterConfig)
    seer: SeerConfig = Field(default_factory=SeerConfig)
    werewolf: WerewolfConfig = Field(default_factory=WerewolfConfig)
    death: DeathConfig = Field(default_factory=DeathConfig)
    vote: VoteConfig = Field(default_factory=VoteConfig)
    day: DayConfig = Field(default_factory=DayConfig)
    win: WinConfig = Field(default_factory=WinConfig)
    postgame: PostgameConfig = Field(default_factory=PostgameConfig)


# ---------------------------------------------------------------------------
# 对局参数
# ---------------------------------------------------------------------------


class AgentMemoryConfig(BaseModel):
    enabled: bool = True
    max_entries: int = 20
    max_chars_per_entry: int = 200


class AgentConfig(BaseModel):
    max_tool_turns: int = 4
    max_concurrency: int = 4
    memory: AgentMemoryConfig = Field(default_factory=AgentMemoryConfig)


class ContextConfig(BaseModel):
    max_public_events: int = 60


class PersistConfig(BaseModel):
    root: str = "data"
    dump_agent_contexts: bool = True


class GameThinkingConfig(BaseModel):
    require_think_tool: bool = False


class GameConfig(BaseModel):
    seed: int | None = None
    agent: AgentConfig = Field(default_factory=AgentConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    persist: PersistConfig = Field(default_factory=PersistConfig)
    thinking: GameThinkingConfig = Field(default_factory=GameThinkingConfig)


# ---------------------------------------------------------------------------
# Provider 与模型
# ---------------------------------------------------------------------------


class ThinkingConfig(BaseModel):
    """单个模型的 CoT 处理策略。字段名等差异一律走配置，不写代码分支。"""

    enabled: bool = False
    # auto 按 reasoning_content → reasoning → reasoning_text 探测，或写任意 provider 私有字段名
    field: str = "auto"
    replay: bool = True
    replay_on_error: str = "fallback"
    # 承载各家非 OpenAI 标准的开关（thinking.keep / preserve_thinking / reasoning_effort ...）
    extra_body: dict[str, Any] = Field(default_factory=dict)


class ModelEntry(BaseModel):
    name: str
    thinking: ThinkingConfig | None = None
    extra_body: dict[str, Any] = Field(default_factory=dict)


class ProviderConfig(BaseModel):
    base_url: str
    api_key_env: str
    max_concurrency: int | None = None
    thinking: ThinkingConfig | None = None
    # provider 级非标准请求体字段，与 model 级 extra_body 深合并
    extra_body: dict[str, Any] = Field(default_factory=dict)
    models: list[ModelEntry] = Field(default_factory=list)


class DefaultsConfig(BaseModel):
    temperature: float = 0.8
    max_tokens: int = 1024
    timeout_s: int = 60
    network_retries: int = 3
    thinking: ThinkingConfig = Field(default_factory=ThinkingConfig)


class ModelsConfig(BaseModel):
    providers: dict[str, ProviderConfig]
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------


def _read_yaml(path: Path, example: str) -> dict[str, Any]:
    """读 YAML。文件不存在或语法非法都抛带修复命令的 ConfigError。"""
    if not path.exists():
        raise ConfigError(
            f"✗ 缺少配置文件：{path.as_posix()}\n"
            f"  请执行：cp {example} {path.as_posix()}\n"
            f"  然后按注释修改为自己想要的配置。"
        )
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"✗ 配置文件不是合法的 YAML：{path.as_posix()}\n  {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"✗ 配置文件顶层必须是键值映射：{path.as_posix()}")
    return data


def _parse(model: type[BaseModel], data: dict[str, Any], path: Path) -> Any:
    """Pydantic 校验，失败时把字段路径翻译成中文可读的定位。"""
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        lines = [f"✗ 配置文件非法：{path.as_posix()}"]
        for err in exc.errors():
            loc = ".".join(str(x) for x in err["loc"]) or "(顶层)"
            lines.append(f"  {loc}：{err['msg']}")
        raise ConfigError("\n".join(lines)) from exc


def load_board(config_dir: Path = CONFIG_DIR) -> BoardConfig:
    path = config_dir / "board.yaml"
    data = _read_yaml(path, f"{(config_dir / 'board.example.yaml').as_posix()}")
    return _parse(BoardConfig, data, path)


def load_game(config_dir: Path = CONFIG_DIR) -> GameConfig:
    path = config_dir / "game.yaml"
    data = _read_yaml(path, f"{(config_dir / 'game.example.yaml').as_posix()}")
    return _parse(GameConfig, data, path)


def load_models(config_dir: Path = CONFIG_DIR, check_env: bool = True) -> ModelsConfig:
    path = config_dir / "models.yaml"
    data = _read_yaml(path, f"{(config_dir / 'models.example.yaml').as_posix()}")
    cfg = _parse(ModelsConfig, data, path)

    _validate_thinking(cfg, path)
    if check_env:
        _validate_api_key_env(cfg, path)
    return cfg


def _validate_thinking(cfg: ModelsConfig, path: Path) -> None:
    """启动校验：非法推理配置在发起任何网络 I/O 之前就失败（对齐 DeepSeek Harness）。"""
    problems: list[str] = []
    for pname, provider in cfg.providers.items():
        for model in provider.models:
            thinking = resolve_thinking(cfg.defaults, provider, model)
            if thinking.replay_on_error not in REPLAY_ON_ERROR_CHOICES:
                problems.append(
                    f"  {pname}/{model.name}：thinking.replay_on_error 只能是 "
                    f"{' 或 '.join(REPLAY_ON_ERROR_CHOICES)}，实际 {thinking.replay_on_error!r}"
                )
            if not thinking.field.strip():
                problems.append(f"  {pname}/{model.name}：thinking.field 不能为空")
    if problems:
        raise ConfigError(f"✗ 推理配置不自洽：{path.as_posix()}\n" + "\n".join(problems))


def _validate_api_key_env(cfg: ModelsConfig, path: Path) -> None:
    missing: list[str] = []
    for pname, provider in cfg.providers.items():
        if not os.environ.get(provider.api_key_env):
            missing.append(
                f"  {pname}：环境变量 {provider.api_key_env} 未设置\n"
                f"    请执行：export {provider.api_key_env}=<你的 key>"
            )
    if missing:
        raise ConfigError(
            f"✗ {path.as_posix()} 中以下 provider 的 api_key_env 未设置：\n" + "\n".join(missing)
        )


# ---------------------------------------------------------------------------
# 配置合并
# ---------------------------------------------------------------------------


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def resolve_thinking(
    defaults: DefaultsConfig, provider: ProviderConfig, model: ModelEntry
) -> ThinkingConfig:
    """按 defaults → provider → model 三层深合并出该模型最终的 CoT 策略。"""
    merged = defaults.thinking.model_dump()
    if provider.thinking is not None:
        merged = _deep_merge(merged, provider.thinking.model_dump(exclude_none=True))
    if model.thinking is not None:
        merged = _deep_merge(merged, model.thinking.model_dump(exclude_none=True))
    return ThinkingConfig.model_validate(merged)


def resolve_extra_body(provider: ProviderConfig, model: ModelEntry) -> dict[str, Any]:
    """合并 provider 级与 model 级的 extra_body（各自 thinking.extra_body 已并入）。"""
    base: dict[str, Any] = {}
    if provider.thinking is not None:
        base = _deep_merge(base, provider.thinking.extra_body)
    base = _deep_merge(base, provider.extra_body)
    base = _deep_merge(base, model.extra_body)
    if model.thinking is not None:
        base = _deep_merge(base, model.thinking.extra_body)
    return base
