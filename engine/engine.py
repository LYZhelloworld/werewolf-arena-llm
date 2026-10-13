"""规则引擎。

``apply(state, action) -> (new_state, events)`` —— 输入不被修改，返回深拷贝后的新状态。
纯确定性：不 import LLM、网络、文件、环境变量。所有随机性走注入的 seed。

引擎与 LLM 互不知晓对方存在：引擎只认 :class:`Action`，只吐 :class:`Event`。
"""

from __future__ import annotations

import random
from datetime import UTC, datetime
from uuid import uuid4

from config import BoardConfig
from engine import phases, rules
from engine.actions import Action, ActionType, IllegalActionError, LegalAction
from engine.events import Event, RenderText, Visibility
from engine.phases import EmitFn
from engine.state import (
    Faction,
    GameState,
    Phase,
    Player,
    PotionState,
    Role,
    phase_label,
)

SPEAKING_TYPES = (ActionType.SPEAK, ActionType.LAST_WORDS, ActionType.REFLECT)


def _make_emitter(state: GameState, events: list[Event]) -> EmitFn:
    """构造发事件闭包：分配 seq、记录当前 day/phase。"""

    def emit(
        type_: str,
        visibility: Visibility,
        actor: int | None = None,
        payload: dict | None = None,
        render: RenderText | None = None,
    ) -> Event:
        event = Event(
            seq=state.next_seq,
            game_id=state.game_id,
            day=state.day,
            phase=state.phase.value,
            type=type_,
            visibility=visibility,
            actor=actor,
            payload=payload or {},
            render=render,
        )
        state.next_seq += 1
        events.append(event)
        return event

    return emit


