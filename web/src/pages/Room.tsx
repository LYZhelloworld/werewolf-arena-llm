import { useEffect, useRef, useState } from "react";
import { getMeta } from "../api/client";
import { useGameStore } from "../store/gameStore";
import { useGameConnection } from "../hooks/useGameConnection";
import { deriveSeats, deriveStats } from "../lib/derive";
import SeatGrid from "../components/SeatGrid";
import EventStream from "../components/EventStream";
import SidePanel from "../components/SidePanel";
import NoticeBanner from "../components/NoticeBanner";

const STATUS_TEXT: Record<string, string> = {
  idle: "空闲",
  connecting: "连接中",
  live: "直播中",
  reconnecting: "重连中",
  done: "已结束",
  error: "错误",
};

export default function Room({
  gameId,
  onExit,
}: {
  gameId: string;
  onExit: () => void;
}) {
  const [seat, setSeat] = useState<number | null>(null);
  useGameConnection(gameId, seat);

  const events = useGameStore((s) => s.events);
  const meta = useGameStore((s) => s.meta);
  const status = useGameStore((s) => s.status);
  const error = useGameStore((s) => s.error);
  const setMeta = useGameStore((s) => s.setMeta);

  useEffect(() => {
    let cancelled = false;
    getMeta(gameId)
      .then((m) => {
        if (!cancelled) setMeta(m);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [gameId, status, setMeta]);

  const godView = seat === null;
  const seats = deriveSeats(events, meta);
  const stats = deriveStats(events);

  // 中间栏自动滚动：仅当用户已经贴在底部附近时，新消息才把视口拉到最下；
  // 用户上滑看历史时（nearBottom=false）不打扰，新消息来了也不会把人拽下去。
  const mainRef = useRef<HTMLDivElement>(null);
  const [autoScroll, setAutoScroll] = useState(true);

  useEffect(() => {
    const el = mainRef.current;
    if (el && autoScroll) {
      el.scrollTop = el.scrollHeight;
    }
  }, [events, autoScroll]);

  const handleScroll = () => {
    const el = mainRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    setAutoScroll(nearBottom);
  };

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center gap-3 border-b border-slate-700 px-4 py-2">
        <button
          onClick={onExit}
          className="rounded bg-slate-700 px-2 py-1 text-xs hover:bg-slate-600"
        >
          ← 大厅
        </button>
        <span className="font-mono text-xs text-slate-400">{gameId}</span>
        <span
          className={`rounded px-2 py-0.5 text-xs ${
            status === "done"
              ? "bg-emerald-700"
              : status === "error"
                ? "bg-rose-700"
                : status === "reconnecting"
                  ? "bg-amber-700"
                  : "bg-sky-700"
          }`}
        >
          {STATUS_TEXT[status] ?? status}
        </span>
      </header>

      <NoticeBanner status={status} error={error} />

      <div className="flex min-h-0 flex-1">
        <aside className="w-72 shrink-0 overflow-y-auto border-r border-slate-700 p-3">
          <SeatGrid seats={seats} godView={godView} mySeat={seat} />
        </aside>
        <main
          ref={mainRef}
          onScroll={handleScroll}
          className="min-w-0 flex-1 overflow-y-auto p-3"
        >
          <EventStream events={events} godView={godView} />
        </main>
        <aside className="w-72 shrink-0 overflow-y-auto border-l border-slate-700 p-3">
          <SidePanel
            seat={seat}
            onSeat={setSeat}
            seats={seats}
            meta={meta}
            status={status}
            stats={stats}
          />
        </aside>
      </div>
    </div>
  );
}
