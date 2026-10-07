"""中文提示词模板。

为什么放在 ``agents/`` 而不是 ``engine/prompts/``：``engine/`` 必须保持零 I/O 的
纯净性（DESIGN.md §5 原则 3），而 prompt 天然属于 LLM 层。

给模型看的事件文本与给观众看的是**两套**：给模型的不能带「（仅狼人可见）」
这类频道标记，否则等于把私有频道的存在与边界暗示给模型（§6 信息隔离）。
"""

from __future__ import annotations

from config import BoardConfig
from engine.events import Event
from engine.state import ROLE_LABELS, GameState, Phase, Player, Role

SPEECH_TYPES = ("speech", "wolf_chat", "last_words", "reflect")


# ---------------------------------------------------------------------------
# System Prompt
# ---------------------------------------------------------------------------


def _role_brief(role: Role, board: BoardConfig) -> str:
    """与该角色相关的规则。只讲自己能做什么，不泄露其他角色的私有机制。"""
    if role == Role.WEREWOLF:
        return (
            "- 你每晚与队友在只有狼人能看到的频道里协商，然后共同决定袭击谁。\n"
            "- 白天你要伪装成好人，可以撒谎、可以栽赃，目标是活到最后。\n"
            "- 狼人获胜条件：神职全部出局，或平民全部出局。"
        )
    if role == Role.SEER:
        reveal = (
            "只告知对方是好人还是狼人" if board.seer.reveal_faction_only else "告知对方的具体身份"
        )
        return (
            f"- 你每晚可以查验一名存活玩家，{reveal}。查验结果只有你自己知道。\n"
            "- 亮明身份能带队但会成为狼人的首要目标，权衡时机。\n"
            "- 好人获胜条件：所有狼人出局。"
        )
    if role == Role.WITCH:
        lines = ["- 你有一瓶解药和一瓶毒药，各只能用一次。"]
        if board.witch.self_save:
            scope = "仅首夜" if board.witch.self_save_first_night_only else "每夜"
            lines.append(f"- 解药可以救下被狼人袭击的玩家，{scope}可以自救。")
        else:
            lines.append("- 解药可以救下被狼人袭击的玩家，但不能自救。")
        if board.witch.same_night_both_potions:
            lines.append("- 同一夜可以既用解药又用毒药。")
        else:
            lines.append("- 同一夜不能同时使用两瓶药。")
        if board.witch.knows_kill_target:
            lines.append("- 你每晚都会得知狼人袭击了谁。")
        lines.append("- 好人获胜条件：所有狼人出局。")
        return "\n".join(lines)
    if role == Role.HUNTER:
        lines = ["- 你出局时可以开枪带走一名玩家。"]
        triggers = []
        if board.hunter.shoot_on_lynch:
            triggers.append("被投票出局")
        if board.hunter.shoot_on_kill:
            triggers.append("被狼人袭击")
        if triggers:
            lines.append(f"- 触发时机：{'、'.join(triggers)}。")
        if not board.hunter.shoot_on_poison:
            lines.append("- 被女巫毒死时无法开枪。")
        if board.hunter.can_hold_fire:
            lines.append("- 你可以选择放弃开枪。")
        lines.append("- 好人获胜条件：所有狼人出局。")
        return "\n".join(lines)
    return "- 你没有特殊能力，靠发言与投票找出狼人。\n- 好人获胜条件：所有狼人出局。"


# 引擎从设计上就不提供的机制（DESIGN.md §1 非目标、§11.4）。板子 schema 里没有对应
# 字段，所以这是**恒真事实**；仍然显式写进 prompt，是因为模型很容易凭训练记忆把别的
# 板子的机制臆想进本局——实测就有模型在无警长的板子上自己脑补出「警上争夺警徽」。
# 同理，「出局不翻牌、不公布死因」当前也是引擎的固有行为（``death.reveal_role`` 尚未接线）。
_ABSENT_MECHANISMS = "当前规则下无警长和警徽竞选。"