class RuleEngine:
    """确定性规则引擎。一个实例持有一块板子，可开多局。"""

    def __init__(
        self,
        board: BoardConfig,
        game_id: str | None = None,
        seed: int | None = None,
    ) -> None:
        self.board = board
        self.seed = seed
        self.game_id = game_id or f"{datetime.now(UTC):%Y%m%d-%H%M%S}-{uuid4().hex[:6]}"

    # ------------------------------------------------------------------
    # 开局
    # ------------------------------------------------------------------

    def new_game(self) -> tuple[GameState, list[Event]]:
        """发牌并进入首夜。返回初始状态与开局事件。"""
        roles = self._deal()
        state = GameState(
            game_id=self.game_id,
            players={seat: Player(seat=seat, role=role) for seat, role in roles.items()},
            board=self.board,
            seed=self.seed,
            potions=PotionState(
                antidote_left=self.board.witch.has_antidote,
                poison_left=self.board.witch.has_poison,
            ),
        )
        events: list[Event] = []
        emit = _make_emitter(state, events)

        emit(
            "game_start",
            Visibility.PUBLIC,
            payload={"game_id": state.game_id, "seats": sorted(roles)},
            render=RenderText(text=f"对局开始，共 {len(roles)} 人"),
        )
        emit(
            "role_assigned",
            Visibility.SPECTATOR,  # 底牌只有观众能看到
            payload={"roles": {str(s): r.value for s, r in sorted(roles.items())}},
            render=RenderText(text="身份已分配（仅观众可见）"),
        )
        phases.setup_phase(state)
        self._emit_phase_enter(state, emit)
        return state, events

    def _deal(self) -> dict[int, Role]:
        """按板子人数发牌。同一 seed 必得同一结果。"""
        pool: list[Role] = []
        for name, count in self.board.roles.model_dump().items():
            pool.extend([Role(name)] * count)
        rng = random.Random(self.seed)
        rng.shuffle(pool)
        return {seat: role for seat, role in zip(range(1, len(pool) + 1), pool, strict=True)}

    # ------------------------------------------------------------------
    # 合法动作
    # ------------------------------------------------------------------

    def legal_actions(self, state: GameState) -> list[LegalAction]:
        """当前阶段所有待行动席位能做的动作。空列表表示引擎可自动推进。"""
        phase = state.phase
        board = state.board
        out: list[LegalAction] = []

        if phase in (Phase.NIGHT_WOLF_CHAT, Phase.NIGHT_WOLF_RUNOFF_SPEECH):
            return [LegalAction(seat=s, type=ActionType.SPEAK) for s in state.pending_actors]

        if phase in (Phase.DAY_DISCUSSION, Phase.DAY_RUNOFF_SPEECH):
            return [LegalAction(seat=s, type=ActionType.SPEAK) for s in state.pending_actors]

        if phase == Phase.LAST_WORDS:
            return [LegalAction(seat=s, type=ActionType.LAST_WORDS) for s in state.pending_actors]

        if phase == Phase.POSTGAME:
            return [LegalAction(seat=s, type=ActionType.REFLECT) for s in state.pending_actors]

        if phase in (Phase.NIGHT_WOLF_VOTE, Phase.NIGHT_WOLF_RUNOFF_VOTE):
            pool = (
                [t for t in state.tie_candidates if state.players[t].alive]
                if phase == Phase.NIGHT_WOLF_RUNOFF_VOTE
                else None
            )
            for seat in state.pending_actors:
                targets = (
                    [t for t in pool if t in rules.killable_targets(state, seat)]
                    if pool is not None
                    else rules.killable_targets(state, seat)
                )
                if targets:
                    out.append(
                        LegalAction(
                            seat=seat, type=ActionType.WOLF_VOTE_KILL, allowed_targets=targets
                        )
                    )
                if board.vote.allow_abstain:
                    out.append(LegalAction(seat=seat, type=ActionType.ABSTAIN))
            return out

        if phase == Phase.NIGHT_SEER:
            targets = rules.checkable_targets(state)
            if targets:
                out.extend(
                    LegalAction(seat=s, type=ActionType.SEER_CHECK, allowed_targets=targets)
                    for s in state.pending_actors
                )
            return out

        if phase == Phase.NIGHT_WITCH:
            for seat in state.pending_actors:
                if rules.can_witch_save(state):
                    out.append(LegalAction(seat=seat, type=ActionType.WITCH_SAVE))
                targets = rules.poisonable_targets(state)
                if targets:
                    out.append(
                        LegalAction(
                            seat=seat, type=ActionType.WITCH_POISON, allowed_targets=targets
                        )
                    )
                out.append(LegalAction(seat=seat, type=ActionType.WITCH_PASS))
            return out

        if phase in (Phase.NIGHT_HUNTER_SHOOT, Phase.HUNTER_SHOOT):
            hunter_seat = state.pending_hunter
            if hunter_seat is not None and hunter_seat in state.pending_actors:
                targets = rules.shootable_targets(state)
                if targets:
                    out.append(
                        LegalAction(
                            seat=hunter_seat, type=ActionType.HUNTER_SHOOT, allowed_targets=targets
                        )
                    )
                if board.hunter.can_hold_fire:
                    out.append(LegalAction(seat=hunter_seat, type=ActionType.HUNTER_HOLD))
            return out

        if phase in (Phase.DAY_VOTE, Phase.DAY_RUNOFF_VOTE):
            targets = (
                [t for t in state.tie_candidates if state.players[t].alive]
                if phase == Phase.DAY_RUNOFF_VOTE
                else rules.alive_seats(state)
            )
            for seat in state.pending_actors:
                if targets:
                    out.append(
                        LegalAction(seat=seat, type=ActionType.VOTE, allowed_targets=targets)
                    )
                if board.vote.allow_abstain:
                    out.append(LegalAction(seat=seat, type=ActionType.ABSTAIN))
            return out

        return out

    # ------------------------------------------------------------------
    # 结算
    # ------------------------------------------------------------------

    def apply(self, state: GameState, action: Action) -> tuple[GameState, list[Event]]:
        """结算一个动作。输入状态不会被修改。"""
        self._validate(state, action)

        new_state = state.model_copy(deep=True)
        events: list[Event] = []
        emit = _make_emitter(new_state, events)

        if action.type == ActionType.ADVANCE:
            phases.advance(new_state, emit)
            return new_state, events

        assert action.actor is not None  # _validate 已保证
        self._consume(new_state, action, emit)

        if action.actor in new_state.pending_actors:
            new_state.pending_actors.remove(action.actor)
        if new_state.phase == Phase.LAST_WORDS:
            new_state.pending_last_words = [
                s for s in new_state.pending_last_words if s != action.actor
            ]

        if not new_state.pending_actors:
            if self._has_more_rounds(new_state):
                new_state.round_index += 1
                new_state.pending_actors = phases.actors_for(new_state, new_state.phase)
                self._emit_phase_enter(new_state, emit)
            else:
                phases.advance(new_state, emit)

        return new_state, events

    def _has_more_rounds(self, state: GameState) -> bool:
        """讨论 / 协商是否还有下一轮。"""
        if state.phase == Phase.NIGHT_WOLF_CHAT:
            return state.round_index + 1 < state.board.werewolf.chat_rounds
        if state.phase == Phase.DAY_DISCUSSION:
            return state.round_index + 1 < state.board.day.discussion_rounds
        return False

    def _emit_phase_enter(self, state: GameState, emit: EmitFn) -> None:
        emit(
            "phase_enter",
            Visibility.PUBLIC,
            payload={"phase": state.phase.value, "day": state.day},
            render=RenderText(text=f"进入阶段：{phase_label(state.phase)}"),
        )

    def _consume(self, state: GameState, action: Action, emit: EmitFn) -> None:
        """把动作写进状态并发出对应事件。"""
        actor = action.actor
        assert actor is not None
        kind = action.type

        if kind == ActionType.SPEAK:
            is_wolf_chat = state.phase in (
                Phase.NIGHT_WOLF_CHAT,
                Phase.NIGHT_WOLF_RUNOFF_SPEECH,
            )
            emit(
                "wolf_chat" if is_wolf_chat else "speech",
                Visibility.WOLF if is_wolf_chat else Visibility.PUBLIC,
                actor=actor,
                payload={"message": action.message},
                render=RenderText(text=f"{actor} 号：{action.message}"),
            )
            return

        if kind in (ActionType.WOLF_VOTE_KILL, ActionType.VOTE):
            state.votes[actor] = action.target
            # 狼人出刀与白天投票共用 vote_cast，展示文本按动作类型分叉，
            # 免得前端把这条赤裸的 {"target": 6} 直接摊给用户看。
            text = (
                f"{actor} 号选择刀 {action.target} 号"
                if kind == ActionType.WOLF_VOTE_KILL
                else f"{actor} 号投给 {action.target} 号"
            )
            emit(
                "vote_cast",
                Visibility.SPECTATOR,
                actor=actor,
                payload={"target": action.target},
                render=RenderText(text=text),
            )
            return

        if kind == ActionType.ABSTAIN:
            state.votes[actor] = None
            emit(
                "vote_abstain",
                Visibility.SPECTATOR,
                actor=actor,
                payload={},
                render=RenderText(text=f"{actor} 号弃权"),
            )
            return

        if kind == ActionType.SEER_CHECK:
            target = action.target
            assert target is not None
            faction = rules.faction_of(state.players[target].role)
            state.night.seer_target = target
            state.night.seer_result = faction
            payload: dict[str, object] = {"seat": target, "faction": faction.value}
            if not state.board.seer.reveal_faction_only:
                payload["role"] = state.players[target].role.value
            label = "狼人" if faction == Faction.WOLF else "好人"
            emit(
                "seer_check",
                Visibility.SEER,
                actor=actor,
                payload=payload,
                render=RenderText(text=f"你查验了 {target} 号，结果是：{label}"),
            )
            return

        if kind == ActionType.WITCH_SAVE:
            state.night.saved = True
            state.potions.antidote_left = False
            emit(
                "witch_save",
                Visibility.WITCH,
                actor=actor,
                payload={"seat": state.night.kill_target},
                render=RenderText(text=f"你用解药救了 {state.night.kill_target} 号"),
            )
            return

        if kind == ActionType.WITCH_POISON:
            target = action.target
            assert target is not None
            state.night.poison_target = target
            state.potions.poison_left = False
            payload = {"seat": target}
            if state.board.witch.knows_poisoned_identity:
                payload["role"] = state.players[target].role.value  # type: ignore[assignment]
            emit(
                "witch_poison",
                Visibility.WITCH,
                actor=actor,
                payload=payload,
                render=RenderText(text=f"你用毒药毒了 {target} 号"),
            )
            return

        if kind == ActionType.WITCH_PASS:
            emit(
                "witch_pass",
                Visibility.WITCH,
                actor=actor,
                payload={},
                render=RenderText(text="你今夜不用药"),
            )
            return

        if kind == ActionType.HUNTER_SHOOT:
            state.pending_hunter_target = action.target
            emit(
                "hunter_shoot",
                Visibility.PUBLIC,
                actor=actor,
                payload={"seat": action.target},
                render=RenderText(text=f"{actor} 号开枪射向 {action.target} 号"),
            )
            return

        if kind == ActionType.HUNTER_HOLD:
            emit(
                "hunter_hold",
                Visibility.PUBLIC,
                actor=actor,
                payload={},
                render=RenderText(text=f"{actor} 号放弃了开枪"),
            )
            return

        if kind == ActionType.LAST_WORDS:
            emit(
                "last_words",
                Visibility.PUBLIC,
                actor=actor,
                payload={"message": action.message},
                render=RenderText(text=f"{actor} 号遗言：{action.message}"),
            )
            return

        if kind == ActionType.REFLECT:
            emit(
                "reflect",
                Visibility.PUBLIC,
                actor=actor,
                payload={"message": action.message},
                render=RenderText(text=f"{actor} 号感想：{action.message}"),
            )
            return

        raise IllegalActionError(f"未知动作类型：{kind}")

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------

    def _validate(self, state: GameState, action: Action) -> None:
        legal = self.legal_actions(state)

        if action.type == ActionType.ADVANCE:
            if legal:
                seats = sorted({la.seat for la in legal})
                raise IllegalActionError(
                    f"阶段 {state.phase.value} 还有 {seats} 号未行动，不能推进"
                )
            if state.phase == Phase.GAME_OVER:
                raise IllegalActionError("对局已结束，不能再推进")
            return

        if action.actor is None:
            raise IllegalActionError("动作缺少 actor")

        mine = [la for la in legal if la.seat == action.actor]
        if not mine:
            raise IllegalActionError(f"{action.actor} 号在阶段 {state.phase.value} 无需行动")

        allowed = {la.type for la in mine}
        if action.type not in allowed:
            choices = "、".join(t.value for t in sorted(allowed, key=lambda x: x.value))
            raise IllegalActionError(
                f"{action.actor} 号在此阶段不能执行 {action.type.value}，可选：{choices}"
            )

        spec = next(la for la in mine if la.type == action.type)
        if spec.allowed_targets:
            if action.target is None:
                raise IllegalActionError(f"{action.type.value} 必须指定目标座位号")
            if action.target not in spec.allowed_targets:
                raise IllegalActionError(
                    f"目标 {action.target} 号不合法，请在 {spec.allowed_targets} 中选择"
                )

        if action.type in SPEAKING_TYPES:
            message = (action.message or "").strip()
            if not message:
                raise IllegalActionError(f"{action.type.value} 的内容不能为空")
            limit = state.board.day.speech_max_chars
            if len(message) > limit:
                raise IllegalActionError(
                    f"内容 {len(message)} 字，超过上限 {limit} 字，请缩短后重试"
                )
            action.message = message

    # ------------------------------------------------------------------
    # 兜底用的确定性随机
    # ------------------------------------------------------------------

    def random_pick(self, state: GameState, candidates: list[int]) -> tuple[int, int]:
        """从候选中确定性地选一个，返回 ``(选中值, 新的 rng_step)``。

        调用方需要用返回的 step 更新状态，否则同一步会反复选出同一个值。
        """
        if not candidates:
            raise ValueError("候选列表为空，无法兜底选择")
        rng = random.Random(f"{state.seed}:{state.rng_step}")
        return candidates[rng.randrange(len(candidates))], state.rng_step + 1
