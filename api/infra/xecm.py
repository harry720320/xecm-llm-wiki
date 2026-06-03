"""xECM Content Server REST API client (read-only subset)."""
import logging
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)


class XecmError(Exception):
    """Base exception for xECM client errors."""


class XecmAuthError(XecmError):
    """Authentication failed."""


class XecmNotFoundError(XecmError):
    """Node not found."""


class XecmTimeoutError(XecmError):
    """Request timed out."""


class XecmClient:
    """Async HTTP client for Content Server REST API v1.

    Authentication is username/password -> ticket (cached and auto-refreshed on 401).
    """

    def __init__(self, url: str, username: str, password: str):
        self._base = url.rstrip("/") + "/api/v1"
        self._username = username
        self._password = password
        self._ticket: str | None = None
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def authenticate(self) -> str:
        """Obtain or return a cached auth ticket."""
        if self._ticket:
            return self._ticket

        client = await self._get_client()
        resp = await client.post(
            self._base.replace("/api/v1", "") + "/api/v1/auth",
            data={"username": self._username, "password": self._password},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        data = resp.json()
        self._ticket = data["ticket"]
        logger.info("xECM authenticated")
        return self._ticket

    async def _request(
        self, method: str, path: str, retry_auth: bool = True, **kwargs
    ) -> httpx.Response:
        client = await self._get_client()
        ticket = await self.authenticate()
        headers = kwargs.pop("headers", {})
        headers["OTCSTicket"] = ticket
        headers.setdefault("Accept", "application/json")

        resp = await client.request(method, self._base + path, headers=headers, **kwargs)
        if resp.status_code == 401 and retry_auth:
            self._ticket = None
            return await self._request(method, path, retry_auth=False, **kwargs)
        return resp

    async def get_node(self, node_id: int) -> dict:
        """Get metadata for a node by ID."""
        resp = await self._request("GET", f"/nodes/{node_id}")
        if resp.status_code == 404:
            raise XecmNotFoundError(f"Node {node_id} not found")
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", data)

    async def list_nodes(self, parent_id: int, limit: int = 100) -> list[dict]:
        """List child nodes of a parent (folders and documents)."""
        resp = await self._request(
            "GET", f"/nodes/{parent_id}/nodes", params={"limit": str(limit)}
        )
        if resp.status_code == 404:
            raise XecmNotFoundError(f"Parent node {parent_id} not found")
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", []) if isinstance(data, dict) else []

    async def download(self, node_id: int) -> bytes:
        """Download document content as bytes."""
        resp = await self._request("GET", f"/nodes/{node_id}/content")
        if resp.status_code == 404:
            raise XecmNotFoundError(f"Document {node_id} not found")
        resp.raise_for_status()
        return resp.content

    async def download_to_path(self, node_id: int, dest: Path) -> None:
        """Download document content to a local file path."""
        content = await self.download(node_id)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)

    async def find_by_name(
        self, parent_id: int, name: str, kind: int | None = None
    ) -> dict | None:
        """Find a child node by name under a parent. Returns None if not found."""
        nodes = await self.list_nodes(parent_id, limit=200)
        for node in nodes:
            if node.get("name") == name:
                if kind is not None and node.get("type") != kind:
                    continue
                return node
        return None

    async def find_workspace(self, name: str) -> dict | None:
        """Find a workspace by name under Enterprise (node 2000)."""
        return await self.find_by_name(2000, name)


from dataclasses import dataclass


@dataclass
class SourceDoc:
    """A source document discovered in an xECM workspace."""
    node_id: int
    parent_id: int
    name: str
    file_type: str
    size: int
    create_date: str
    modify_date: str


class XecmSourceReader:
    """Discovers documents in an xECM workspace and downloads them to local cache."""

    def __init__(self, client: XecmClient):
        self._client = client

    async def discover(self, root_id: int) -> list[SourceDoc]:
        """Recursively discover all documents (type 144) in a workspace."""
        result: list[SourceDoc] = []
        await self._walk(root_id, result)
        return result

    async def _walk(self, node_id: int, result: list[SourceDoc]) -> None:
        children = await self._client.list_nodes(node_id, limit=200)
        for child in children:
            node_type = child.get("type")
            if node_type == 144:
                name = child.get("name", "unknown")
                ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
                result.append(SourceDoc(
                    node_id=child["id"],
                    parent_id=child["parent_id"],
                    name=name,
                    file_type=ext,
                    size=child.get("size", 0),
                    create_date=child.get("create_date", ""),
                    modify_date=child.get("modify_date", ""),
                ))
            elif child.get("container"):
                await self._walk(child["id"], result)

    async def download_to_cache(self, node_id: int, cache_dir: Path) -> Path:
        """Download a document to a local cache directory.
        Returns the path to the cached file.
        """
        node = await self._client.get_node(node_id)
        filename = node.get("name", f"doc-{node_id}")
        dest_dir = cache_dir / str(node_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / filename
        await self._client.download_to_path(node_id, dest_path)
        return dest_path
