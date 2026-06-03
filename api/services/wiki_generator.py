"""Standalone wiki generation using Anthropic API (not Claude Code MCP).

Reads indexed source documents from SQLite, constructs prompts,
calls the Anthropic Messages API, and writes wiki pages to disk.
"""
import json
import logging
import re
import uuid
import hashlib
from pathlib import Path

import anthropic

from config import settings

logger = logging.getLogger(__name__)

WIKI_SYSTEM_PROMPT = """You are a wiki generator. Given source documents, create a comprehensive wiki.

CRITICAL FORMAT RULE: You MUST output every wiki page in this exact format:

### wiki/index.md
( page content )

### wiki/entities/my-entity.md
( page content )

### wiki/topics/my-topic.md
( page content )

Each page starts with ### wiki/path.md on its own line. The ### must be at the beginning of the line. Then the page content follows. Pages are separated by a blank line.

Generate AT LEAST these pages:
1. wiki/index.md — overview of all topics
2. wiki/entities/<slug>.md — at least 3 entity pages for key concepts found in sources
3. wiki/topics/<slug>.md — at least 2 topic pages

Rules:
- Every factual claim MUST include a citation: [source: filename]
- Use the exact filenames from the source documents for citations
- Page titles should be concise (3-6 words)
- Use markdown headings, lists, and tables
- CRITICAL: Output MULTIPLE pages using the ### wiki/path.md format
"""


