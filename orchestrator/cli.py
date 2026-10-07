"""命令行入口：跑一局（或多局）对局，终端彩色输出并落盘为 JSON。

M2 阶段用的是随机策略而非 LLM——目的是在接入模型之前，先把规则引擎、
编排层与持久化端到端跑通。接 LLM 之后本文件的流程不用改，
只换掉注入的 Policy。

用法::

    uv run python -m orchestrator.cli                  # 跑一局，上帝视角，彩色输出
    uv run python -m orchestrator.cli --view 3         # 以 3 号玩家的视角观战
    uv run python -m orchestrator.cli --games 50 --summary   # 批量跑，只看结果
    uv run python -m orchestrator.cli --no-save        # 不落盘，只看终端
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from config import BoardConfig, ConfigError, GameConfig, load_board, load_game
from engine.engine import RuleEngine
from engine.events import Event
from engine.state import GameState
from engine.visibility import filter_events_for
from orchestrator.game_loop import GameRunner
from orchestrator.policies import RandomPolicy
from orchestrator.render import TerminalRenderer
from storage.event_log import EventLog
from storage.index import update_index
from storage.meta import build_meta

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parent.parent / "configs"
SPECTATOR_VIEWS = ("spectator", "god", "all")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m orchestrator.cli",
        description="九人狼人杀：用随机策略跑完整局（M2，尚未接入 LLM）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  uv run python -m orchestrator.cli\n"
            "  uv run python -m orchestrator.cli --view 3\n"
            "  uv run python -m orchestrator.cli --games 50 --summary\n"
        ),
    )
    parser.add_argument("--seed", type=int, default=None, help="随机种子；留空则每局随机")
    parser.add_argument("--games", type=int, default=1, help="跑几局，默认 1")
    parser.add_argument(
        "--delay", type=float, default=0.0, help="每批事件之间停顿的秒数，用于放慢观战节奏"
    )
    parser.add_argument(
        "--view",
        default="spectator",
        help="视角：spectator（默认，上帝视角）或座位号 1-9",
    )
    parser.add_argument("--summary", action="store_true", help="只打印每局一行摘要")
    parser.add_argument("--no-color", action="store_true", help="关闭彩色输出")
    parser.add_argument("--no-save", action="store_true", help="不写入 data/ 存档")
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=DEFAULT_CONFIG_DIR,
        help=f"配置目录，默认 {DEFAULT_CONFIG_DIR.as_posix()}",
    )
    return parser


def parse_view(value: str, total_seats: int) -> int | None:
    """把 ``--view`` 参数解析成座位号；``None`` 表示观众视角。"""
    if value.lower() in SPECTATOR_VIEWS:
        return None
    try:
        seat = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--view 只能是 spectator 或 1-{total_seats} 的座位号，实际是 {value!r}"
        ) from None
    if not 1 <= seat <= total_seats:
        raise argparse.ArgumentTypeError(f"--view 座位号超出范围：{seat}，本局共 {total_seats} 人")
    return seat


def _visible_to(batch: list[Event], state: GameState, seat: int | None) -> list[Event]:
    """按视角过滤事件。观众视角看全量；玩家视角走引擎的隔离逻辑。"""
    if seat is None:
        return batch
    player = state.players[seat]
    return filter_events_for(batch, seat, player.role, player.alive)


def play_one_game(
    board: BoardConfig,
    game: GameConfig,
    seed: int | None,
    view_seat: int | None,
    renderer: TerminalRenderer,
    *,
    show_detail: bool,
    delay: float,
    save: bool,
) -> dict:
    """跑一局，返回摘要字典。渲染与落盘都在这里接好。"""
    started_at = datetime.now(UTC)
    engine = RuleEngine(board, seed=seed)
    state, opening = engine.new_game()

    log = None if not save else EventLog(game.persist.root, state.game_id)

    def on_events(batch: list[Event], current: GameState) -> None:
        # 落盘永远是全量，跟观战视角无关——复盘要能看到一切
        if log is not None:
            log.append(batch)
        if show_detail:
            renderer.render(_visible_to(batch, current, view_seat))
            if delay > 0:
                time.sleep(delay)

    if log is not None:
        log.append(opening)
    if show_detail:
        renderer.render(_visible_to(opening, state, view_seat))

    runner = GameRunner(engine, RandomPolicy(seed=seed), on_events=on_events)
    state, collected = runner.run(state)
    events = opening + collected

    if log is not None:
        log.archive(events)
        log.write_meta(build_meta(state, started_at=started_at))
        update_index(
            game.persist.root,
            {
                "game_id": state.game_id,
                "started_at": started_at.isoformat(),
                "seed": seed,
                "models": {},  # M2 还没有模型，占位
                "winner": state.winner.value if state.winner else None,
                "days": state.day,
                "event_count": len(events),
                "path": f"games/{state.game_id}",
            },
        )

    return {
        "game_id": state.game_id,
        "seed": seed,
        "winner": state.winner.value if state.winner else None,
        "days": state.day,
        "events": len(events),
        "rejections": len(runner.rejections),
        "dir": log.dir.as_posix() if log is not None else None,
    }


def _summary_line(result: dict) -> str:
    winner = {"wolf": "狼人", "good": "好人"}.get(result["winner"] or "", "未分胜负")
    return (
        f"{result['game_id']}  胜方={winner}  天数={result['days']}  "
        f"事件={result['events']}  兜底={result['rejections']}  存档={result['dir'] or '未保存'}"
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        board = load_board(args.config_dir)
        game = load_game(args.config_dir)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1

    total_seats = sum(int(v) for v in board.roles.model_dump().values())
    view_seat = parse_view(args.view, total_seats)
    show_detail = not args.summary
    renderer = TerminalRenderer(color=not args.no_color)

    if show_detail and view_seat is not None:
        renderer.write(f"（以 {view_seat} 号视角观战：看不到底牌、狼人频道与别人的私有信息）")

    started = time.perf_counter()
    results = [
        play_one_game(
            board,
            game,
            seed=None if args.seed is None else args.seed + i,
            view_seat=view_seat,
            renderer=renderer,
            show_detail=show_detail,
            delay=args.delay,
            save=not args.no_save,
        )
        for i in range(args.games)
    ]
    elapsed = time.perf_counter() - started

    if not show_detail or args.games > 1:
        for result in results:
            print(_summary_line(result))
    elif results[0]["dir"] is not None:
        print(f"\n存档：{results[0]['dir']}")

    winners = [r["winner"] for r in results]
    print(
        f"\n共 {len(results)} 局，用时 {elapsed:.2f}s："
        f"狼人 {winners.count('wolf')} 胜 / 好人 {winners.count('good')} 胜"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
