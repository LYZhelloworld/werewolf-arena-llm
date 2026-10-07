import type { Event, GameMeta } from "../api/types";

export const ROLE_LABELS: Record<string, string> = {
  werewolf: "狼人",
  villager: "平民",
  seer: "预言家",
  witch: "女巫",
  hunter: "猎人",
};

export function roleLabel(role: string | null): string {
  if (!role) return "未知";
  return ROLE_LABELS[role] ?? role;
}

// 阶段英文值 -> 中文标签（覆盖主流阶段；未知阶段退化成原值）。
export const PHASE_LABELS: Record<string, string> = {
  setup: "准备",
  night_wolf: "夜间 · 狼人协商",
  night_seer: "夜间 · 预言家查验",
  night_witch: "夜间 · 女巫用药",
  night_hunter_shoot: "夜间 · 猎人开枪",
  night_resolve: "夜间 · 结算",
  day_announce: "白天 · 死亡公告",
  day_discuss: "白天 · 自由讨论",
  day_vote: "白天 · 投票",
  day_resolve: "白天 · 计票",
  day_last_words: "白天 · 遗言",
  postgame: "赛后 · 感想",
  finished: "结束",
};

export function phaseLabel(phase: string, day: number): string {
  const base = PHASE_LABELS[phase] ?? phase;
  return `第 ${day} 天 · ${base}`;
}

export const VIS_INFO: Record<string, { label: string; badge: string; border: string }> = {
  public: { label: "公开", badge: "bg-slate-600", border: "border-l-slate-500" },
  "private:wolf": {
    label: "仅狼人可见",
    badge: "bg-red-700",
    border: "border-l-red-600",
  },
  "private:seer": {
    label: "仅预言家可见",
    badge: "bg-sky-700",
    border: "border-l-sky-600",
  },
  "private:witch": {
    label: "仅女巫可见",
    badge: "bg-purple-700",
    border: "border-l-purple-600",
  },
  "private:self": {
    label: "仅本人可见",
    badge: "bg-amber-700",
    border: "border-l-amber-600",
  },
  spectator: {
    label: "观众可见",
    badge: "bg-teal-700",
    border: "border-l-teal-600",
  },
};

export interface SeatInfo {
  seat: number;
  alive: boolean;
  role: string | null;
  model: string | null;
  failed: boolean; // 该席位出现过兜底 / 非法动作
}

export function deriveSeats(events: Event[], meta: GameMeta | null): SeatInfo[] {
  const roles: Record<string, string> = {};
  const dead = new Set<number>();
  const failed = new Set<number>();

  for (const e of events) {
    if (e.type === "role_assigned" && e.payload.roles) {
      Object.assign(roles, e.payload.roles as Record<string, string>);
    }
    if (e.type === "death" && typeof e.payload.seat === "number") {
      dead.add(e.payload.seat);
    }
    if (e.actor != null && (e.type === "action_fallback" || e.llm?.fallback)) {
      failed.add(e.actor);
    }
  }
  if (meta?.final_roles) Object.assign(roles, meta.final_roles);

  const count = Math.max(9, Object.keys(roles).length);
  const seats: SeatInfo[] = [];
  for (let s = 1; s <= count; s++) {
    seats.push({
      seat: s,
      alive: !dead.has(s),
      role: roles[String(s)] ?? null,
      model: meta?.models?.[String(s)] ?? null,
      failed: failed.has(s),
    });
  }
  return seats;
}

export interface Stats {
  calls: number;
  tokens: number;
  fallback: number;
  rejected: number; // 非法动作次数
  downgraded: number; // CoT 回传降级次数
  retries: number; // 网络重试次数
}

export function deriveStats(events: Event[]): Stats {
  let calls = 0;
  let tokens = 0;
  let fallback = 0;
  let rejected = 0;
  let downgraded = 0;
  let retries = 0;
  for (const e of events) {
    if (e.llm) {
      calls += 1;
      tokens += (e.llm.prompt_tokens ?? 0) + (e.llm.completion_tokens ?? 0);
      if (e.llm.fallback) fallback += 1;
      retries += e.llm.network_retries ?? 0;
    }
    if (e.type === "action_rejected") rejected += 1;
    if (e.type === "llm_downgrade") downgraded += 1;
  }
  return { calls, tokens, fallback, rejected, downgraded, retries };
}

// 需要显眼提示的异常类事件（M8 异常提示）。
export const ALERT_TYPES: Record<string, { label: string; row: string; badge: string }> = {
  action_fallback: {
    label: "兜底",
    row: "border-l-amber-500 bg-amber-950/40",
    badge: "bg-amber-600",
  },
  action_rejected: {
    label: "非法动作",
    row: "border-l-orange-500 bg-orange-950/30",
    badge: "bg-orange-600",
  },
  llm_downgrade: {
    label: "CoT 降级",
    row: "border-l-rose-500 bg-rose-950/30",
    badge: "bg-rose-600",
  },
  game_aborted: {
    label: "中止",
    row: "border-l-rose-600 bg-rose-950/40",
    badge: "bg-rose-700",
  },
};

const WINNER_LABELS: Record<string, string> = {
  wolf: "狼人胜",
  good: "好人胜",
};

export function winnerLabel(winner: string | null): string {
  if (!winner) return "未分胜负";
  return WINNER_LABELS[winner] ?? winner;
}