def board_total(board: BoardConfig) -> int:
    """板子定义的座位总数（各角色人数之和）。"""
    roles = board.roles
    return roles.werewolf + roles.seer + roles.witch + roles.hunter + roles.villager


def board_brief(board: BoardConfig) -> str:
    """本局板子的公开配置，作为 system prompt 的一节。

    身份分布与全局规则对所有玩家都是公开信息（现实里也人尽皆知），放进来不构成
    信息泄露；作用是把「本局到底怎么玩」讲清楚，免得模型拿别的板子的规则乱套。
    只写引擎**实际兑现**的规则，避免 prompt 与引擎行为不一致。
    """
    roles = board.roles
    composition = "、".join(
        f"{count} {ROLE_LABELS[role]}"
        for role, count in (
            (Role.WEREWOLF, roles.werewolf),
            (Role.SEER, roles.seer),
            (Role.WITCH, roles.witch),
            (Role.HUNTER, roles.hunter),
            (Role.VILLAGER, roles.villager),
        )
        if count > 0
    )

    wolf = board.werewolf
    night = f"狼人先私密协商 {wolf.chat_rounds} 轮，再" if wolf.chat_rounds > 0 else "狼人直接"
    night += "由全体狼人投票决定刀口" if wolf.vote_kill else "决定刀口"
    if wolf.tie_break_by_lowest_seat:
        night += "（平票取座位号最小者）"
    night += "；随后预言家查验、女巫用药。"

    day = (
        f"白天有 {board.day.discussion_rounds} 轮自由讨论"
        f"（单条发言不超过 {board.day.speech_max_chars} 字），然后投票放逐。"
    )

    vote = board.vote
    ballot = (
        "所有人同时投票，投票期间看不到别人投给谁"
        if vote.secret_ballot
        else "按座位依次公开投票，别人能看到你投给谁"
    )
    if vote.runoff_on_tie:
        tie = (
            "平票则加时辩一轮后重投，再次平票则本日无人出局"
            if vote.no_lynch_on_second_tie
            else "平票则加时辩一轮后重投"
        )
    else:
        tie = "平票则本日无人出局"
    vote_line = f"{ballot}；{tie}；" + ("允许弃权。" if vote.allow_abstain else "不允许弃权。")

    words = "只有被投票出局的人有遗言。" if board.death.last_words_on_lynch else "没有人有遗言。"
    death_line = f"出局只公布座位号，不公布身份，也不公布死因。{words}"

    win_line = (
        "屠边——神职全部出局或平民全部出局即狼人获胜，狼人全部出局则好人获胜。"
        if board.win.eliminate_one_faction
        else "狼人全部出局则好人获胜。"
    )

    return "\n".join(
        [
            f"{board_total(board)} 人局：{composition}。{_ABSENT_MECHANISMS}",
            f"- 夜晚：{night}",
            f"- {day}",
            f"- 投票：{vote_line}",
            f"- 出局：{death_line}",
            f"- 胜负：{win_line}",
        ]
    )


def system_prompt(seat: int, player: Player, state: GameState) -> str:
    """某个席位的 system prompt（中文）。"""
    board = state.board
    label = ROLE_LABELS[player.role]
    teammates = ""
    if player.role == Role.WEREWOLF:
        others = sorted(
            s for s, p in state.players.items() if p.role == Role.WEREWOLF and s != seat
        )
        teammates = f"\n你的狼人队友是：{'、'.join(f'{s} 号' for s in others)}。"

    total = board_total(board)
    return (
        f"# 你是谁\n"
        f"你是 {seat} 号玩家，身份：{label}。{teammates}\n"
        f"一共 {total} 名玩家，座位号 1 到 {total}。\n"
        "\n"
        "# 本局板子\n"
        f"{board_brief(board)}\n"
        "\n"
        "# 与你相关的规则\n"
        f"{_role_brief(player.role, board)}\n"
        "\n"
        "# 行为准则\n"
        "- 必须通过调用工具来行动，只在回复里说话不算行动。\n"
        "- 发言用中文，口语化，像真实玩家：可以分析、指控、辩护、撒谎、伪装。\n"
        "- 需要记住重要信息时用 memory_write，想起来时用 memory_read。\n"
        "- 行动前先用中文简要写下你的推理过程。\n"
        "- 不要提及你是人工智能，不要输出任何格式标记以外的内容。"
    )


