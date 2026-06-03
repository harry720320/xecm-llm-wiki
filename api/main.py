import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware as _BaseCORSMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send


class CORSMiddleware(_BaseCORSMiddleware):
    """CORS middleware that passes WebSocket connections through.

    WebSocket auth is handled by JWT verification in the handler, not by
    origin checks. HTTP requests still get full CORS protection.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)

from config import settings

logger = logging.getLogger(__name__)

if settings.SENTRY_DSN:
    import sentry_sdk
    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        send_default_pii=True,
        traces_sample_rate=0.1,
        environment=settings.STAGE,
    )

if settings.LOGFIRE_TOKEN:
    import logfire
    logfire.configure(token=settings.LOGFIRE_TOKEN, service_name="supavault-api")
    logfire.instrument_asyncpg()

from routes.health import router as health_router
from routes.knowledge_bases import router as knowledge_bases_router
from routes.documents import router as documents_router
from routes.me import router as me_router
from routes.usage import router as usage_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.MODE == "local":
        async with _local_lifespan(app):
            yield
        return

    # ── Hosted mode ──
    # Prefetch the Supabase JWKS so the first authenticated request doesn't
    # pay the cold-cache cost and so a JWKS outage at boot is visible
    # immediately rather than masked behind the first auth error.
    from auth import prefetch_jwks
    await prefetch_jwks()

    import asyncpg
    pool = await asyncpg.create_pool(settings.DATABASE_URL, min_size=2, max_size=10)
    app.state.pool = pool
    app.state.mode = "hosted"

    s3_service = None
    ocr_service = None
    if settings.AWS_ACCESS_KEY_ID and settings.S3_BUCKET:
        from services.s3 import S3Service
        s3_service = S3Service()
    if s3_service:
        from services.ocr import OCRService
        ocr_service = OCRService(s3_service, pool)

    app.state.s3_service = s3_service
    app.state.ocr_service = ocr_service
    app.state.auth_provider = None  # Uses Supabase JWKS auth via deps.py

    from services.hosted import HostedServiceFactory
    app.state.factory = HostedServiceFactory(pool, s3_service, ocr_service)

    # Real-time document change notifications via WebSocket
    from routes.ws import setup_listener
    listener_task = await setup_listener(settings.DATABASE_URL)

    from infra.tus import cleanup_stale_uploads
    cleanup_task = asyncio.create_task(cleanup_stale_uploads())

    if ocr_service:
        rows = await pool.fetch(
            "SELECT id::text, user_id::text FROM documents "
            "WHERE status IN ('pending', 'processing') AND NOT archived"
        )
        for row in rows:
            logger.info("Recovering stuck document %s", row["id"][:8])
            asyncio.create_task(ocr_service.process_document(row["id"], row["user_id"]))

    yield

    cleanup_task.cancel()
    listener_task.cancel()
    await pool.close()


async def _index_cached_document(
    db, doc, cached_path: "Path", workspace_str: str
) -> None:
    """Index a cached xECM document into SQLite."""
    import hashlib
    import uuid
    from pathlib import Path as _Path
    from services.chunker import chunk_text

    relative = f"xecm/{doc.node_id}/{doc.name}"
    dir_path = f"/xecm/{doc.node_id}/"
    ext = doc.file_type
    stem = doc.name.rsplit(".", 1)[0] if "." in doc.name else doc.name
    title = stem.replace("-", " ").replace("_", " ").strip().title()

    content_bytes = cached_path.read_bytes()
    content_hash = hashlib.sha256(content_bytes).hexdigest()

    text_content = None
    simple_text_types = {"md", "txt", "csv", "svg", "json", "xml"}
    needs_processing = ext in {"pdf", "pptx", "ppt", "docx", "doc", "xlsx", "xls", "html", "htm"}

    if ext in simple_text_types:
        try:
            text_content = content_bytes.decode("utf-8", errors="replace")
        except Exception:
            pass

    cursor = await db.execute(
        "SELECT id FROM documents WHERE relative_path = ?", (relative,)
    )
    existing = await cursor.fetchone()
    if existing:
        doc_id = existing[0]
        await db.execute(
            "UPDATE documents SET content = ?, file_size = ?, content_hash = ?, "
            "mtime_ns = ?, last_indexed_at = datetime('now'), updated_at = datetime('now') "
            "WHERE id = ?",
            (text_content, doc.size, content_hash, 0, doc_id),
        )
        await db.commit()
    else:
        doc_id = str(uuid.uuid4())
        cursor = await db.execute(
            "SELECT COALESCE(MAX(document_number), 0) + 1 FROM documents"
        )
        row = await cursor.fetchone()
        doc_number = row[0]
        status = "ready" if text_content else "pending"

        await db.execute(
            "INSERT INTO documents (id, user_id, filename, title, path, relative_path, "
            "source_kind, file_type, file_size, status, content, tags, version, "
            "content_hash, mtime_ns, last_indexed_at, document_number) "
            "VALUES (?, (SELECT user_id FROM workspace LIMIT 1), ?, ?, ?, ?, "
            "'source', ?, ?, ?, ?, '[]', 0, ?, ?, datetime('now'), ?)",
            (doc_id, doc.name, title, dir_path, relative, ext or "bin",
             doc.size, status, text_content, content_hash, 0, doc_number),
        )
        await db.commit()

        if text_content:
            ws_row = await db.execute("SELECT id FROM workspace LIMIT 1")
            ws = await ws_row.fetchone()
            kb_id = ws[0] if ws else ""
            chunks = chunk_text(text_content)
            from infra.db.sqlite import SQLiteChunkRepository
            await SQLiteChunkRepository(db).store(doc_id, "local", kb_id, chunks)
        elif needs_processing:
            from domain.local_processor import process_document
            import asyncio as _asyncio
            _asyncio.create_task(process_document(db, doc_id, _Path(workspace_str)))


async def _local_lifespan_inner(app: FastAPI):
    """Local mode: SQLite + local filesystem + single-user auth."""
    import uuid
    from pathlib import Path
    from infra.db.sqlite import create_pool as create_sqlite_pool
    from infra.storage.local import LocalStorageService
    from infra.auth.local import LocalAuthProvider

    workspace = Path(settings.WORKSPACE_PATH).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "wiki").mkdir(exist_ok=True)
    (workspace / ".llmwiki").mkdir(exist_ok=True)
    (workspace / ".llmwiki" / "cache").mkdir(exist_ok=True)

    db_path = str(workspace / ".llmwiki" / "index.db")
    db = await create_sqlite_pool(db_path)

    local_user_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, "local"))
    auth_provider = LocalAuthProvider(local_user_id)
    storage = LocalStorageService(str(workspace), settings.API_URL)

    # Ensure workspace row exists
    cursor = await db.execute("SELECT id FROM workspace LIMIT 1")
    if not await cursor.fetchone():
        ws_id = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO workspace (id, name, description, user_id) VALUES (?, ?, '', ?)",
            (ws_id, workspace.name, local_user_id),
        )
        await db.commit()
        logger.info("Initialized local workspace: %s", workspace)

    # ── xECM mode: discover and cache source documents from xECM ──
    _xecm_enabled = settings.XECM_ENABLED
    if _xecm_enabled:
        from config import load_xecm_config
        from infra.xecm import XecmClient, XecmSourceReader

        xecm_cfg = load_xecm_config(str(workspace))
        xecm_url = xecm_cfg.get("XECM_URL") or settings.XECM_URL
        xecm_user = xecm_cfg.get("XECM_USERNAME") or settings.XECM_USERNAME
        xecm_pass = xecm_cfg.get("XECM_PASSWORD") or settings.XECM_PASSWORD
        xecm_ws = xecm_cfg.get("XECM_WORKSPACE") or settings.XECM_WORKSPACE

        if xecm_url and xecm_user and xecm_pass:
            xecm_client = XecmClient(url=xecm_url, username=xecm_user, password=xecm_pass)
            try:
                # "Enterprise" is the root workspace (node 2000)
                if xecm_ws.lower() == "enterprise":
                    ws_id = 2000
                    logger.info("xECM using Enterprise workspace (node 2000)")
                else:
                    ws_node = await xecm_client.find_workspace(xecm_ws)
                    if not ws_node:
                        logger.warning("xECM workspace '%s' not found — skipping sync", xecm_ws)
                        ws_id = None
                    else:
                        ws_id = ws_node["id"]
                        logger.info("xECM workspace '%s' -> node %s", xecm_ws, ws_id)

                if ws_id is not None:
                    source_docs = await XecmSourceReader(xecm_client).discover(ws_id)
                    cache_root = workspace / ".llmwiki" / "cache" / "sources"

                    for doc in source_docs:
                        try:
                            cached_path = await XecmSourceReader(xecm_client).download_to_cache(
                                doc.node_id, cache_root
                            )
                            await _index_cached_document(db, doc, cached_path, str(workspace))
                        except Exception as e:
                            logger.warning("Failed to sync xECM doc %s: %s", doc.name, e)

                    logger.info("xECM sync complete: %d documents", len(source_docs))
            except Exception as e:
                logger.error("xECM sync failed: %s", e)
            finally:
                await xecm_client.close()
        else:
            logger.warning("XECM_ENABLED but config incomplete — check .xecm_config")

    app.state.mode = "local"
    app.state.pool = None  # No asyncpg pool in local mode
    app.state.sqlite_db = db
    app.state.s3_service = None
    app.state.storage_service = storage
    app.state.ocr_service = None
    app.state.auth_provider = auth_provider
    app.state.workspace_path = str(workspace)

    if _xecm_enabled:
        from services.xecm_source import XecmServiceFactory
        app.state.factory = XecmServiceFactory(db, storage, local_user_id)
    else:
        from services.local import LocalServiceFactory
        app.state.factory = LocalServiceFactory(db, storage, local_user_id)

    logger.info("Local mode — workspace: %s", workspace)
    return db


@asynccontextmanager
async def _local_lifespan(app: FastAPI):
    db = await _local_lifespan_inner(app)

    # Start file watcher
    watcher_task = None
    try:
        from domain.watcher import watch_workspace
        from pathlib import Path
        workspace = Path(app.state.workspace_path)
        watcher_task = asyncio.create_task(watch_workspace(db, workspace))
        logger.info("File watcher started")
    except ImportError:
        logger.warning("watchfiles not installed — file watcher disabled")

    try:
        yield
    finally:
        if watcher_task:
            watcher_task.cancel()
            try:
                await watcher_task
            except asyncio.CancelledError:
                pass
        await db.close()


app = FastAPI(title="LLM Wiki API", lifespan=lifespan)

# Rate limiting — applied as middleware so every authenticated route gets a
# broad ceiling. Hot endpoints can add tighter `@limiter.limit(...)` overrides.
# Skip in local mode where there's only one user.
if settings.MODE != "local":
    from slowapi import _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded
    from slowapi.middleware import SlowAPIMiddleware

    from infra.rate_limit import limiter

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.APP_URL],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=[
        "Location", "Upload-Offset", "Upload-Length",
        "Tus-Resumable", "Tus-Version", "Tus-Max-Size", "Tus-Extension",
        "X-Document-Id",
    ],
)

if settings.LOGFIRE_TOKEN:
    import logfire
    logfire.instrument_fastapi(app)

app.include_router(health_router)
app.include_router(me_router)
app.include_router(usage_router)
app.include_router(knowledge_bases_router)
app.include_router(documents_router)

if settings.MODE == "local":
    from routes.local_upload import router as local_upload_router
    from routes.files import router as files_router, set_workspace_root
    from routes.local_graph import router as local_graph_router
    app.include_router(local_upload_router)
    app.include_router(files_router)
    app.include_router(local_graph_router)
    set_workspace_root(settings.WORKSPACE_PATH)
    from routes.wiki import router as wiki_router
    app.include_router(wiki_router)
else:
    from routes.api_keys import router as api_keys_router
    from routes.admin import router as admin_router
    from routes.graph import router as graph_router
    from routes.ws import router as ws_router
    from routes.public import router as public_router
    from infra.tus import router as tus_router
    app.include_router(api_keys_router)
    app.include_router(admin_router)
    app.include_router(tus_router)
    app.include_router(graph_router)
    app.include_router(ws_router)
    app.include_router(public_router)
