import type { ConnStatus } from "../store/gameStore";
import type { GameMeta } from "../api/types";
import type { SeatInfo, Stats } from "../lib/derive";
import { roleLabel, winnerLabel } from "../lib/derive";

const STATUS_LABEL: Record<ConnStatus, string> = {
  idle: "空闲",
  connecting: "连接中",
  live: "直播中",
  reconnecting: "重连中",
  done: "已结束",
  error: "错误",
};

export default function SidePanel({
  seat,
  onSeat,
  seats,
  meta,
  status,
  stats,
}: {
  seat: number | null;
  onSeat: (seat: number | null) => void;
  seats: SeatInfo[];
  meta: GameMeta | null;
  status: ConnStatus;
  stats: Stats;
}) {
  const godView = seat === null;
  return (
    <div className="space-y-5">
      <section>
        <h3 className="text-xs uppercase text-slate-400">视角</h3>
        <div className="mt-1 flex flex-wrap gap-1">
          <button
            onClick={() => onSeat(null)}
            className={`rounded px-2 py-1 text-xs ${
              seat === null ? "bg-indigo-600 text-white" : "bg-slate-700 text-slate-300 hover:bg-slate-600"
            }`}
          >
            上帝视角
          </button>
          {seats.map((s) => (
            <button
              key={s.seat}
              onClick={() => onSeat(s.seat)}
              className={`rounded px-2 py-1 text-xs ${
                seat === s.seat
                  ? "bg-indigo-600 text-white"
                  : "bg-slate-700 text-slate-300 hover:bg-slate-600"
              }`}
            >
              {s.seat}号
            </button>
          ))}
        </div>
        <p className="mt-1 text-[10px] text-slate-500">
          上帝视角可见全部底牌与思考；切到某玩家则按该玩家权限过滤（CoT 与私有频道隐藏）。
        </p>
      </section>

      <section>
        <h3 className="text-xs uppercase text-slate-400">状态</h3>
        <div className="mt-1 text-sm text-slate-200">{STATUS_LABEL[status]}</div>
        {meta && (
          <div className="mt-1 text-[11px] text-slate-400">
            天数 {meta.days ?? "-"} · 席位 {Object.keys(meta.models).length}
          </div>
        )}
        {meta?.winner && (
          <div className="mt-1 text-sm text-emerald-300">
            胜方：{winnerLabel(meta.winner)}
          </div>
        )}
        {godView &&
          meta?.final_roles &&
          Object.keys(meta.final_roles).length > 0 && (
            <div className="mt-2 space-y-0.5 text-[11px] text-slate-400">
              {Object.entries(meta.final_roles)
                .map(([s, r]) => `${s}号 ${roleLabel(r)}`)
                .join(" · ")}
            </div>
          )}
      </section>

      <section>
        <h3 className="text-xs uppercase text-slate-400">统计</h3>
        <div className="mt-1 grid grid-cols-3 gap-2 text-center">
          <div>
            <div className="text-lg text-slate-100">{stats.calls}</div>
            <div className="text-[10px] text-slate-500">调用</div>
          </div>
          <div>
            <div className="text-lg text-slate-100">{stats.tokens}</div>
            <div className="text-[10px] text-slate-500">token</div>
          </div>
          <div>
            <div className="text-lg text-amber-400">{stats.fallback}</div>
            <div className="text-[10px] text-slate-500">兜底</div>
          </div>
          <div>
            <div className="text-lg text-orange-400">{stats.rejected}</div>
            <div className="text-[10px] text-slate-500">非法动作</div>
          </div>
          <div>
            <div className="text-lg text-rose-400">{stats.downgraded}</div>
            <div className="text-[10px] text-slate-500">CoT 降级</div>
          </div>
          <div>
            <div className="text-lg text-sky-400">{stats.retries}</div>
            <div className="text-[10px] text-slate-500">网络重试</div>
          </div>
        </div>
      </section>
    </div>
  );
}