# ---------------------------------------------------------------------------
# 阶段指令
# ---------------------------------------------------------------------------

# 面向**模型**的阶段措辞，故意与 ``engine.state.PHASE_LABELS``（面向观众的 UI 文案）
# 不同：这里要的是「现在要你干什么」的指令感（"狼人决定袭击目标"），而不是名词化的标题
# （"夜晚 · 狼人出刀"）。两张表用途不同，不要合并。
PROMPT_PHASE_LABELS: dict[str, str] = {
    Phase.NIGHT_WOLF_CHAT: "夜晚 · 狼人协商",
    Phase.NIGHT_WOLF_VOTE: "夜晚 · 狼人决定袭击目标",
    Phase.NIGHT_WOLF_RUNOFF_SPEECH: "夜晚 · 狼人平票后加时协商",
    Phase.NIGHT_WOLF_RUNOFF_VOTE: "夜晚 · 狼人平票后重新表决",
    Phase.NIGHT_SEER: "夜晚 · 预言家查验",
    Phase.NIGHT_WITCH: "夜晚 · 女巫用药",
    Phase.NIGHT_HUNTER_SHOOT: "夜晚 · 猎人开枪",
    Phase.DAY_ANNOUNCE: "白天 · 公布昨夜结果",
    Phase.DAY_DISCUSSION: "白天 · 自由讨论",
    Phase.DAY_VOTE: "白天 · 投票放逐",
    Phase.DAY_RUNOFF_SPEECH: "白天 · 平票加时发言",
    Phase.DAY_RUNOFF_VOTE: "白天 · 平票重新投票",
    Phase.LAST_WORDS: "遗言",
    Phase.HUNTER_SHOOT: "猎人开枪",
    Phase.POSTGAME: "赛后感想",
}


def situation_text(state: GameState, seat: int) -> str:
    """局面摘要：存活 / 出局 / 当前阶段。"""
    alive = sorted(s for s, p in state.players.items() if p.alive)
    dead = sorted(s for s, p in state.players.items() if not p.alive)
    alive_text = "、".join(str(s) for s in alive)
    dead_text = "、".join(str(s) for s in dead) if dead else "无"
    label = PROMPT_PHASE_LABELS.get(state.phase, state.phase.value)
    return f"第 {state.day} 天 · {label}\n存活：{alive_text}（你是 {seat} 号）\n已出局：{dead_text}"


