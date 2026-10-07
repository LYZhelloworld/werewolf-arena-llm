"""命令行入口：跑一局（或多局）对局，终端彩色输出并落盘为 JSON。

策略有两种，由参数决定：

- ``--mock``（默认）：脚本化客户端顶替 LLM，不需要 API key。
  用来验证编排、工具协议、CoT 捕获与持久化链路。
- ``--model NAME``：接真实模型，9 席全部用这一个模型（会有实际费用）。

用法::

    uv run python -m orchestrator.cli                  # 跑一局，上帝视角，彩色输出
    uv run python -m orchestrator.cli --view 3         # 以 3 号玩家的视角观战
    uv run python -m orchestrator.cli --games 50 --summary   # 批量跑，只看结果
    uv run python -m orchestrator.cli --no-save        # 不落盘，只看终端
    uv run python -m orchestrator.cli --model deepseek-chat   # 接真实模型
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from agents.client import OpenAICompatClient
from agents.mock import MockClient, auto_responder
from agents.policy import LLMPolicy, SeatSpec, specs_from_models
from config import (
    BoardConfig,
    ConfigError,
    GameConfig,
    ModelsConfig,
    api_key_report,
    load_board,
    load_game,
    load_models,
    resolve_api_key,
)
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
MOCK_MODEL = "mock"


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
    parser.add_argument(
        "--model",
        default=None,
        help="用 configs/models.yaml 里的哪个模型（默认 --mock，不接真实模型）",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="用脚本化客户端顶替 LLM（默认行为），不需要 API key",
    )
    parser.add_argument(
        "--random",
        action="store_true",
        help="用随机策略跑，完全不产生 LLM 调用（压规则用，最快）",
    )
    parser.add_argument("--no-color", action="store_true", help="关闭彩色输出")
    parser.add_argument(
        "--no-cot", action="store_true", help="观众视角下不打印各席位的内心活动（默认打印）"
    )
    parser.add_argument(
        "--cot-chars",
        type=int,
        default=600,
        help="单条内心活动最多打印多少字，0 表示不截断（默认 600）",
    )
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


def build_policy(
    game: GameConfig,
    total_seats: int,
    *,
    model: str | None = None,
    random: bool = False,
    seed: int | None = None,
    config_dir: Path = DEFAULT_CONFIG_DIR,
) -> tuple[object, dict[str, str]]:
    """按参数构造策略，返回 ``(policy, 席位→模型名)``。

    ``model`` 没给就用脚本化客户端顶替 LLM：链路照跑，不产生任何费用。

    参数刻意写成显式关键字而非 ``argparse.Namespace``：CLI 与服务层共用本函数，
    而 Namespace 的鸭子类型让「少传一个字段」只能等到运行时才炸 —— 服务层就曾因为
    少传 ``config_dir`` 而在 POST /games 时抛 AttributeError（500）。
    """
    seats = list(range(1, total_seats + 1))
    if random:
        return RandomPolicy(seed=seed), {str(s): "random" for s in seats}
    if model is None:
        client = MockClient(auto_responder())
        specs = {seat: SeatSpec(provider="mock", model=MOCK_MODEL) for seat in seats}
        return LLMPolicy({"mock": client}, specs, game), {str(s): MOCK_MODEL for s in seats}

    models: ModelsConfig = load_models(config_dir)  # 缺 key 会在这里退出
    assignment = {seat: model for seat in seats}
    specs = specs_from_models(models, assignment)
    clients = {}
    for name, provider in models.providers.items():
        if any(spec.provider == name for spec in specs.values()):
            clients[name] = OpenAICompatClient(
                provider.base_url,
                resolve_api_key(provider),
                timeout_s=models.defaults.timeout_s,
                network_retries=models.defaults.network_retries,
            )
    policy = LLMPolicy(clients, specs, game, models.defaults)
    return policy, {str(seat): model for seat in seats}


def play_one_game(
    board: BoardConfig,
    game: GameConfig,
    seed: int | None,
    view_seat: int | None,
    renderer: TerminalRenderer,
    policy: object,
    models_map: dict[str, str],
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

    runner = GameRunner(engine, policy, on_events=on_events)  # type: ignore[arg-type]
    state, collected = runner.run(state)
    events = opening + collected

    if log is not None:
        log.archive(events)
        log.write_meta(build_meta(state, started_at=started_at, models=models_map))
        # 每个席位的完整消息历史，事后可逐条复盘（§8.4）
        if game.persist.dump_agent_contexts:
            for seat, context in sorted(getattr(policy, "contexts", {}).items()):
                log.write_context(seat, context.snapshot())
        update_index(
            game.persist.root,
            {
                "game_id": state.game_id,
                "started_at": started_at.isoformat(),
                "seed": seed,
                "models": models_map,
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
    renderer = TerminalRenderer(
        color=not args.no_color, show_thinking=not args.no_cot, cot_chars=args.cot_chars
    )

    if show_detail and view_seat is not None:
        renderer.write(f"（以 {view_seat} 号视角观战：看不到底牌、狼人频道与别人的私有信息）")

    if args.model is not None and not args.summary:
        try:
            models_cfg = load_models(args.config_dir)  # 缺文件 / 缺 key 在这里就有中文提示
        except ConfigError as exc:
            print(exc, file=sys.stderr)
            return 1
        renderer.write("API key 来源：")
        for line in api_key_report(models_cfg):
            renderer.write(line)

    started = time.perf_counter()
    results = []
    for i in range(args.games):
        # 每局都要新的上下文：角色重新分配，agent 的历史不能串局
        try:
            policy, models_map = build_policy(
                game,
                total_seats,
                model=args.model,
                random=args.random,
                seed=args.seed,
                config_dir=args.config_dir,
            )
        except ConfigError as exc:
            # 模型名写错 / models.yaml 缺字段：给中文提示，别甩 traceback
            print(exc, file=sys.stderr)
            return 1
        results.append(
            play_one_game(
                board,
                game,
                seed=None if args.seed is None else args.seed + i,
                view_seat=view_seat,
                renderer=renderer,
                policy=policy,
                models_map=models_map,
                show_detail=show_detail,
                delay=args.delay,
                save=not args.no_save,
            )
        )
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
