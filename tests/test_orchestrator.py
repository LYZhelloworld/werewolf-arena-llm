"""编排层单测：随机策略能跑完整局、非法动作能兜底、渲染与 CLI 行为正确。"""

from __future__ import annotations

import io
import shutil
from pathlib import Path

import pytest

from config import BoardConfig
from engine.actions import Action, ActionType, LegalAction
from engine.engine import RuleEngine
from engine.events import Event, Thinking, Visibility
from engine.state import GameState, Phase
from orchestrator.cli import main
from orchestrator.game_loop import GameRunner
from orchestrator.policies import FirstLegalPolicy, RandomPolicy
from orchestrator.render import TerminalRenderer

CONFIG_SRC = Path(__file__).resolve().parent.parent / "configs"


def _event(
    seq: int,
    type_: str,
    visibility: Visibility = Visibility.PUBLIC,
    day: int = 1,
    phase: str = "day_discussion",
    actor: int | None = None,
    payload: dict | None = None,
    thinking: Thinking | None = None,
) -> Event:
    return Event(
        seq=seq,
        game_id="test",
        day=day,
        phase=phase,
        type=type_,
        visibility=visibility,
        actor=actor,
        payload=payload or {},
        thinking=thinking,
    )


def _phase_enter(seq: int, phase: str, day: int = 1) -> Event:
    return _event(seq, "phase_enter", day=day, phase=phase, payload={"phase": phase, "day": day})


def _run_one(seed: int) -> tuple[GameState, list[Event], GameRunner]:
    engine = RuleEngine(BoardConfig(), seed=seed)
    state, opening = engine.new_game()
    runner = GameRunner(engine, RandomPolicy(seed=seed))
    state, events = runner.run(state)
    return state, opening + events, runner


# ---------------------------------------------------------------------------
# 策略与主循环
# ---------------------------------------------------------------------------


def test_random_policy_finishes_every_game() -> None:
    for seed in range(15):
        state, events, runner = _run_one(seed)
        assert state.phase == Phase.GAME_OVER, f"seed={seed} 没有终局"
        assert state.winner is not None
        assert [e.seq for e in events] == list(range(1, len(events) + 1))
        assert runner.rejections == [], f"seed={seed} 随机策略不该产生非法动作"


def test_game_has_speeches_votes_and_postgame() -> None:
    _, events, _ = _run_one(3)
    types = {e.type for e in events}
    assert {"speech", "wolf_chat", "vote_result", "reflect", "game_over"} <= types


def test_illegal_action_falls_back_and_is_recorded() -> None:
    """决策方给出非法动作时，编排层改用兜底策略，游戏不受影响。"""

    class BrokenPolicy:
        """永远投一个不在候选里的座位。"""

        def decide(self, state: GameState, legal: list[LegalAction]) -> Action:
            spec = legal[0]
            if spec.allowed_targets:
                return Action(actor=spec.seat, type=spec.type, target=999)
            return Action(actor=spec.seat, type=spec.type, message="嗯")

    engine = RuleEngine(BoardConfig(), seed=11)
    state, _ = engine.new_game()
    runner = GameRunner(engine, BrokenPolicy(), fallback=FirstLegalPolicy())
    state, events = runner.run(state)

    assert state.phase == Phase.GAME_OVER
    assert runner.rejections, "非法动作必须被记录下来"
    assert all(r.action for r in runner.rejections)
    # 兜底仍然把流程走完了，投票结果照常产生
    assert any(e.type == "vote_result" for e in events)


def test_first_legal_policy_picks_the_smallest_target() -> None:
    legal = [
        LegalAction(seat=2, type=ActionType.VOTE, allowed_targets=[7, 4, 9]),
        LegalAction(seat=2, type=ActionType.ABSTAIN),
    ]
    action = FirstLegalPolicy().decide(GameState(game_id="t"), legal)
    assert action.type == ActionType.VOTE
    assert action.target == 4


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def test_renderer_prints_speech_and_marks_private_channel() -> None:
    buf = io.StringIO()
    renderer = TerminalRenderer(stream=buf, color=False)
    renderer.render(
        [
            _event(1, "speech", actor=3, payload={"message": "我是好人"}),
            _event(
                2,
                "wolf_chat",
                visibility=Visibility.WOLF,
                phase="night_wolf_chat",
                actor=6,
                payload={"message": "刀 3 号"},
            ),
        ]
    )
    text = buf.getvalue()
    assert "3 号：我是好人" in text
    assert "（仅狼人可见）" in text
    assert "6 号：刀 3 号" in text


def test_renderer_numbers_discussion_rounds() -> None:
    buf = io.StringIO()
    renderer = TerminalRenderer(stream=buf, color=False)
    renderer.render(
        [
            _phase_enter(1, "day_discussion"),
            _event(2, "speech", actor=1, payload={"message": "第一轮"}),
            _phase_enter(3, "day_discussion"),
            _event(4, "speech", actor=2, payload={"message": "第二轮"}),
        ]
    )
    text = buf.getvalue()
    assert "第 1 轮" in text and "第 2 轮" in text


def test_renderer_skips_phases_without_content() -> None:
    """没有人行动的阶段（比如预言家已死）不该留下一个空标题。"""
    buf = io.StringIO()
    renderer = TerminalRenderer(stream=buf, color=False)
    renderer.render([_phase_enter(1, "night_seer")])
    assert buf.getvalue() == ""
    renderer.render(
        [_phase_enter(2, "day_discussion"), _event(3, "speech", actor=1, payload={"message": "喂"})]
    )
    assert "白天 · 自由讨论" in buf.getvalue()


