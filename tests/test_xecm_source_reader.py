"""Integration tests for XecmSourceReader."""
import pytest
from pathlib import Path
import tempfile
from infra.xecm import XecmClient, XecmSourceReader, SourceDoc

XECM_URL = "http://192.168.0.29/otcs/cs.exe"


@pytest.fixture
async def client():
    c = XecmClient(url=XECM_URL, username="admin", password="OpenText1")
    yield c
    await c.close()


@pytest.fixture
async def reader(client):
    return XecmSourceReader(client)


@pytest.mark.asyncio
async def test_discover_enterprise(reader):
    docs = await reader.discover(2000)
    assert isinstance(docs, list)
    assert len(docs) >= 2  # known PDF + log file

    pdf = next((d for d in docs if d.node_id == 11061), None)
    assert pdf is not None
    assert pdf.name == "Extended ECM CE 24.1 Release Notes.pdf"
    assert pdf.file_type == "pdf"
    assert pdf.size > 0


@pytest.mark.asyncio
async def test_download_and_cache(reader):
    with tempfile.TemporaryDirectory() as tmpdir:
        cache_dir = Path(tmpdir) / "sources" / "11061"
        path = await reader.download_to_cache(11061, cache_dir)
        assert path.is_file()
        assert path.stat().st_size > 0

        content = path.read_bytes()
        assert content[:4] == b"%PDF"
