import React, { useEffect, useState } from "react";
import {
  getDocumentByUrl,
  saveWebPage,
  savePdf,
  type DocumentByUrl,
  type Highlight,
} from "@/lib/api";
import {
  getSelectedFolderPath,
  getSelectedKnowledgeBaseId,
  normalizeFolderPath,
  setSelectedFolderPath,
  setSelectedKnowledgeBaseId,
} from "@/lib/settings";
import KBPicker from "./KBPicker";
import StatusFeedback, { type Status } from "./StatusFeedback";
import { recordSave } from "@/lib/streak";

const TRACKING_PARAMS = new Set([
  "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
  "utm_id", "utm_name", "utm_brand", "utm_social",
  "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src",
  "_branch_match_id", "igshid",
]);

function canonicalize(href: string): string {
  try {
    const u = new URL(href);
    u.hash = "";
    const keep = new URLSearchParams();
    u.searchParams.forEach((v, k) => {
      if (!TRACKING_PARAMS.has(k.toLowerCase())) keep.append(k, v);
    });
    u.search = keep.toString() ? `?${keep.toString()}` : "";
    if (u.pathname.length > 1 && u.pathname.endsWith("/")) {
      u.pathname = u.pathname.replace(/\/+$/, "");
    }
    return u.toString();
  } catch {
    return href;
  }
}

function safeHost(href: string): string {
  try {
    return new URL(href).hostname.replace(/^www\./, "");
  } catch {
    return href;
  }
}

function slugifyFilename(value: string): string {
  return value
    .toLowerCase()
    .replace(/[^\w\s.-]/g, "")
    .replace(/\s+/g, "-")
    .replace(/-+/g, "-")
    .slice(0, 80)
    .replace(/^[-_.]+|[-_.]+$/g, "") || "web-clip";
}

interface Props {
  apiUrl: string;
  accessToken: string | null;
}

interface TabInfo {
  url: string;
  title: string;
  isPdf: boolean;
  tabId: number;
}

