import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlmodel import Session

from app.api.v1.router import api_router
from app.api.deps import SessionDep
from app.core.config import settings
from app.core.logging import configure_logging
from app.db.seed import seed
from app.db.session import engine, init_db, startup_lock
from app.services.auth import purge_expired_sessions
from app.services.finance import ensure_default_categories
from app.services.imports import sweep_staging
from app.services.public_site import cache as public_cache

configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.init_db_on_startup:
        with startup_lock():
            init_db()
            with Session(engine) as session:
                ensure_default_categories(session)
                purge_expired_sessions(session)
                session.commit()
        sweep_staging()
    if settings.seed_on_startup:
        with Session(engine) as session:
            seed(session)
    yield


_docs = settings.docs_enabled
app = FastAPI(
    title=settings.app_name,
    version=settings.version,
    lifespan=lifespan,
    docs_url="/docs" if _docs else None,
    redoc_url="/redoc" if _docs else None,
    openapi_url="/openapi.json" if _docs else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    # The session cookie may only cross origins to an explicit allow list: with "*",
    # Starlette would echo back any caller's origin and let every site make signed-in calls.
    allow_credentials="*" not in settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _origin_of(url: str) -> str | None:
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}".lower()


def _same_site_write(request: Request) -> bool:
    """Whether a write came from the API's own host, a forwarding proxy's host, or an
    allowed UI origin. Browsers always send `Origin` (or at least `Referer`) on cross-site
    writes, including no-cors form/Blob posts that CORS never preflights; non-browser clients
    (scripts, the test client) send neither and are let through, since without the browser
    there is no ambient cookie to abuse."""
    source = request.headers.get("origin") or request.headers.get("referer")
    if source is None:
        return True
    origin = _origin_of(source)
    if origin is None:  # "null" (sandboxed iframe, data: URL) or garbage
        return False
    host = urlsplit(origin).netloc
    own_hosts = {request.headers.get("host", "").lower()}
    if forwarded := request.headers.get("x-forwarded-host"):
        own_hosts.add(forwarded.split(",")[0].strip().lower())
    allowed = {o.rstrip("/").lower() for o in settings.cors_origins}
    return host in own_hosts or origin in allowed


@app.middleware("http")
async def cross_site_write_guard(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """CSRF defence for the cookie session. SameSite=Lax already blocks cross-site writes when
    the UI is served from the API's origin, but a split deployment needs SameSite=None, and
    then nothing else would stop another site posting to a board endpoint."""
    if (
        request.method in UNSAFE_METHODS
        and "*" not in settings.cors_origins  # development default: no origin restriction
        and not _same_site_write(request)
    ):
        logger.warning(
            "blocked cross-site %s %s from %s",
            request.method,
            request.url.path,
            request.headers.get("origin") or request.headers.get("referer"),
        )
        return JSONResponse(status_code=403, content={"detail": "Cross-site request refused"})
    return await call_next(request)

PUBLIC_CACHE_CONTROL = "public, max-age=60, stale-while-revalidate=300"


@app.middleware("http")
async def public_cache_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    response = await call_next(request)
    if request.method not in ("GET", "HEAD", "OPTIONS") and response.status_code < 400:
        # Anything that changed data invalidates the public pages' in-process cache.
        public_cache.clear()
    elif (
        request.method == "GET"
        and request.url.path.startswith("/api/v1/public")
        and response.status_code == 200
    ):
        response.headers.setdefault("Cache-Control", PUBLIC_CACHE_CONTROL)
    return response


app.include_router(api_router, prefix="/api/v1")


@app.get("/health")
def health(session: SessionDep) -> JSONResponse:
    """Liveness plus a database round trip, so a load balancer stops routing to an instance
    that can't reach Postgres."""
    try:
        session.exec(text("SELECT 1"))  # type: ignore[call-overload]
    except Exception:
        logger.exception("health check: database unreachable")
        return JSONResponse(status_code=503, content={"status": "degraded", "database": "down"})
    return JSONResponse(content={"status": "ok", "database": "ok"})
