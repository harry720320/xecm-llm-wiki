"""Live wiki generation test using xECM source documents + Anthropic-compatible API.

The wiki generator reads source docs from SQLite (indexed from xECM on startup)
and calls the LLM API to generate wiki pages.

Usage: set ANTHROPIC_AUTH_TOKEN and ANTHROPIC_BASE_URL in env, then run.
"""
import asyncio
import os
import sys
import tempfile
import uuid
from pathlib import Path

# Add api to path
sys.path.insert(0, str(Path(__file__).parent.parent / "api"))

# Use Claude Code's API settings
API_KEY = os.environ["ANTHROPIC_AUTH_TOKEN"]
BASE_URL = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
# Use a model that works with the configured backend
MODEL = os.environ.get("ANTHROPIC_DEFAULT_SONNET_MODEL", "claude-sonnet-4-6")

print(f"Base URL: {BASE_URL}")
print(f"Model: {MODEL}")


async def main():
    import aiosqlite
    from anthropic import AsyncAnthropic

    with tempfile.TemporaryDirectory() as tmpdir:
        ws = Path(tmpdir)
        (ws / "wiki").mkdir(exist_ok=True)

        # ── Step 1: Set up SQLite with source docs ──
        db_path = str(ws / "index.db")
        db = await aiosqlite.connect(db_path)
        await db.execute("PRAGMA journal_mode=WAL")

        # Create minimal schema
        await db.execute("""
            CREATE TABLE IF NOT EXISTS workspace (
                id TEXT PRIMARY KEY, name TEXT, description TEXT, user_id TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY, user_id TEXT, filename TEXT, title TEXT,
                path TEXT, relative_path TEXT, source_kind TEXT, file_type TEXT,
                file_size INTEGER, status TEXT, content TEXT, tags TEXT DEFAULT '[]',
                version INTEGER DEFAULT 0, content_hash TEXT, mtime_ns INTEGER,
                last_indexed_at TEXT, document_number INTEGER,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                page_count INTEGER, error_message TEXT, parser TEXT, metadata TEXT,
                stale_since TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS document_chunks (
                id TEXT PRIMARY KEY, document_id TEXT, chunk_index INTEGER,
                content TEXT, source_content TEXT, page INTEGER,
                start_char INTEGER, token_count INTEGER, header_breadcrumb TEXT
            )
        """)

        ws_id = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO workspace (id, name, description, user_id) VALUES (?, ?, '', 'local')",
            (ws_id, "Test Wiki"),
        )

        # Insert the xECM PDF as a source doc (use its metadata, content as summary text
        # since we can't extract PDF text in this test)
        doc_id = str(uuid.uuid4())
        content = """Extended ECM CE 24.1 Release Notes

This document covers the new features in OpenText Extended ECM Content Edition 24.1.

Key Features:
1. Enhanced REST API v1 with improved node management
2. Business Workspace (type 848) support for xECM integration
3. Extended ECM volume folders (type 882) for content organization
4. Wiki type (5573) for native wiki pages within Content Server
5. Improved authentication with OTCSTicket token-based access
6. Support for document types including PDF, Office, and text files
7. Container-based hierarchy with folders and workspaces

The Content Server REST API provides endpoints for:
- Authentication via POST /api/v1/auth
- Node listing via GET /api/v1/nodes/{id}/nodes
- Content download via GET /api/v1/nodes/{id}/content
- Node metadata via GET /api/v1/nodes/{id}

Enterprise Workspace (node 2000) is the root container for all content.
Documents are type 144, folders are type 0, Business Workspaces are type 848.
"""

        await db.execute(
            "INSERT INTO documents (id, user_id, filename, title, path, relative_path, "
            "source_kind, file_type, file_size, status, content, tags, version, "
            "content_hash, mtime_ns, last_indexed_at, document_number) "
            "VALUES (?, 'local', ?, ?, '/', ?, 'source', 'pdf', ?, 'ready', ?, '[]', 0, "
            "'abc', 0, datetime('now'), 1)",
            (doc_id, "xECM_24.1_Release_Notes.pdf", "xECM 24.1 Release Notes",
             "xECM_24.1_Release_Notes.pdf", len(content), content),
        )

        # Insert a second source document
        doc_id2 = str(uuid.uuid4())
        content2 = """LLM Wiki Integration with xECM

Architecture Overview:

The LLM Wiki system integrates with OpenText xECM Content Server to provide
automated wiki generation from enterprise content stored in xECM workspaces.

Components:
1. XecmClient - Async HTTP client for Content Server REST API
   - Authentication using username/password to obtain OTCSTicket
   - Node listing, content download, metadata retrieval
   - Read-only operations (no create/update/delete)

2. XecmSourceReader - Workspace discovery and document caching
   - Recursively traverses workspace folders
   - Discovers documents (type 144) within Business Workspaces (type 848)
   - Downloads content to local cache directory
   - Returns SourceDoc objects with metadata

3. WikiGenerator - Standalone wiki generation using LLM API
   - Reads indexed source documents from SQLite
   - Constructs prompts with source content
   - Calls LLM API to generate structured wiki pages
   - Writes output to local filesystem as markdown
   - No Claude Code dependency

4. XecmSourceDocumentService - Service layer for xECM source docs
   - Extends LocalDocumentService
   - Serves xECM documents from local cache
   - Falls back to on-demand download if cache miss

Configuration:
- .xecm_config file in workspace root
- XECM_URL, XECM_USERNAME, XECM_PASSWORD, XECM_WORKSPACE
- ANTHROPIC_API_KEY for wiki generation
- XECM_ENABLED flag to toggle xECM mode

The wiki is generated as markdown files in wiki/ directory with:
- wiki/index.md - overview of all topics
- wiki/entities/*.md - entity detail pages
- wiki/topics/*.md - cross-cutting topic pages
Every claim includes a citation [source: filename].
"""

        await db.execute(
            "INSERT INTO documents (id, user_id, filename, title, path, relative_path, "
            "source_kind, file_type, file_size, status, content, tags, version, "
            "content_hash, mtime_ns, last_indexed_at, document_number) "
            "VALUES (?, 'local', ?, ?, '/', ?, 'source', 'md', ?, 'ready', ?, '[]', 0, "
            "'def', 0, datetime('now'), 2)",
            (doc_id2, "LLM_Wiki_xECM_Integration.md", "LLM Wiki xECM Integration",
             "LLM_Wiki_xECM_Integration.md", len(content2), content2),
        )
        await db.commit()

        print(f"Inserted 2 source documents into SQLite")

        # ── Step 2: Generate wiki via Anthropic-compatible API ──
        client = AsyncAnthropic(
            api_key=API_KEY,
            base_url=BASE_URL,
        )

        # Build prompt from source docs
        source_text = ""
        cursor = await db.execute(
            "SELECT filename, content FROM documents WHERE source_kind = 'source' AND status = 'ready'"
        )
        rows = await cursor.fetchall()
        for filename, content in rows:
            source_text += f"\n\n## Source: {filename}\n\n{content}\n"

        system_prompt = """You are a wiki generator. Given source documents, create a comprehensive wiki.

Output format — write wiki pages as markdown:

1. Index page (wiki/index.md): Overview of all topics covered.
2. Entity pages (wiki/entities/<slug>.md): One page per entity with summary, details, citations.
3. Topic pages (wiki/topics/<slug>.md): Broader topics spanning multiple documents.

Rules:
- Every factual claim MUST include a citation: [source: filename]
- Use exact filenames from sources for citations
- Page titles concise (3-6 words)
- Use markdown headings, lists, and tables"""

        user_prompt = (
            "Generate a wiki from the following source documents. "
            "Output each wiki page starting with its file path on a line by itself "
            "in the format: ### `wiki/path/page.md` followed by the page content.\n\n"
            f"{source_text}\n\n"
            "Generate wiki/index.md, plus entity pages and topic pages. "
            "Include citations [source: filename] for all facts."
        )

        print(f"\nCalling API ({MODEL})...")
        print(f"Source text: {len(source_text)} chars")

        try:
            response = await client.messages.create(
                model=MODEL,
                max_tokens=16000,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )

            # Filter for TextBlock content (skip ThinkingBlock from DeepSeek)
            raw_text = ""
            for block in response.content:
                if hasattr(block, 'text'):
                    raw_text += block.text
            print(f"\n=== API Response ({len(raw_text)} chars) ===\n")
            print(raw_text)
            print("\n=== End Response ===\n")

            # Count pages by finding page path markers
            import re
            pages = re.findall(r'###\s+`(wiki/[^`]+\.md)`', raw_text)
            print(f"Wiki pages generated: {len(pages)}")
            for p in pages:
                print(f"  - {p}")

        except Exception as e:
            print(f"API call failed: {e}")
            import traceback
            traceback.print_exc()

        finally:
            await client.close()
            await db.close()


if __name__ == "__main__":
    asyncio.run(main())
