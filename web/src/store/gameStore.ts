import { create } from "zustand";
import type { Event, GameMeta } from "../api/types";

export type ConnStatus = "idle" | "connecting" | "live" | "done" | "error";

interface GameStore {
  gameId: string | null;
  seat: number | null; // null = 上帝视角
  events: Event[];
  meta: GameMeta | null;
  status: ConnStatus;
  error: string | null;

  setGame: (gameId: string) => void;
  setSeat: (seat: number | null) => void;
  resetEvents: () => void;
  appendEvents: (events: Event[]) => void;
  setMeta: (meta: GameMeta) => void;
  setStatus: (s: ConnStatus) => void;
  setError: (msg: string | null) => void;
}

export const useGameStore = create<GameStore>((set) => ({
  gameId: null,
  seat: null,
  events: [],
  meta: null,
  status: "idle",
  error: null,

  setGame: (gameId) =>
    set({ gameId, events: [], meta: null, status: "idle", error: null }),
  setSeat: (seat) => set({ seat }),
  resetEvents: () => set({ events: [], error: null }),

  appendEvents: (incoming) =>
    set((state) => {
      if (incoming.length === 0) return state;
      const seen = new Set(state.events.map((e) => e.seq));
      const fresh = incoming.filter((e) => !seen.has(e.seq));
      if (fresh.length === 0) return state;
      const merged = [...state.events, ...fresh].sort((a, b) => a.seq - b.seq);
      return { events: merged };
    }),

  setMeta: (meta) => set({ meta }),
  setStatus: (status) => set({ status }),
  setError: (error) => set({ error }),
}));
