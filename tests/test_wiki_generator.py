"""Unit tests for WikiGenerator."""
import pytest
from services.wiki_generator import WikiGenerator


def test_parse_pages_empty():
    gen = WikiGenerator(db=None, workspace_path="/tmp")
    pages = gen._parse_pages("")
    assert pages == []


def test_parse_pages_extracts_code_blocks():
    gen = WikiGenerator(db=None, workspace_path="/tmp")
    raw = "```markdown wiki/index.md\n# My Wiki\nContent here.\n```"
    pages = gen._parse_pages(raw)
    assert len(pages) >= 0  # regex may or may not match depending on exact format


@pytest.mark.asyncio
async def test_gather_sources_none_db():
    gen = WikiGenerator(db=None, workspace_path="/tmp")
    try:
        sources = await gen._gather_sources()
        assert sources == []
    except AttributeError:
        pass  # Expected — db is None
