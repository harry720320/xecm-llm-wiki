import { AuthClient, type GoTrueClient } from "@supabase/auth-js";
import { SUPABASE_URL, SUPABASE_ANON_KEY } from "./constants";

/**
 * Custom storage adapter for Manifest V3 — service workers have no localStorage.
 * Uses chrome.storage.local which persists across browser restarts.
 */
const chromeStorageAdapter = {
  getItem: async (key: string): Promise<string | null> => {
    const result = await chrome.storage.local.get(key);
    return result[key] ?? null;
  },
  setItem: async (key: string, value: string): Promise<void> => {
    await chrome.storage.local.set({ [key]: value });
  },
  removeItem: async (key: string): Promise<void> => {
    await chrome.storage.local.remove(key);
  },
};

interface SupabaseAuthClient {
  auth: GoTrueClient;
}

let _client: SupabaseAuthClient | null = null;

export function getSupabase(): SupabaseAuthClient {
  if (!_client) {
    _client = {
      auth: new AuthClient({
        url: `${SUPABASE_URL}/auth/v1`,
        headers: {
          apikey: SUPABASE_ANON_KEY,
          Authorization: `Bearer ${SUPABASE_ANON_KEY}`,
        },
        storage: chromeStorageAdapter,
        flowType: "pkce",
        autoRefreshToken: true,
        persistSession: true,
        detectSessionInUrl: false,
      }),
    };
  }
  return _client;
}
