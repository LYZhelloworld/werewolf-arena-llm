import { useEffect, useState } from "react";
import { API_BASE, createGame, listGames } from "../api/client";
import type { GameListItem } from "../api/types";
import { winnerLabel } from "../lib/derive";

type Mode = "mock" | "random" | "model";

export default function Lobby({ onEnter }: { onEnter: (gameId: string) => void }) {
  const [mode, setMode] = useState<Mode>("mock");
  const [model, setModel] = useState("");
  const [seed, setSeed] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [games, setGames] = useState<GameListItem[]>([]);

  const refresh = () => {
    listGames()
      .then(setGames)
      .catch(() => setGames([]));
  };

  useEffect(() => {
    refresh();
  }, []);

  const start = async () => {
    setBusy(true);
    setError(null);
    try {
      const req =
        mode === "random"
          ? { random: true }
          : mode === "model"
            ? { model: model.trim() || null }
            : { model: null as string | null, random: false };
      const seedNum = seed.trim() === "" ? null : Number(seed);
      const resp = await createGame({ ...req, seed: seedNum });
      onEnter(resp.game_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mx-auto max-w-3xl p-6">
      <h1 className="text-2xl font-bold text-slate-100">九人狼人杀 · 观战台</h1>
      <p className="mt-1 text-sm text-slate-400">
        9 个大语言模型同台对局，人类实时观战。后端：<span className="font-mono">{API_BASE}</span>
      </p>

      <section className="mt-6 rounded-xl border border-slate-700 bg-slate-800/40 p-4">
        <h2 className="text-sm font-semibold text-slate-200">新建对局</h2>

        <div className="mt-3 flex flex-wrap gap-2">
          <ModeButton active={mode === "mock"} onClick={() => setMode("mock")}>
            默认（Mock 客户端，无需 key）
          </ModeButton>
          <ModeButton active={mode === "random"} onClick={() => setMode("random")}>
            随机策略（规则随机，无 LLM）
          </ModeButton>
          <ModeButton active={mode === "model"} onClick={() => setMode("model")}>
            指定模型（需配置 API key）
          </ModeButton>
        </div>

        {mode === "model" && (
          <input
            className="mt-3 w-full rounded bg-slate-900 px-3 py-2 text-sm text-slate-100 outline-none ring-1 ring-slate-700 focus:ring-indigo-500"
            placeholder="模型名，如 gpt-4o / deepseek-chat"
            value={model}
            onChange={(e) => setModel(e.target.value)}
          />
        )}

        <div className="mt-3 flex items-center gap-3">
          <label className="text-xs text-slate-400">随机种子（可选）</label>
          <input
            className="w-32 rounded bg-slate-900 px-3 py-1.5 text-sm text-slate-100 outline-none ring-1 ring-slate-700 focus:ring-indigo-500"
            placeholder="留空随机"
            value={seed}
            inputMode="numeric"
            onChange={(e) => setSeed(e.target.value)}
          />
          <button
            onClick={start}
            disabled={busy}
            className="ml-auto rounded bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
          >
            {busy ? "启动中…" : "开始观战"}
          </button>
        </div>

        {error && <p className="mt-3 text-xs text-rose-400">{error}</p>}
      </section>

      <section className="mt-6">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold text-slate-200">历史对局</h2>
          <button onClick={refresh} className="text-xs text-slate-400 hover:text-slate-200">
            刷新
          </button>
        </div>
        <ul className="mt-2 space-y-2">
          {games.length === 0 && (
            <li className="text-xs text-slate-500">暂无对局（需后端已启动）。</li>
          )}
          {games.map((g) => (
            <li
              key={g.game_id}
              onClick={() => onEnter(g.game_id)}
              className="flex cursor-pointer items-center gap-3 rounded-lg border border-slate-700 bg-slate-800/40 px-3 py-2 hover:border-indigo-500"
            >
              <span className="font-mono text-xs text-slate-400">{g.game_id}</span>
              {g.running ? (
                <span className="rounded bg-sky-700 px-1.5 py-0.5 text-[10px] text-white">进行中</span>
              ) : (
                <span className="rounded bg-slate-700 px-1.5 py-0.5 text-[10px] text-slate-200">
                  {winnerLabel(g.winner)}
                </span>
              )}
              <span className="ml-auto text-[11px] text-slate-500">
                {g.days ?? "-"} 天 · {g.event_count} 事件
              </span>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}

function ModeButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={`rounded-full px-3 py-1.5 text-xs ${
        active ? "bg-indigo-600 text-white" : "bg-slate-700 text-slate-300 hover:bg-slate-600"
      }`}
    >
      {children}
    </button>
  );
}
