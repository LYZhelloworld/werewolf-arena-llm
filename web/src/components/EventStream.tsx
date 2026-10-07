import { useState } from "react";
import type { Event } from "../api/types";
import { ALERT_TYPES, VIS_INFO, eventText, phaseLabel } from "../lib/derive";

function CoT({ thinking }: { thinking: NonNullable<Event["thinking"]> }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="mt-1">
      <button
        onClick={() => setOpen((o) => !o)}
        className="text-[11px] text-fuchsia-300 underline"
      >
        {open ? "▾" : "▸"} 思考过程（仅你可见）
      </button>
      {open && (
        <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap rounded border border-fuchsia-800 bg-fuchsia-950/40 p-2 text-[11px] text-fuchsia-200">
          {thinking.text ?? "（该模型未输出思考）"}
          {"\n— "}
          {thinking.source}
          {thinking.replayed ? " · 已回传" : ""}
        </pre>
      )}
    </div>
  );
}

function EventItem({ e, godView }: { e: Event; godView: boolean }) {
  const vis = VIS_INFO[e.visibility] ?? VIS_INFO.public;
  const alert = ALERT_TYPES[e.type];
  const rowCls = alert ? alert.row : `${vis.border} bg-slate-800/60`;
  return (
    <div className={`rounded border-l-4 ${rowCls} p-2`}>
      <div className="flex flex-wrap items-center gap-2 text-[11px] text-slate-400">
        <span>#{e.seq}</span>
        {e.actor != null && <span className="text-slate-300">{e.actor} 号</span>}
        <span className={`rounded px-1 py-0.5 text-white ${vis.badge}`}>{vis.label}</span>
        {alert && (
          <span className={`rounded px-1 py-0.5 text-white ${alert.badge}`}>{alert.label}</span>
        )}
        <span className="text-slate-500">{e.type}</span>
      </div>
      <div className="mt-1 text-sm text-slate-100">{eventText(e)}</div>
      {godView && e.thinking && <CoT thinking={e.thinking} />}
      {e.llm && (
        <div className="mt-1 text-[10px] text-slate-500">
          {e.llm.model} · {e.llm.latency_ms}ms · tok{" "}
          {(e.llm.prompt_tokens ?? 0) + (e.llm.completion_tokens ?? 0)}
          {e.llm.fallback && <span className="text-amber-400"> · 兜底</span>}
          {e.llm.network_retries > 0 && (
            <span className="text-orange-400"> · 重试{e.llm.network_retries}</span>
          )}
          {e.llm.tool_turns > 0 && <span> · 工具{e.llm.tool_turns}轮</span>}
        </div>
      )}
    </div>
  );
}

export default function EventStream({
  events,
  godView,
}: {
  events: Event[];
  godView: boolean;
}) {
  let lastKey = "";
  return (
    <div className="space-y-2">
      {events.map((e) => {
        const key = `${e.day}:${e.phase}`;
        const showDivider = key !== lastKey;
        lastKey = key;
        return (
          <div key={e.seq}>
            {showDivider && (
              <div className="sticky top-0 z-10 bg-slate-900/90 py-1 text-xs font-semibold text-slate-300 backdrop-blur">
                {phaseLabel(e.phase, e.day)}
              </div>
            )}
            <EventItem e={e} godView={godView} />
          </div>
        );
      })}
      {events.length === 0 && (
        <div className="text-sm text-slate-500">等待事件流…</div>
      )}
    </div>
  );
}
