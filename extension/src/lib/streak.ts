export interface StreakState {
  totalSources: number;
  lastSaveDate: string | null;
  streakDays: number;
}

export const EMPTY_STREAK: StreakState = {
  totalSources: 0,
  lastSaveDate: null,
  streakDays: 0,
};

const STATS_KEY = "llmwiki_save_stats";

export function todayKey(now: Date = new Date()): string {
  const y = now.getFullYear();
  const m = String(now.getMonth() + 1).padStart(2, "0");
  const d = String(now.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

export function yesterdayOf(key: string): string {
  const [y, m, d] = key.split("-").map(Number);
  const date = new Date(y, m - 1, d);
  date.setDate(date.getDate() - 1);
  return todayKey(date);
}

export function applySave(prev: StreakState, today: string): StreakState {
  const totalSources = prev.totalSources + 1;
  if (prev.lastSaveDate === today) {
    return {
      totalSources,
      lastSaveDate: today,
      streakDays: prev.streakDays || 1,
    };
  }
  if (prev.lastSaveDate && prev.lastSaveDate === yesterdayOf(today)) {
    return {
      totalSources,
      lastSaveDate: today,
      streakDays: prev.streakDays + 1,
    };
  }
  return { totalSources, lastSaveDate: today, streakDays: 1 };
}

function coerceStreakState(value: unknown): StreakState {
  if (!value || typeof value !== "object") return { ...EMPTY_STREAK };
  const v = value as Partial<StreakState>;
  return {
    totalSources: typeof v.totalSources === "number" ? v.totalSources : 0,
    lastSaveDate: typeof v.lastSaveDate === "string" ? v.lastSaveDate : null,
    streakDays: typeof v.streakDays === "number" ? v.streakDays : 0,
  };
}

export async function getStats(): Promise<StreakState> {
  const result = await chrome.storage.local.get(STATS_KEY);
  return coerceStreakState(result[STATS_KEY]);
}

export async function recordSave(): Promise<StreakState> {
  const prev = await getStats();
  const next = applySave(prev, todayKey());
  await chrome.storage.local.set({ [STATS_KEY]: next });
  return next;
}
