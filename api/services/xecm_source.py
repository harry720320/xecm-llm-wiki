"""xECM-aware document service — overrides source listing to include xECM docs."""
import logging
from pathlib import Path

from config import settings
from services.local import LocalDocumentService

logger = logging.getLogger(__name__)


class XecmSourceDocumentService(LocalDocumentService):
    """Extends LocalDocumentService with xECM source document support.

    Source documents downloaded from xECM are cached locally and indexed
    in SQLite by the startup sync in main.py. This service serves them
    from the local cache, downloading on-demand if missing.
    """

    async def get_content(self, doc_id: str) -> dict | None:
        """Get document content. For xECM source docs, serves from local cache."""
        doc = await self.doc_repo.get(doc_id)
        if not doc:
            return None

        if doc.get("content"):
            return {"id": doc["id"], "content": doc["content"], "version": doc.get("version", 0)}

        relative = doc.get("relative_path", "")
        if relative.startswith("xecm/"):
            cached = Path(settings.WORKSPACE_PATH) / ".llmwiki" / "cache" / "sources" / relative.replace("xecm/", "")
            if cached.is_file():
                return {
                    "id": doc["id"],
                    "content": f"[Binary file cached at: {cached}]",
                    "version": doc.get("version", 0),
                }

        return await super().get_content(doc_id)

    async def get_url(self, doc_id: str) -> dict | None:
        """Get URL for viewing/downloading a document."""
        doc = await self.doc_repo.get(doc_id)
        if not doc:
            return None

        api_url = settings.API_URL.rstrip("/")
        relative = doc.get("relative_path", "")

        if relative.startswith("xecm/"):
            return {"url": f"{api_url}/v1/files/{relative}"}

        return await super().get_url(doc_id)


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
