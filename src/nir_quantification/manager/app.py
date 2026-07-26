from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api import create_router
from .config import ManagerSettings
from .db import create_session_factory, create_sqlite_engine, ensure_runtime_indexes, ensure_runtime_schema, init_database
from .jobs import JobManager
from .parsers import ParserRegistry


def create_app(settings: ManagerSettings | None = None) -> FastAPI:
    settings = settings or ManagerSettings.from_env()
    engine = create_sqlite_engine(settings)
    init_database(engine)
    ensure_runtime_schema(engine)
    ensure_runtime_indexes(engine)
    session_factory = create_session_factory(engine)
    parser_registry = ParserRegistry()
    job_manager = JobManager(settings=settings, session_factory=session_factory, parser_registry=parser_registry)
    job_manager.ensure_class_stats()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            job_manager.shutdown(wait=True)

    app = FastAPI(title="NIR Spectrum Manager", lifespan=lifespan)
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.job_manager = job_manager
    app.include_router(create_router(settings, session_factory, job_manager))

    if settings.static_dir is not None and settings.static_dir.exists():
        assets_dir = settings.static_dir / "assets"
        if assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

        @app.get("/", include_in_schema=False)
        @app.get("/{full_path:path}", include_in_schema=False)
        def spa_entry(full_path: str = ""):
            target = settings.static_dir / full_path
            if full_path and target.exists() and target.is_file():
                return FileResponse(target)
            return FileResponse(settings.static_dir / "index.html")

    return app
