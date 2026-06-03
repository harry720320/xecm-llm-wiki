"""xECM-aware document service — overrides source listing to include xECM docs."""
import logging
from pathlib import Path

from config import settings
from services.local import LocalDocumentService

logger = logging.getLogger(__name__)


class XecmSourceDocumentService(LocalDocumentService):
    """Extends LocalDocumentService with xECM source document support.

    Source documents downloaded from xECM are cached locally and indexed
    in SQLite by the startup sync in main.py. xECM is read-only — all
    write operations skip filesystem writes for xECM documents.
    """

    def _is_xecm(self, doc: dict) -> bool:
        return (doc.get("relative_path") or "").startswith("xecm/")

    async def get_content(self, doc_id: str) -> dict | None:
        doc = await self.doc_repo.get(doc_id)
        if not doc:
            return None

        if doc.get("content"):
            return {"id": doc["id"], "content": doc["content"], "version": doc.get("version", 0)}

        if self._is_xecm(doc):
            return {
                "id": doc["id"],
                "content": f"[Binary file: {doc['filename']}]",
                "version": doc.get("version", 0),
            }

        return await super().get_content(doc_id)

    async def get_url(self, doc_id: str) -> dict | None:
        doc = await self.doc_repo.get(doc_id)
        if not doc:
            return None

        if self._is_xecm(doc):
            # Link to local file proxy which proxies to Content Server
            api_url = settings.API_URL.rstrip("/")
            relative = doc.get("relative_path", "")
            return {"url": f"{api_url}/v1/files/{relative}"}

        return await super().get_url(doc_id)

    async def update_content(self, doc_id: str, content: str) -> dict | None:
        doc = await self.doc_repo.get(doc_id)
        if not doc:
            return None

        if self._is_xecm(doc):
            # xECM is read-only — update SQLite only, skip filesystem write
            row = await self.doc_repo.update_content(doc_id, self.user_id, content)
            kb_id = await self.doc_repo.get_kb_id(doc_id)
            if kb_id and content:
                from services.chunker import chunk_text
                chunks = chunk_text(content) if content else []
                await self.chunk_repo.store(doc_id, self.user_id, kb_id, chunks)
            return row

        return await super().update_content(doc_id, content)

    async def delete(self, doc_id: str) -> bool:
        doc = await self.doc_repo.get(doc_id)
        if doc and self._is_xecm(doc):
            # xECM is read-only — archive in SQLite only
            return await self.doc_repo.archive(doc_id, self.user_id)

        return await super().delete(doc_id)

    async def update_metadata(self, doc_id: str, fields: dict) -> dict | None:
        doc = await self.doc_repo.get(doc_id)
        if doc and self._is_xecm(doc):
            # xECM is read-only — update SQLite only, skip filesystem rename
            fields = {k: v for k, v in fields.items() if k != "knowledge_base_id"}
            if not fields:
                return doc
            return await self.doc_repo.update_metadata(doc_id, self.user_id, **fields)

        return await super().update_metadata(doc_id, fields)


class XecmServiceFactory:
    """Service factory for xECM mode — injects XecmSourceDocumentService."""

    def __init__(self, db, storage=None, user_id: str = ""):
        self.db = db
        self.storage = storage
        self.user_id = user_id

    def user_service(self, user_id: str):
        from services.local import LocalUserService
        return LocalUserService(self.db, user_id)

    def kb_service(self, user_id: str):
        from services.local import LocalKBService
        return LocalKBService(self.db, user_id)

    def document_service(self, user_id: str) -> XecmSourceDocumentService:
        return XecmSourceDocumentService(self.db, user_id)

    def public_wiki_service(self):
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Public wikis aren't available in local mode.")
