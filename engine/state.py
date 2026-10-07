"""引擎状态。纯数据，无任何 I/O、无 LLM 依赖。"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from config import BoardConfig


class Role(StrEnum):
    """角色。英文枚举值。"""

    WEREWOLF = "werewolf"
    SEER = "seer"
    WITCH = "witch"
    HUNTER = "hunter"
    VILLAGER = "villager"


class Faction(StrEnum):
    """阵营。预言家查验只告知阵营，不告知具体角色。"""

    WOLF = "wolf"
    GOOD = "good"


FACTION_OF: dict[Role, Faction] = {
    Role.WEREWOLF: Faction.WOLF,
    Role.SEER: Faction.GOOD,
    Role.WITCH: Faction.GOOD,
    Role.HUNTER: Faction.GOOD,
    Role.VILLAGER: Faction.GOOD,
}

GOD_ROLES = {Role.SEER, Role.WITCH, Role.HUNTER}

# 角色的中文名。枚举值本身保持英文（协议字段），展示层用这张表翻译。
ROLE_LABELS: dict[Role, str] = {
    Role.WEREWOLF: "狼人",
    Role.SEER: "预言家",
    Role.WITCH: "女巫",
    Role.HUNTER: "猎人",
    Role.VILLAGER: "平民",
}


class DeathCause(StrEnum):
    """死因。决定猎人能否开枪，也决定是否有遗言。"""

    KILL = "kill"  # 被狼人刀死
    POISON = "poison"  # 被女巫毒死
    LYNCH = "lynch"  # 被投票出局
    SHOT = "shot"  # 被猎人开枪


class Winner(StrEnum):
    WOLF = "wolf"
    GOOD = "good"


class Phase(StrEnum):
    """阶段。英文枚举值，直接写进 ``event.phase``。见 DESIGN.md §4.10。"""

    NIGHT_WOLF_CHAT = "night_wolf_chat"
    NIGHT_WOLF_VOTE = "night_wolf_vote"
    NIGHT_WOLF_RUNOFF_SPEECH = "night_wolf_runoff_speech"
    NIGHT_WOLF_RUNOFF_VOTE = "night_wolf_runoff_vote"
    NIGHT_SEER = "night_seer"
    NIGHT_WITCH = "night_witch"
    NIGHT_RESOLVE = "night_resolve"
    NIGHT_HUNTER_SHOOT = "night_hunter_shoot"
    DAY_ANNOUNCE = "day_announce"
    DAY_DISCUSSION = "day_discussion"
    DAY_VOTE = "day_vote"
    DAY_RUNOFF_SPEECH = "day_runoff_speech"
    DAY_RUNOFF_VOTE = "day_runoff_vote"
    DAY_RESOLVE = "day_resolve"
    LAST_WORDS = "last_words"
    HUNTER_SHOOT = "hunter_shoot"
    POSTGAME = "postgame"
    GAME_OVER = "game_over"


class Player(BaseModel):
    seat: int
    role: Role
    alive: bool = True
    death_cause: DeathCause | None = None
    death_day: int | None = None


class Death(BaseModel):
    """一次死亡。夜间死亡汇总后在白天统一公告。"""

    seat: int
    cause: DeathCause


class PotionState(BaseModel):
    """女巫的药。跨夜持久化。"""

    antidote_left: bool = True
    poison_left: bool = True


class NightState(BaseModel):
    """当夜的临时结算数据。每天开始时重置。"""

    kill_target: int | None = None  # 狼人刀口
    saved: bool = False  # 女巫是否用解药
    poison_target: int | None = None  # 女巫毒的目标
    seer_target: int | None = None
    seer_result: Faction | None = None
    deaths: list[Death] = Field(default_factory=list)


class GameState(BaseModel):
    """完整对局状态。``apply()`` 深拷贝后修改并返回新对象，输入不被改动。"""

    game_id: str
    day: int = 1
    phase: Phase = Phase.NIGHT_WOLF_CHAT
    players: dict[int, Player] = Field(default_factory=dict)
    board: BoardConfig = Field(default_factory=BoardConfig)

    # 随机性：发牌与兜底选择都走这里，禁止直接用全局 random
    seed: int | None = None
    rng_step: int = 0

    next_seq: int = 1

    # 当前阶段的调度状态
    round_index: int = 0  # 讨论 / 协商的第几轮（0-based）
    pending_actors: list[int] = Field(default_factory=list)  # 本阶段还没行动的座位
    votes: dict[int, int | None] = Field(default_factory=dict)  # seat -> 目标，None = 弃权
    tie_candidates: list[int] = Field(default_factory=list)  # 平票候选，用于加时重投

    night: NightState = Field(default_factory=NightState)
    potions: PotionState = Field(default_factory=PotionState)

    # 出局结算的挂起项
    pending_lynch: int | None = None
    pending_last_words: list[int] = Field(default_factory=list)
    pending_hunter: int | None = None
    pending_hunter_target: int | None = None
    pending_deaths: list[Death] = Field(default_factory=list)  # 当夜死亡，待白天公告

    winner: Winner | None = None
