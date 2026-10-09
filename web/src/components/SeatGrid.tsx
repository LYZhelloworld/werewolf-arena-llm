import type { SeatInfo } from "../lib/derive";
import { roleLabel } from "../lib/derive";

export default function SeatGrid({
  seats,
  godView,
  mySeat,
}: {
  seats: SeatInfo[];
  godView: boolean;
  mySeat: number | null;
}) {
  return (
    <div>
      <h3 className="mb-2 text-xs uppercase text-slate-400">座位（{seats.length}）</h3>
      <div className="grid grid-cols-3 gap-2">
        {seats.map((s) => (
          <div
            key={s.seat}
            className={`rounded-lg border p-2 ${
              s.alive
                ? "border-slate-600 bg-slate-800"
                : "border-slate-800 bg-slate-900 opacity-60"
            }`}
          >
            <div className="flex items-center justify-between">
              <span className="text-sm font-semibold text-slate-200">
                {s.seat} 号
                {mySeat === s.seat && (
                  <span className="ml-1 text-[10px] text-indigo-300">（你）</span>
                )}
              </span>
              <span className={`text-[10px] ${s.alive ? "text-emerald-400" : "text-rose-400"}`}>
                {s.alive ? "存活" : "出局"}
              </span>
            </div>
            <div className="text-xs text-slate-300">
              {godView ? roleLabel(s.role) : s.role && mySeat === s.seat ? roleLabel(s.role) : "？"}
            </div>
            {s.model && (
              <div className="truncate text-[10px] text-slate-500" title={s.model}>
                {s.model}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