def test_renderer_color_is_off_for_pipes() -> None:
    buf = io.StringIO()
    assert TerminalRenderer(stream=buf).color is False
    assert TerminalRenderer(stream=buf, color=True).paint("x", "red") == "\033[31mx\033[0m"


def test_renderer_shows_memory_events_with_actor() -> None:
    """记忆读写必须带上是哪个席位做的，否则观众认不出是谁写的。"""
    buf = io.StringIO()
    renderer = TerminalRenderer(stream=buf, color=False)
    renderer.render(
        [
            _event(
                1,
                "memory_write",
                visibility=Visibility.SELF,
                actor=5,
                payload={"key": "身份", "content": "5号是猎人"},
            ),
            _event(
                2,
                "memory_read",
                visibility=Visibility.SELF,
                actor=9,
                payload={"keys": ["8号遗言"]},
            ),
        ]
    )
    text = buf.getvalue()
    assert "5 号" in text and "记下「身份」：5号是猎人" in text
    assert "9 号" in text and "8号遗言" in text
    assert "（仅本人可见）" in text


def test_renderer_shows_agent_alert_events() -> None:
    """兜底 / 非法动作 / CoT 降级都要带上席位与原因，不能只剩一个裸 payload。"""
    buf = io.StringIO()
    renderer = TerminalRenderer(stream=buf, color=False)
    renderer.render(
        [
            _event(
                1,
                "action_rejected",
                visibility=Visibility.SPECTATOR,
                actor=3,
                payload={"tool": "vote", "reason": "目标不在可选范围"},
            ),
            _event(
                2,
                "llm_downgrade",
                visibility=Visibility.SPECTATOR,
                actor=4,
                payload={"reason": "回传 thinking 被拒"},
            ),
            _event(
                3,
                "action_fallback",
                visibility=Visibility.SPECTATOR,
                actor=6,
                payload={"reason": "轮数用尽"},
            ),
        ]
    )
    text = buf.getvalue()
    assert "3 号 动作被拒（vote）：目标不在可选范围" in text
    assert "4 号 CoT 回传被拒，已降级：回传 thinking 被拒" in text
    assert "6 号 走兜底：轮数用尽" in text


def test_renderer_prints_thinking_when_present() -> None:
    """观众视角下把该席位的内心活动打在它的动作之前。"""
    buf = io.StringIO()
    renderer = TerminalRenderer(stream=buf, color=False)
    renderer.render(
        [
            _event(
                1,
                "speech",
                actor=8,
                payload={"message": "我是预言家"},
                thinking=Thinking(text="我昨晚验了 1 号，是狼。", source="reasoning_content"),
            ),
            _event(2, "speech", actor=2, payload={"message": "我先听听"}),
        ]
    )
    text = buf.getvalue()
    assert "〔内心〕8 号（reasoning_content）：我昨晚验了 1 号，是狼。" in text
    # 没有 thinking 的事件不该凭空多出一行
    assert text.count("〔内心〕") == 1
    # 内心活动要排在对应动作之前
    assert text.index("〔内心〕") < text.index("8 号：我是预言家")


def test_renderer_no_cot_switch_hides_thinking() -> None:
    buf = io.StringIO()
    renderer = TerminalRenderer(stream=buf, color=False, show_thinking=False)
    renderer.render(
        [
            _event(
                1,
                "speech",
                actor=8,
                payload={"message": "喂"},
                thinking=Thinking(text="秘密想法", source="content"),
            )
        ]
    )
    text = buf.getvalue()
    assert "〔内心〕" not in text
    assert "8 号：喂" in text


def test_renderer_truncates_long_thinking_but_can_be_disabled() -> None:
    """本地小模型的 CoT 很长，默认截断；--cot-chars 0 时不截断。"""
    long_text = "想" * 900
    event = _event(
        1,
        "speech",
        actor=1,
        payload={"message": "喂"},
        thinking=Thinking(text=long_text, source="content"),
    )

    buf = io.StringIO()
    TerminalRenderer(stream=buf, color=False, cot_chars=100).render([event])
    text = buf.getvalue()
    assert "想" * 100 in text
    assert "想" * 101 not in text
    assert "……（共 900 字）" in text

    buf2 = io.StringIO()
    TerminalRenderer(stream=buf2, color=False, cot_chars=0).render([event])
    assert "想" * 900 in buf2.getvalue()
    assert "……" not in buf2.getvalue()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    for name in ("board", "game"):
        shutil.copy(CONFIG_SRC / f"{name}.example.yaml", tmp_path / f"{name}.yaml")
    return tmp_path


def test_cli_exits_when_config_is_missing(tmp_path: Path, capsys) -> None:
    assert main(["--config-dir", str(tmp_path)]) == 1
    assert "cp" in capsys.readouterr().err


def test_cli_batch_summary(config_dir: Path, capsys) -> None:
    code = main(
        ["--config-dir", str(config_dir), "--games", "3", "--seed", "7", "--summary", "--no-save"]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "共 3 局" in out
    assert out.count("胜方=") == 3


def test_cli_writes_archive(config_dir: Path, tmp_path: Path, monkeypatch) -> None:
    # persist.root 是相对路径 data，切到临时目录跑，避免污染仓库
    monkeypatch.chdir(tmp_path)
    code = main(["--config-dir", str(config_dir), "--games", "1", "--seed", "42", "--summary"])
    assert code == 0
    game_dirs = [p for p in (tmp_path / "data" / "games").iterdir() if p.is_dir()]
    assert len(game_dirs) == 1
    game_dir = game_dirs[0]
    assert (game_dir / "events.json").exists()
    assert (game_dir / "meta.json").exists()
    assert (tmp_path / "data" / "games" / "index.json").exists()
    # 归档后 jsonl 是多余的，必须清掉——只留一份权威事件数据
    assert not (game_dir / "events.jsonl").exists()
