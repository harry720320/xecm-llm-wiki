import { describe, expect, it } from "vitest";

import {
  applySave,
  EMPTY_STREAK,
  todayKey,
  yesterdayOf,
  type StreakState,
} from "./streak";

describe("streak helpers", () => {
  describe("todayKey", () => {
    it("returns YYYY-MM-DD in local time", () => {
      expect(todayKey(new Date(2026, 0, 3))).toBe("2026-01-03");
      expect(todayKey(new Date(2026, 11, 31))).toBe("2026-12-31");
    });
  });

  describe("yesterdayOf", () => {
    it("rolls back one day, handling month and year boundaries", () => {
      expect(yesterdayOf("2026-03-15")).toBe("2026-03-14");
      expect(yesterdayOf("2026-03-01")).toBe("2026-02-28");
      expect(yesterdayOf("2026-01-01")).toBe("2025-12-31");
    });
  });

  describe("applySave", () => {
    it("starts a streak at 1 on the first save ever", () => {
      const next = applySave(EMPTY_STREAK, "2026-05-31");
      expect(next).toEqual({
        totalSources: 1,
        lastSaveDate: "2026-05-31",
        streakDays: 1,
      });
    });

    it("keeps streak unchanged on a second save the same day", () => {
      const prev: StreakState = {
        totalSources: 5,
        lastSaveDate: "2026-05-31",
        streakDays: 3,
      };
      const next = applySave(prev, "2026-05-31");
      expect(next).toEqual({
        totalSources: 6,
        lastSaveDate: "2026-05-31",
        streakDays: 3,
      });
    });

    it("increments streak when saving the next calendar day", () => {
      const prev: StreakState = {
        totalSources: 5,
        lastSaveDate: "2026-05-30",
        streakDays: 3,
      };
      const next = applySave(prev, "2026-05-31");
      expect(next).toEqual({
        totalSources: 6,
        lastSaveDate: "2026-05-31",
        streakDays: 4,
      });
    });

    it("resets to 1 after a gap of more than one day", () => {
      const prev: StreakState = {
        totalSources: 5,
        lastSaveDate: "2026-05-20",
        streakDays: 7,
      };
      const next = applySave(prev, "2026-05-31");
      expect(next).toEqual({
        totalSources: 6,
        lastSaveDate: "2026-05-31",
        streakDays: 1,
      });
    });

    it("recovers from a stored streak of 0 on the same day", () => {
      const prev: StreakState = {
        totalSources: 1,
        lastSaveDate: "2026-05-31",
        streakDays: 0,
      };
      const next = applySave(prev, "2026-05-31");
      expect(next.streakDays).toBe(1);
    });
  });
});
