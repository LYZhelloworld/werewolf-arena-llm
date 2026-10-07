"""终端渲染：把事件流画成可读的中文观战文本。

观众视角默认能看到一切（底牌、狼人频道、夜间死亡、每个席位的内心活动）；
``--view N`` 切换成某位玩家的视角后，事件先过 ``engine.visibility.filter_events_for``
再进渲染器，所以屏幕上剩下的应该正好是他该知道的——这是信息隔离最直观的自检方式。

内心活动（CoT）**不需要单独的开关判断**：玩家视角的 ``event.thinking``
已被过滤阶段物理置为 ``None``，渲染器只要「有就打印」，天然只对观众可见。
``--no-cot`` 只是给观众一个「别刷屏」的开关。

颜色只在交互式终端里开：管道输出、``NO_COLOR``、不支持 ANSI 的旧控制台
一律降级为纯文本。
"""

from __future__ import annotations

import os
import sys
from typing import Any, TextIO

from engine.events import Event, Visibility
from engine.state import PHASE_LABELS, ROLE_LABELS, DeathCause, Role

CAUSE_LABELS: dict[str, str] = {
    DeathCause.KILL.value: "被狼人刀死",
    DeathCause.POISON.value: "被女巫毒死",
    DeathCause.LYNCH.value: "被投票出局",
    DeathCause.SHOT.value: "被猎人开枪",
}

# 非公开频道在终端上要显式标注，避免观众误以为玩家也看到了
CHANNEL_MARKS: dict[str, str] = {
    Visibility.WOLF.value: "仅狼人可见",
    Visibility.SEER.value: "仅预言家可见",
    Visibility.WITCH.value: "仅女巫可见",
    Visibility.SELF.value: "仅本人可见",
    Visibility.SPECTATOR.value: "仅观众可见",
}

WINNER_LABELS = {"wolf": "狼人阵营获胜", "good": "好人阵营获胜"}

# 会跑多轮的阶段，标题上加「第 N 轮」区分，否则同一晚会出现两个一模一样的标题
ROUND_PHASES = frozenset(
    {"night_wolf_chat", "night_wolf_runoff_speech", "day_discussion", "day_runoff_speech"}
)

_RESET = "0"
_STYLES = {
    "bold": "1",
    "dim": "2",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "blue": "34",
    "magenta": "35",
    "cyan": "36",
    "gray": "90",
}