class WikiGenerator:
    """Generates wiki pages from indexed source documents using Anthropic API."""

    def __init__(self, db, workspace_path: str):
        self._db = db
        self._workspace = Path(workspace_path)
        kwargs = {
            "api_key": settings.ANTHROPIC_API_KEY,
            "timeout": 180.0,
            "max_retries": 2,
        }
        if settings.ANTHROPIC_BASE_URL:
            kwargs["base_url"] = settings.ANTHROPIC_BASE_URL
        self._client = anthropic.AsyncAnthropic(**kwargs)
        self._model = settings.ANTHROPIC_WIKI_MODEL

    async def generate(self, kb_id: str) -> dict:
        """Generate wiki pages for a knowledge base. Returns summary dict."""
        sources = await self._gather_sources()
        if not sources:
            return {"status": "no_sources", "pages_created": 0}

        logger.info("Generating wiki from %d source documents", len(sources))

        wiki_pages = await self._generate_pages(sources)

        written = 0
        for page in wiki_pages:
            try:
                await self._write_page(page)
                written += 1
            except Exception as e:
                logger.warning("Failed to write page %s: %s", page.get("path", "?"), e)

        return {"status": "complete", "pages_created": written}

    async def _gather_sources(self) -> list[dict]:
        """Gather source document content from SQLite.
        Includes both ready and pending docs. Pending docs get metadata-only entries.
        """
        cursor = await self._db.execute(
            "SELECT id, filename, title, content, file_type, relative_path, status, file_size "
            "FROM documents WHERE source_kind = 'source' AND status != 'failed' "
            "ORDER BY filename"
        )
        rows = await cursor.fetchall()
        if not rows:
            return []

        cols = [d[0] for d in cursor.description]
        results = []
        for row in rows:
            doc = dict(zip(cols, row))
            content = doc.get("content")
            if not content:
                # Include metadata for non-text files so the wiki covers them
                doc["content"] = (
                    f"[Binary file: {doc['filename']} "
                    f"({doc.get('file_type', 'unknown')}, "
                    f"{doc.get('file_size', 0)} bytes)]\n"
                    f"This file was present in the workspace but its text could not be extracted."
                )
            results.append(doc)
        return results

    async def _generate_pages(self, sources: list[dict]) -> list[dict]:
        """Call Anthropic API to generate wiki pages from source documents."""
        source_text = ""
        for doc in sources:
            content = doc.get("content") or ""
            if not content:
                continue
            max_chars = min(len(content), 80000 // max(len(sources), 1))
            truncated = content[:max_chars]
            if len(content) > max_chars:
                truncated += "\n\n[... truncated]"
            source_text += f"\n\n## Source: {doc['filename']}\n\n{truncated}\n"

        if not source_text.strip():
            return []

        user_prompt = (
            f"Generate multiple wiki pages from these source documents. "
            f"REMEMBER: Every page MUST start with ### wiki/path.md on its own line.\n\n"
            f"Required pages:\n"
            f"- wiki/index.md\n"
            f"- wiki/entities/*.md (at least 3 entity pages)\n"
            f"- wiki/topics/*.md (at least 2 topic pages)\n\n"
            f"{source_text}\n\n"
            f"Now output the wiki pages. Start each page with ### wiki/path.md:"
        )

        try:
            raw_text = ""
            async with self._client.messages.stream(
                model=self._model,
                max_tokens=32000,
                system=WIKI_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            ) as stream:
                async for event in stream:
                    if event.type == "content_block_delta":
                        if hasattr(event.delta, 'text'):
                            raw_text += event.delta.text
                        elif hasattr(event.delta, 'text_delta'):
                            raw_text += event.delta.text_delta
                    elif event.type == "content_block_start":
                        if hasattr(event, 'content_block') and hasattr(event.content_block, 'text'):
                            raw_text += event.content_block.text
                    elif event.type == "message_delta":
                        if hasattr(event, 'delta') and hasattr(event.delta, 'text'):
                            raw_text += event.delta.text

            logger.info("Streaming response: %d chars", len(raw_text))
            return self._parse_pages(raw_text)

        except Exception as e:
            logger.error("Wiki generation API call failed: %s", e)
            return []

    def _parse_pages(self, raw_text: str) -> list[dict]:
        """Parse LLM output into structured pages.

        Handles every format the LLM might produce:
        - ### wiki/path/page.md (plain h3 heading)
        - # wiki/path/page.md (h1 heading)
        - Backtick-wrapped variants
        - Code block wrapped content
        - Single page fallback (saves as wiki/index.md)
        """
        pages = []
        text = raw_text.strip()

        # Strip wrapping code block markers
        if text.startswith("```"):
            lines = text.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()

        # Split on ### wiki/ or # wiki/ page headers (any heading level 1-3)
        marker_pattern = r'(?:^|\n)(?=(?:#{1,3})\s+`?wiki/[^\s`\n]+\.md`?\s*\n)'
        sections = re.split(marker_pattern, text)

        for section in sections:
            section = section.strip()
            if not section:
                continue
            m = re.match(
                r'(?:#{1,3})\s+`?(wiki/[^\s`\n]+\.md)`?\s*\n(.*)',
                section, re.DOTALL
            )
            if m:
                pages.append({"path": m.group(1).strip(), "content": m.group(2).strip()})

        # Fallback: single page with no page delimiters
        if not pages and len(text) > 200:
            title_match = re.match(r'#\s+(.+)', text)
            if title_match:
                slug = re.sub(r'[^\w-]', '', title_match.group(1).lower().replace(' ', '-')[:50])
            else:
                slug = 'index'
            pages.append({"path": f"wiki/{slug}.md", "content": text})

        return pages

    async def _write_page(self, page: dict) -> None:
        """Write a wiki page to the local filesystem and update SQLite."""
        path = page["path"]
        content = page["content"]
        full_path = self._workspace / path
        full_path.parent.mkdir(parents=True, exist_ok=True)

        full_path.write_text(content, encoding="utf-8")

        filename = full_path.name
        dir_path = "/" + str(full_path.parent.relative_to(self._workspace)).replace("\\", "/") + "/"
        relative = str(full_path.relative_to(self._workspace)).replace("\\", "/")
        stem = filename.rsplit(".", 1)[0] if "." in filename else filename
        title = stem.replace("-", " ").replace("_", " ").strip().title()
        content_hash = hashlib.sha256(content.encode()).hexdigest()

        cursor = await self._db.execute(
            "SELECT id FROM documents WHERE relative_path = ?", (relative,)
        )
        existing = await cursor.fetchone()

        if existing:
            doc_id = existing[0]
            await self._db.execute(
                "UPDATE documents SET content = ?, title = ?, content_hash = ?, "
                "last_indexed_at = datetime('now'), updated_at = datetime('now') "
                "WHERE id = ?",
                (content, title, content_hash, doc_id),
            )
        else:
            doc_id = str(uuid.uuid4())
            cursor = await self._db.execute(
                "SELECT COALESCE(MAX(document_number), 0) + 1 FROM documents"
            )
            row = await cursor.fetchone()
            doc_number = row[0]

            await self._db.execute(
                "INSERT INTO documents (id, user_id, filename, title, path, relative_path, "
                "source_kind, file_type, file_size, status, content, tags, version, "
                "content_hash, mtime_ns, last_indexed_at, document_number) "
                "VALUES (?, (SELECT user_id FROM workspace LIMIT 1), ?, ?, ?, ?, "
                "'wiki', 'md', ?, 'ready', ?, '[]', 0, ?, ?, datetime('now'), ?)",
                (doc_id, filename, title, dir_path, relative,
                 len(content.encode()), content, content_hash, 0, doc_number),
            )

        from services.chunker import chunk_text
        from infra.db.sqlite import SQLiteChunkRepository

        ws_row = await self._db.execute("SELECT id FROM workspace LIMIT 1")
        ws = await ws_row.fetchone()
        kb_id = ws[0] if ws else ""

        chunks = chunk_text(content)
        await SQLiteChunkRepository(self._db).store(doc_id, "local", kb_id, chunks)

        await self._db.commit()
        logger.info("Wiki page written: %s", path)