def phase_instruction(state: GameState, seat: int, legal_targets: list[int]) -> str:
    """当前阶段要你做什么（中文）。

    **每条分支都要点名本阶段至少一个工具**：只说「说出你的建议」时，弱模型会
    老老实实写一段发言然后结束回合（它以为自己已经说过话了），本轮就白跑——
    实测 9B 模型在没点名工具的阶段整轮 4 次请求全部只说话。工具名见
    :class:`agents.tools.ActionType`。
    """
    phase = state.phase
    if phase in (Phase.NIGHT_WOLF_CHAT, Phase.NIGHT_WOLF_RUNOFF_SPEECH):
        return "现在是狼人内部的私密协商，只有队友能听到。用 speak 说出你的建议：今晚刀谁，为什么。"
    if phase in (Phase.NIGHT_WOLF_VOTE, Phase.NIGHT_WOLF_RUNOFF_VOTE):
        return f"用 wolf_vote_kill 选出今晚袭击的目标，可选：{_seats(legal_targets)}。"
    if phase == Phase.NIGHT_SEER:
        return f"用 seer_check 选择今晚查验的对象，可选：{_seats(legal_targets)}。"
    if phase == Phase.NIGHT_WITCH:
        return "你可以用 witch_save 救人、witch_poison 毒人，或用 witch_pass 不用药。"
    if phase in (Phase.NIGHT_HUNTER_SHOOT, Phase.HUNTER_SHOOT):
        return f"你出局了，可以开枪带走一人，可选：{_seats(legal_targets)}；也可 hunter_hold 放弃。"
    if phase == Phase.DAY_DISCUSSION:
        return f"现在是第 {state.day} 天自由讨论第 {state.round_index + 1} 轮，用 speak 发言。"
    if phase in (Phase.DAY_VOTE, Phase.DAY_RUNOFF_VOTE):
        return f"用 vote 投票放逐一人，可选：{_seats(legal_targets)}；信息不足可以 abstain 弃权。"
    if phase == Phase.DAY_RUNOFF_SPEECH:
        return "你们票数持平，用 speak 再发言一轮争取支持。"
    if phase == Phase.LAST_WORDS:
        return "你被放逐了，用 last_words 留下遗言。"
    if phase == Phase.POSTGAME:
        return "比赛结束了，用 reflect 说说你对这局的感想。"
    return "按当前情况调用工具行动。"


def _seats(seats: list[int]) -> str:
    return "、".join(str(s) for s in seats) if seats else "（无）"


# ---------------------------------------------------------------------------
# 给模型看的事件文本
# ---------------------------------------------------------------------------


def render_event(event: Event) -> str | None:
    """把一条事件渲染成给模型看的中文文本。

    返回 ``None`` 表示这条事件不需要告诉模型（如投票中间过程由结果统一呈现）。
    注意：**不带任何频道标记**，模型不该知道自己看的这段文字属于哪个频道。
    """
    actor = event.actor
    payload = event.payload
    kind = event.type

    if kind in SPEECH_TYPES:
        return f"{actor} 号：{payload.get('message', '')}"
    if kind == "death":
        return f"{payload.get('seat')} 号出局。"
    if kind == "night_deaths_announced":
        seats = payload.get("seats", [])
        if not seats:
            return "昨夜是平安夜，无人出局。"
        return f"昨夜出局：{'、'.join(str(s) for s in seats)} 号。"
    if kind == "seer_check":
        faction = "狼人" if payload.get("faction") == "wolf" else "好人"
        return f"你查验了 {payload.get('seat')} 号：{faction}。"
    if kind == "witch_save":
        return "你用解药救下了被袭击的玩家。"
    if kind == "witch_poison":
        return f"你毒死了 {payload.get('seat')} 号。"
    if kind == "witch_pass":
        return "你今夜没有用药。"
    if kind == "wolf_kill":
        return (
            f"今晚的袭击目标是 {payload.get('seat')} 号。" if payload.get("seat") else "今晚空刀。"
        )
    if kind == "vote_result":
        target = payload.get("target")
        if target is None:
            return "本日无人被放逐。"
        return f"投票结果：{target} 号被放逐。"
    if kind == "vote_tie":
        tied = payload.get("tied", [])
        return f"平票：{'、'.join(str(s) for s in tied)} 号，需要重新表决。"
    if kind == "hunter_shoot":
        return f"{actor} 号开枪带走了 {payload.get('seat')} 号。"
    if kind == "hunter_hold":
        return f"{actor} 号放弃了开枪。"
    if kind == "memory_write":
        return None  # 记忆结果由工具返回直接给出，不再重复进历史
    if kind == "roles_revealed":
        roles = payload.get("roles", {})
        text = "、".join(f"{s} 号是 {name}" for s, name in sorted(roles.items()))
        return f"身份揭晓：{text}。"
    return None
