import { describe, expect, it } from "vitest";

import {
  isBuiltInDisabledHost,
  normalizeFolderPath,
} from "./settings";

describe("extension settings helpers", () => {
  describe("normalizeFolderPath", () => {
    it("normalizes ordinary webclipper folders", () => {
      expect(normalizeFolderPath("webclipper/research")).toBe("/webclipper/research/");
      expect(normalizeFolderPath("/webclipper//research/")).toBe("/webclipper/research/");
      expect(normalizeFolderPath("")).toBe("/webclipper/");
    });

    it("falls back on traversal or Windows-style paths", () => {
      expect(normalizeFolderPath("/webclipper/../wiki/")).toBe("/webclipper/");
      expect(normalizeFolderPath("/webclipper\\research")).toBe("/webclipper/");
    });
  });

  describe("isBuiltInDisabledHost", () => {
    it("disables the product app domain and subdomains", () => {
      expect(isBuiltInDisabledHost("llmwiki.app")).toBe(true);
      expect(isBuiltInDisabledHost("www.llmwiki.app")).toBe(true);
      expect(isBuiltInDisabledHost("localhost")).toBe(false);
      expect(isBuiltInDisabledHost("example.com")).toBe(false);
    });
  });
});
