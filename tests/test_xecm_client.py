"""Integration tests for XecmClient — requires xECM demo instance at 192.168.0.29."""
import pytest
from infra.xecm import XecmClient, XecmAuthError, XecmNotFoundError

XECM_URL = "http://192.168.0.29/otcs/cs.exe"
XECM_USER = "admin"
XECM_PASS = "OpenText1"


@pytest.fixture
async def client():
    c = XecmClient(url=XECM_URL, username=XECM_USER, password=XECM_PASS)
    yield c
    await c.close()


@pytest.mark.asyncio
async def test_authenticate(client):
    ticket = await client.authenticate()
    assert ticket is not None
    assert len(ticket) > 20


@pytest.mark.asyncio
async def test_get_node_enterprise_workspace(client):
    node = await client.get_node(2000)
    assert node["id"] == 2000
    assert node["type"] == 141
    assert node["type_name"] == "Enterprise Workspace"


@pytest.mark.asyncio
async def test_list_nodes(client):
    nodes = await client.list_nodes(2000, limit=5)
    assert isinstance(nodes, list)


@pytest.mark.asyncio
async def test_download_document(client):
    content = await client.download(11061)
    assert len(content) > 0
    assert content[:4] == b"%PDF"


@pytest.mark.asyncio
async def test_download_not_found(client):
    with pytest.raises(XecmNotFoundError):
        await client.download(99999999)


@pytest.mark.asyncio
async def test_find_by_name(client):
    result = await client.find_by_name(
        2000, "Extended ECM CE 24.1 Release Notes.pdf", kind=144
    )
    assert result is not None
    assert result["id"] == 11061