def _enable_windows_ansi() -> None:
    """Windows 旧版控制台默认不解析 ANSI 转义序列，开一下虚拟终端模式。

    失败就降级为无色输出——这是终端能力问题，不是程序错误。
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except (OSError, AttributeError, ValueError):
        pass


class TerminalRenderer:
    """把事件逐条打印成中文行。"""

    def __init__(
        self,
        stream: TextIO | None = None,
        color: bool | None = None,
        show_thinking: bool = True,
        cot_chars: int = 600,
    ) -> None:
        self.stream = stream if stream is not None else sys.stdout
        if color is None:
            color = self._terminal_supports_color()
        self.color = color
        # 观众视角下是否打印每个席位的内心活动（玩家视角本就没有可打）
        self.show_thinking = show_thinking
        # 单条内心活动最长打印多少字；<= 0 表示不截断。
        # 本地小模型的 CoT 动辄数千字且大量重复，不截断会把终端刷爆。
        self.cot_chars = cot_chars
        if color:
            _enable_windows_ansi()
        # 阶段标题延迟到该阶段第一条内容出现时再打，避免空阶段留下一堆孤零零的标题
        self._pending_header: str | None = None
        self._last_phase: str | None = None
        self._round = 1

    def _terminal_supports_color(self) -> bool:
        if os.environ.get("NO_COLOR"):
            return False
        return bool(getattr(self.stream, "isatty", lambda: False)())

    # ------------------------------------------------------------------
    # 基础输出
    # ------------------------------------------------------------------

    def paint(self, text: str, *styles: str) -> str:
        if not self.color or not styles:
            return text
        codes = "".join(f"\033[{_STYLES[s]}m" for s in styles if s in _STYLES)
        return f"{codes}{text}\033[{_RESET}m"

    def write(self, line: str = "") -> None:
        print(line, file=self.stream)

    def render(self, events: list[Event]) -> None:
        """渲染一批事件。``None`` 表示该事件在此视角下不展示。"""
        for event in events:
            if event.type == "phase_enter":
                self._pending_header = self._phase_header(event)
                continue
            line = self.line_for(event)
            if line is None:
                continue
            self._flush_header()
            thought = self._thinking_line(event)
            if thought is not None:
                self.write(thought)
            self.write(line)

    def _flush_header(self) -> None:
        if self._pending_header is None:
            return
        self.write("")
        self.write(self._pending_header)
        self._pending_header = None

    def _phase_header(self, event: Event) -> str:
        if event.phase == self._last_phase and event.phase in ROUND_PHASES:
            self._round += 1
        else:
            self._round = 1
        self._last_phase = event.phase

        label = PHASE_LABELS.get(event.phase, event.phase)
        suffix = f" · 第 {self._round} 轮" if event.phase in ROUND_PHASES else ""
        return self.paint(f"── 第 {event.day} 天 · {label}{suffix} ──", "bold", "cyan")

    # ------------------------------------------------------------------
    # 事件 → 文本
    # ------------------------------------------------------------------

    def line_for(self, event: Event) -> str | None:
        handler = getattr(self, f"_on_{event.type}", None)
        if handler is not None:
            return handler(event)
        # 未知类型：至少把引擎给的展示文本打出来，不要静默吞掉
        if event.render is not None:
            return self.paint(f"  {event.render.text}", "dim")
        return self.paint(f"  [{event.type}] {event.payload}", "dim")

    def _channel(self, event: Event) -> str:
        """私有频道的中文标记，公开事件为空串。"""
        mark = CHANNEL_MARKS.get(event.visibility.value)
        return self.paint(f"（{mark}）", "gray") if mark else ""

    def _thinking_line(self, event: Event) -> str | None:
        """观众视角下该席位此刻的内心活动。

        玩家视角的 ``thinking`` 已在过滤阶段被物理剔除（恒为 ``None``），
        所以这里「有就打印」天然只对观众可见，不会泄漏给玩家。
        ``source`` 一并打出来，一眼看清 CoT 是从推理字段、正文还是 think 工具取的。
        """
        if not self.show_thinking or event.thinking is None:
            return None
        text = " ".join((event.thinking.text or "").split())
        if not text:
            return None
        total = len(text)
        if self.cot_chars > 0 and total > self.cot_chars:
            text = f"{text[: self.cot_chars]}……（共 {total} 字）"
        seat = f"{event.actor} 号" if event.actor is not None else "系统"
        label = f"    〔内心〕{seat}（{event.thinking.source}）：{text}"
        return self.paint(label, "dim", "magenta")

    def _seat(self, seat: Any) -> str:
        return self.paint(f"{seat} 号", "bold")

    def _on_game_start(self, event: Event) -> str:
        seats = event.payload.get("seats", [])
        return self.paint(f"=== 对局开始 {event.game_id} · 共 {len(seats)} 人 ===", "bold", "cyan")

    def _on_role_assigned(self, event: Event) -> str:
        roles = event.payload.get("roles", {})
        text = " ".join(
            f"{seat}={ROLE_LABELS.get(Role(role), role)}" for seat, role in sorted(roles.items())
        )
        return f"{self._channel(event)} 底牌：{text}"

    def _on_phase_enter(self, event: Event) -> str:
        return self._phase_header(event)

    def _on_speech(self, event: Event) -> str:
        return f"  {self._seat(event.actor)}：{event.payload.get('message', '')}"

    def _on_wolf_chat(self, event: Event) -> str:
        msg = event.payload.get("message", "")
        return f"{self._channel(event)} {self.paint(f'{event.actor} 号', 'bold', 'magenta')}：{msg}"

    def _on_last_words(self, event: Event) -> str:
        return self.paint(f"  {event.actor} 号遗言：{event.payload.get('message', '')}", "yellow")

    def _on_reflect(self, event: Event) -> str:
        return self.paint(f"  {event.actor} 号感想：{event.payload.get('message', '')}", "blue")

    def _on_vote_cast(self, event: Event) -> str:
        return self.paint(
            f"{self._channel(event)} {event.actor} 号投给 {event.payload.get('target')} 号", "dim"
        )

    def _on_vote_abstain(self, event: Event) -> str:
        return self.paint(f"{self._channel(event)} {event.actor} 号弃权", "dim")

    def _on_vote_result(self, event: Event) -> str:
        target = event.payload.get("target")
        detail = self._counts_text(event.payload.get("counts", {}))
        mark = self._channel(event)

        if event.payload.get("second_tie"):
            return self.paint(f"  {mark} 二次平票，本日无人出局（{detail}）", "yellow")
        if event.payload.get("tie_broken"):
            return self.paint(
                f"  {mark} 二次平票，按座位号最小定为 {target} 号（{detail}）", "yellow"
            )
        if target is None:
            return self.paint(f"  {mark} 无人出局（{detail or '全部弃权'}）", "dim")
        return self.paint(f"  {mark} 结果：{target} 号出局（{detail}）", "bold")

    def _on_vote_tie(self, event: Event) -> str:
        tied = "、".join(f"{s} 号" for s in event.payload.get("tied", []))
        detail = self._counts_text(event.payload.get("counts", {}))
        return self.paint(f"  {self._channel(event)} 平票：{tied}（{detail}）", "yellow")

    def _counts_text(self, counts: dict) -> str:
        return " ".join(f"{seat}号{count}票" for seat, count in sorted(counts.items()))

    def _on_seer_check(self, event: Event) -> str:
        seat = event.payload.get("seat")
        faction = "狼人" if event.payload.get("faction") == "wolf" else "好人"
        # reveal_faction_only 时 payload 里没有 role，不要编一个出来
        role = event.payload.get("role")
        detail = f"{faction}（{role}）" if role else faction
        return f"{self._channel(event)} {event.actor} 号查验了 {seat} 号：{detail}"

    def _on_witch_save(self, event: Event) -> str:
        return f"{self._channel(event)} {event.actor} 号用解药救了 {event.payload.get('seat')} 号"

    def _on_witch_poison(self, event: Event) -> str:
        return f"{self._channel(event)} {event.actor} 号用毒药毒了 {event.payload.get('seat')} 号"

    def _on_witch_pass(self, event: Event) -> str:
        return f"{self._channel(event)} {event.actor} 号今晚不用药"

    def _on_wolf_kill(self, event: Event) -> str:
        saved = "（被女巫救下）" if event.payload.get("saved") else ""
        return f"{self._channel(event)} 狼队刀口：{event.payload.get('seat')} 号{saved}"

    def _on_hunter_shoot(self, event: Event) -> str:
        return self.paint(f"  {event.actor} 号开枪射向 {event.payload.get('seat')} 号", "red")

    def _on_hunter_hold(self, event: Event) -> str:
        return self.paint(f"  {event.actor} 号放弃了开枪", "dim")

    def _on_death(self, event: Event) -> str:
        cause = CAUSE_LABELS.get(str(event.payload.get("cause")), "出局")
        seat = event.payload.get("seat")
        return self.paint(f"  {self._channel(event)} {seat} 号{cause}", "red")

    def _on_night_deaths_announced(self, event: Event) -> str:
        seats = event.payload.get("seats", [])
        if not seats:
            return self.paint("  昨夜是平安夜，无人出局", "green")
        names = "、".join(f"{s} 号" for s in seats)
        return self.paint(f"  昨夜出局：{names}", "red")

    def _on_roles_revealed(self, event: Event) -> str:
        roles = event.payload.get("roles", {})
        text = " ".join(
            f"{seat}={ROLE_LABELS.get(Role(role), role)}" for seat, role in sorted(roles.items())
        )
        return self.paint(f"  全部身份：{text}", "bold")

    def _on_game_over(self, event: Event) -> str:
        winner = WINNER_LABELS.get(str(event.payload.get("winner")), "未知")
        return self.paint(f"\n=== 对局结束：{winner} ===", "bold", "green")

    # ------------------------------------------------------------------
    # agent 侧事件（记忆、兜底、非法动作、CoT 降级）——必须带上是谁做的
    # ------------------------------------------------------------------

    def _on_memory_write(self, event: Event) -> str:
        key = event.payload.get("key", "")
        content = event.payload.get("content", "")
        return self.paint(
            f"{self._channel(event)} {self._seat(event.actor)} 记下「{key}」：{content}", "dim"
        )

    def _on_memory_read(self, event: Event) -> str:
        keys = event.payload.get("keys") or []
        body = "、".join(str(k) for k in keys) if keys else "（暂无记忆）"
        return self.paint(f"{self._channel(event)} {self._seat(event.actor)} 回忆：{body}", "dim")

    def _on_action_fallback(self, event: Event) -> str:
        reason = event.payload.get("reason", "")
        return self.paint(
            f"{self._channel(event)} {self._seat(event.actor)} 走兜底：{reason}", "yellow"
        )

    def _on_action_rejected(self, event: Event) -> str:
        tool = event.payload.get("tool", "")
        reason = event.payload.get("reason", "")
        return self.paint(
            f"{self._channel(event)} {self._seat(event.actor)} 动作被拒（{tool}）：{reason}",
            "yellow",
        )

    def _on_llm_downgrade(self, event: Event) -> str:
        reason = event.payload.get("reason", "")
        return self.paint(
            f"{self._channel(event)} {self._seat(event.actor)} CoT 回传被拒，已降级：{reason}",
            "yellow",
        )
