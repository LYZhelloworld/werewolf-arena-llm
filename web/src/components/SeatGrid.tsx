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
  // 当前视角的身份（非上帝视角时才有意义）：狼人视角下要能看到队友的身份。
  const myRole = mySeat != null ? (seats.find((s) => s.seat === mySeat)?.role ?? null) : null;
  const isWerewolfView = !godView && myRole === "werewolf";

  // 每个座位这一格该显示什么身份文本：
  // - 上帝视角：全部可见；
  // - 本人座位：显示自己的身份；
  // - 狼人视角的队友：显示「狼人」（狼人彼此身份公开，这是规则内的信息）；
  // - 其余：一律「？」，不泄露。
  const roleTextOf = (s: SeatInfo): string => {
    if (godView) return roleLabel(s.role);
    if (mySeat === s.seat) return roleLabel(s.role);
    if (isWerewolfView && s.role === "werewolf") return roleLabel(s.role);
    return "？";
  };

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
            <div className="text-xs text-slate-300">{roleTextOf(s)}</div>
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