export default function SaveForm({ apiUrl, accessToken }: Props) {
  const [tab, setTab] = useState<TabInfo | null>(null);
  const [title, setTitle] = useState("");
  const [knowledgeBaseId, setKnowledgeBaseId] = useState<string | null>(null);
  const [folderPath, setFolderPath] = useState("/webclipper/");
  const [showMore, setShowMore] = useState(false);
  const [existingDoc, setExistingDoc] = useState<DocumentByUrl | null>(null);
  const [checkingExisting, setCheckingExisting] = useState(false);
  const [status, setStatus] = useState<Status>({ type: "idle" });

  useEffect(() => {
    detectCurrentPage();
    getSelectedKnowledgeBaseId()
      .then((id) => {
        if (id) setKnowledgeBaseId((current) => current ?? id);
      })
      .catch(() => {
        // Non-fatal: the picker will fall back to the first KB.
      });
    getSelectedFolderPath()
      .then((path) => setFolderPath(path))
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (!tab || tab.isPdf) {
      setExistingDoc(null);
      setCheckingExisting(false);
      return;
    }

    let cancelled = false;

    async function checkExistingDocument() {
      if (!tab) return;
      setCheckingExisting(true);
      setExistingDoc(null);
      try {
        const doc = await getDocumentByUrl(apiUrl, accessToken, canonicalize(tab.url));
        if (cancelled) return;
        if (doc) {
          setExistingDoc(doc);
          setKnowledgeBaseId(doc.knowledge_base_id);
          setFolderPath(doc.path || "/webclipper/");
          setSelectedKnowledgeBaseId(doc.knowledge_base_id).catch(() => {});
          if (doc.path) setSelectedFolderPath(doc.path).catch(() => {});
          chrome.tabs.sendMessage(tab.tabId, {
            type: "DOCUMENT_SAVED",
            documentId: doc.id,
          }).catch(() => {
            // Content script may not be present on restricted pages.
          });
        }
      } catch {
        // A miss is normal for new pages. Other failures should not block saving.
      } finally {
        if (!cancelled) setCheckingExisting(false);
      }
    }

    checkExistingDocument();

    return () => {
      cancelled = true;
    };
  }, [apiUrl, accessToken, tab]);

  async function detectCurrentPage() {
    const [activeTab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!activeTab?.url || !activeTab.id) return;

    const url = activeTab.url;
    const isPdf =
      url.toLowerCase().endsWith(".pdf") ||
      (activeTab.title?.toLowerCase().endsWith(".pdf") ?? false);

    setTab({ url, title: activeTab.title ?? "", isPdf, tabId: activeTab.id });
    setTitle(activeTab.title ?? "");
  }

  async function handleSave() {
    if (!tab || !knowledgeBaseId) return;

    try {
      if (tab.isPdf) {
        await handleSavePdf();
      } else {
        await handleSaveWeb();
      }
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : "Save failed";
      setStatus({ type: "error", message });
    }
  }

  async function handleSaveWeb() {
    if (!tab || !knowledgeBaseId) return;

    setStatus({ type: "saving", message: "Extracting page..." });

    let html: string;
    try {
      // Run in the page so the extension's own marks/UI are stripped from
      // the snapshot — we don't want yellow <mark> nodes or the popover
      // floating in the saved HTML.
      const [{ result }] = await chrome.scripting.executeScript({
        target: { tabId: tab.tabId },
        func: async () => {
          const MAX_IMAGES = 12;
          const MAX_IMAGE_BYTES = 2_500_000;
          const MAX_TOTAL_BYTES = 6_000_000;

          const clone = document.documentElement.cloneNode(true) as HTMLElement;
          clone.querySelectorAll(
            ".llmwiki-pill, .llmwiki-popover, #llmwiki-highlight-style",
          ).forEach((el) => el.remove());
          clone.querySelectorAll("mark.llmwiki-hl").forEach((mark) => {
            const parent = mark.parentNode;
            if (!parent) return;
            while (mark.firstChild) parent.insertBefore(mark.firstChild, mark);
            parent.removeChild(mark);
          });

          const liveImages = Array.from(document.images);
          const cloneImages = Array.from(clone.querySelectorAll("img"));
          const candidates = liveImages
            .map((img, index) => {
              const rect = img.getBoundingClientRect();
              const width = Math.round(rect.width || img.naturalWidth || 0);
              const height = Math.round(rect.height || img.naturalHeight || 0);
              const src = img.currentSrc || img.src || largestSrcsetUrl(img.srcset) || "";
              const inArticle = !!img.closest("article, main, [role='main']");
              return {
                index,
                src,
                width,
                height,
                score: (inArticle ? 10_000_000 : 0) + width * height,
              };
            })
            .filter((item) => {
              if (!item.src || item.src.startsWith("data:") || item.src.startsWith("blob:")) return false;
              if (!/^https?:\/\//i.test(item.src)) return false;
              return item.width >= 80 && item.height >= 50;
            })
            .sort((a, b) => b.score - a.score)
            .slice(0, MAX_IMAGES);

          let totalBytes = 0;
          for (const item of candidates) {
            if (totalBytes >= MAX_TOTAL_BYTES) break;
            const remaining = MAX_TOTAL_BYTES - totalBytes;
            const maxBytes = Math.min(MAX_IMAGE_BYTES, remaining);
            try {
              const response = await chrome.runtime.sendMessage({
                type: "FETCH_IMAGE_DATA_URL",
                url: item.src,
                maxBytes,
              });
              if (!response?.dataUrl || response?.error) continue;
              totalBytes += response.size ?? 0;
              const cloneImg = cloneImages[item.index];
              if (!cloneImg) continue;
              cloneImg.setAttribute("src", response.dataUrl);
              cloneImg.removeAttribute("srcset");
              cloneImg.removeAttribute("sizes");
              if (item.width) cloneImg.setAttribute("width", String(item.width));
              if (item.height) cloneImg.setAttribute("height", String(item.height));
              cloneImg.setAttribute("data-llmwiki-inlined-image", "true");
            } catch {
              // Leave the original URL in place so the API can still try server-side.
            }
          }

          return clone.outerHTML;

          function largestSrcsetUrl(srcset: string): string {
            let bestUrl = "";
            let bestWidth = 0;
            for (const raw of srcset.split(",")) {
              const parts = raw.trim().split(/\s+/);
              if (!parts[0]) continue;
              const width = parts[1]?.endsWith("w")
                ? Number.parseInt(parts[1], 10)
                : 0;
              if (!bestUrl || width > bestWidth) {
                bestUrl = parts[0];
                bestWidth = width;
              }
            }
            try {
              return bestUrl ? new URL(bestUrl, location.href).toString() : "";
            } catch {
              return bestUrl;
            }
          }
        },
      });
      html = result as string;
    } catch {
      throw new Error("Could not extract page content. Try refreshing the page.");
    }

    let highlights: Highlight[] = [];
    try {
      const reply = await chrome.tabs.sendMessage(tab.tabId, {
        type: "GET_PAGE_HIGHLIGHTS",
      });
      if (reply?.highlights && Array.isArray(reply.highlights)) {
        highlights = reply.highlights as Highlight[];
      }
    } catch {
      // Content script may not be present (e.g. PDF, restricted page). Ignore.
    }

    setStatus({ type: "saving", message: "Saving to LLM Wiki..." });

    const canonicalUrl = canonicalize(tab.url);
    const normalizedFolderPath = normalizeFolderPath(folderPath);

    const result = await saveWebPage(apiUrl, accessToken, knowledgeBaseId, {
      url: canonicalUrl,
      title: title || tab.title,
      path: normalizedFolderPath,
      html,
      highlights: highlights.length ? highlights : undefined,
    });

    // Tell the content script about the new doc id so subsequent highlight
    // edits in this same tab can persist via PATCH /highlights without a reload.
    try {
      await chrome.tabs.sendMessage(tab.tabId, {
        type: "DOCUMENT_SAVED",
        documentId: result.id,
      });
    } catch {
      // Page might be closed or content script unavailable — fine.
    }

    setExistingDoc({
      id: result.id,
      knowledge_base_id: knowledgeBaseId,
      title: title || tab.title,
      path: normalizedFolderPath,
      filename: "",
      version: 1,
      highlights,
    });
    setSelectedKnowledgeBaseId(knowledgeBaseId).catch(() => {});
    setSelectedFolderPath(normalizedFolderPath).catch(() => {});
    const stats = await recordSave().catch(() => null);
    setStatus({ type: "success", stats: stats ?? undefined });
  }

  async function handleSavePdf() {
    if (!tab || !knowledgeBaseId) return;

    setStatus({ type: "saving", message: "Downloading PDF..." });

    const downloadResult = await chrome.runtime.sendMessage({
      type: "DOWNLOAD_PDF",
      url: tab.url,
    });

    if ("error" in downloadResult) {
      throw new Error(downloadResult.error);
    }

    setStatus({ type: "saving", message: "Uploading to LLM Wiki..." });

    const pdfBytes = new Uint8Array(downloadResult.blob);
    const normalizedFolderPath = normalizeFolderPath(folderPath);
    await savePdf(apiUrl, accessToken, pdfBytes, downloadResult.filename, knowledgeBaseId, normalizedFolderPath);

    setSelectedKnowledgeBaseId(knowledgeBaseId).catch(() => {});
    setSelectedFolderPath(normalizedFolderPath).catch(() => {});
    const stats = await recordSave().catch(() => null);
    setStatus({ type: "success", stats: stats ?? undefined });
  }

  function handleKnowledgeBaseChange(id: string) {
    setKnowledgeBaseId(id);
    setSelectedKnowledgeBaseId(id).catch(() => {});
  }

  if (!tab) {
    return (
      <div className="flex items-center justify-center py-6">
        <div className="h-4 w-4 animate-spin rounded-full border-2 border-zinc-200 border-t-zinc-800" />
      </div>
    );
  }

  const isSaving = status.type === "saving";
  const isAlreadySaved = !!existingDoc;
  const canSave = knowledgeBaseId && !isSaving && !isAlreadySaved && status.type !== "success";

  return (
    <div className="space-y-3">
      {/* Title */}
      <div>
        <label className="mb-1.5 block text-xs font-medium text-zinc-700">Title</label>
        <input
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          className="h-9 w-full rounded-md border border-zinc-200 bg-white px-3 text-sm
                     text-zinc-950 shadow-sm outline-none transition-colors
                     placeholder:text-zinc-400 focus:border-zinc-400 focus:ring-2
                     focus:ring-zinc-950/10"
          placeholder="Page title"
        />
      </div>

      {/* KB picker */}
      <KBPicker
        apiUrl={apiUrl}
        accessToken={accessToken}
        value={knowledgeBaseId}
        onChange={handleKnowledgeBaseChange}
      />

      {/* Folder/More section disabled for v0 — re-enable when folder picker is ready.
      <div className="rounded-md border border-zinc-200 bg-zinc-50/60">
        <button
          type="button"
          onClick={() => setShowMore((v) => !v)}
          className="flex h-8 w-full items-center justify-between px-3 text-xs font-medium text-zinc-600 transition-colors hover:text-zinc-950"
        >
          <span>More</span>
          <span className="text-zinc-400">{showMore ? "-" : "+"}</span>
        </button>
        {showMore && (
          <div className="space-y-2 border-t border-zinc-200 px-3 py-3">
            <div>
              <label className="mb-1.5 block text-xs font-medium text-zinc-700">Folder</label>
              <input
                list="llmwiki-folder-suggestions"
                value={folderPath}
                onChange={(e) => setFolderPath(e.target.value)}
                onBlur={() => setFolderPath(normalizeFolderPath(folderPath))}
                className="h-8 w-full rounded-md border border-zinc-200 bg-white px-2.5 text-xs text-zinc-950 shadow-sm outline-none transition-colors placeholder:text-zinc-400 focus:border-zinc-400 focus:ring-2 focus:ring-zinc-950/10"
                placeholder="/webclipper/"
              />
              <datalist id="llmwiki-folder-suggestions">
                <option value="/webclipper/" />
                <option value="/articles/" />
                <option value="/research/" />
                <option value="/inbox/" />
              </datalist>
            </div>
            <div className="min-w-0 text-[11px] text-zinc-500">
              <span className="font-medium text-zinc-600">Filename</span>{" "}
              <span className="break-all">{normalizedFolderPath}{filenamePreview}</span>
            </div>
          </div>
        )}
      </div>
      */}

      {/* Save button — hidden when the page is already saved */}
      {!isAlreadySaved && (
        <button
          onClick={handleSave}
          disabled={!canSave}
          className="h-9 w-full rounded-md bg-zinc-950 px-4 text-sm font-medium text-zinc-50
                     shadow-sm transition-colors hover:bg-zinc-800
                     focus-visible:outline-none focus-visible:ring-2
                     focus-visible:ring-zinc-950 focus-visible:ring-offset-2
                     disabled:cursor-not-allowed disabled:opacity-50"
        >
          {isSaving ? "Saving..." : "Save to LLM Wiki"}
        </button>
      )}

      {checkingExisting && (
        <p className="text-xs text-zinc-500">Checking saved status...</p>
      )}
      {isAlreadySaved && status.type !== "success" && (
        <p className="text-xs text-emerald-700">
          This page is already in LLM Wiki.
        </p>
      )}

      <StatusFeedback status={status} />
    </div>
  );
}
