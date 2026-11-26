from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import Session

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.logging import configure_logging
from app.db.seed import seed
from app.db.session import engine, init_db
from app.services.finance import ensure_default_categories
from app.services.public import cache as public_cache

configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.init_db_on_startup:
        init_db()
        with Session(engine) as session:
            ensure_default_categories(session)
    if settings.seed_on_startup:
        with Session(engine) as session:
            seed(session)
    yield


app = FastAPI(title=settings.app_name, version=settings.version, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

PUBLIC_CACHE_CONTROL = "public, max-age=60, stale-while-revalidate=300"


@app.middleware("http")
async def public_cache_middleware(request: Request, call_next):
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
def health() -> dict[str, str]:
    return {"status": "ok"}
