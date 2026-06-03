"""Wiki generation endpoint — triggers LLM-powered wiki generation."""
import logging
from fastapi import APIRouter, HTTPException, Request

from config import settings

logger = logging.getLogger(__name__)
router = APIRouter(tags=["wiki"])


@router.post("/v1/wiki/generate")
async def generate_wiki(request: Request):
    """Trigger wiki generation from indexed source documents."""
    if not settings.ANTHROPIC_API_KEY:
        raise HTTPException(
            status_code=400,
            detail="ANTHROPIC_API_KEY not configured. Set it in .env or environment.",
        )

    db = request.app.state.sqlite_db
    workspace = request.app.state.workspace_path

    from services.wiki_generator import WikiGenerator
    generator = WikiGenerator(db, workspace)
    result = await generator.generate(kb_id="")

    return result


@router.get("/v1/wiki/status")
async def wiki_status(request: Request):
    """Get wiki generation status — counts of source vs wiki pages."""
    db = request.app.state.sqlite_db
    cursor = await db.execute(
        "SELECT source_kind, COUNT(*) as count FROM documents "
        "WHERE status = 'ready' GROUP BY source_kind"
    )
    rows = await cursor.fetchall()
    status = {"source_count": 0, "wiki_count": 0}
    for row in rows:
        if row[0] == "source":
            status["source_count"] = row[1]
        elif row[0] == "wiki":
            status["wiki_count"] = row[1]
    return status
