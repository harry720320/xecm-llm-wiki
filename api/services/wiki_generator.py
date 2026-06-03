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

Output format — write wiki pages as markdown files:

1. Index page (wiki/index.md): Overview of all topics covered in the source documents.

2. Entity pages (wiki/entities/<slug>.md): One page per significant entity (person, company, product, technology, concept). Include summary, key details from sources, citations [source: filename], and cross-references.

3. Topic pages (wiki/topics/<slug>.md): Broader topics that span multiple documents.

Rules:
- Every factual claim MUST include a citation: [source: filename]
- Use the exact filenames from the source documents for citations
- If sources disagree, note the disagreement
- No boilerplate intros
- Page titles should be concise (3-6 words)
- Use markdown headings, lists, and tables where appropriate
"""


class WikiGenerator:
    """Generates wiki pages from indexed source documents using Anthropic API."""

    def __init__(self, db, workspace_path: str):
        self._db = db
        self._workspace = Path(workspace_path)
        kwargs = {"api_key": settings.ANTHROPIC_API_KEY}
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
        """Gather source document content from SQLite."""
        cursor = await self._db.execute(
            "SELECT id, filename, title, content, file_type, relative_path "
            "FROM documents WHERE source_kind = 'source' AND status = 'ready' "
            "ORDER BY filename"
        )
        rows = await cursor.fetchall()
        if not rows:
            return []

        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in rows]

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
            f"Generate a wiki from the following source documents. "
            f"Output each wiki page as a markdown code block with the file path:\n\n"
            f"{source_text}\n\n"
            f"Generate: 1) wiki/index.md (overview), "
            f"2) wiki/entities/*.md (entity pages), "
            f"3) wiki/topics/*.md (topic pages). "
            f"Include citations [source: filename] for all facts."
        )

        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=32000,
                system=WIKI_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )

            raw_text = ""
            for block in response.content:
                if hasattr(block, 'text'):
                    raw_text += block.text
            return self._parse_pages(raw_text)

        except Exception as e:
            logger.error("Wiki generation API call failed: %s", e)
            return []

    def _parse_pages(self, raw_text: str) -> list[dict]:
        """Parse LLM output into structured pages."""
        pages = []
        pattern = r'(?:###\s+`([^`]+\.md)`|```markdown\s+([^\n]+\.md))\n(.*?)```'
        matches = re.findall(pattern, raw_text, re.DOTALL)

        for match in matches:
            path = match[0] or match[1]
            content = (match[2] if len(match) > 2 else match[1]).strip()
            if path and content:
                pages.append({"path": path, "content": content})

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
