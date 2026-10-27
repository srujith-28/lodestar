"""FastAPI application entry point."""

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.agent_routes import router as agent_router, skills_router
from app.api.auth_routes import router as auth_router
from app.api.memory_routes import router as memory_router
from app.api.overview_routes import router as overview_router
from app.api.routes import api_router, router
from app.core.config import REPOSITORY_ROOT, Settings, get_settings
from app.database import initialize_database


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    initialize_database()
    yield


def create_app(settings: Settings) -> FastAPI:
    """Build the app with development docs only outside production."""
    production = settings.app_env == "production"
    application = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None if production else "/docs",
        redoc_url=None if production else "/redoc",
        openapi_url=None if production else "/openapi.json",
    )

    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Content-Type", "X-CSRF-Token"],
    )

    application.include_router(router)
    application.include_router(auth_router)
    application.include_router(api_router)
    application.include_router(memory_router)
    application.include_router(overview_router)
    application.include_router(agent_router)
    application.include_router(skills_router)

    # A single Render web service builds Vite once and serves the resulting SPA
    # from FastAPI. API routes are registered first so they retain precedence.
    frontend_dist = REPOSITORY_ROOT / "frontend" / "dist"
    if frontend_dist.is_dir():
        application.mount(
            "/",
            StaticFiles(directory=frontend_dist, html=True),
            name="frontend",
        )

    @application.middleware("http")
    async def prevent_api_caching(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @application.exception_handler(RequestValidationError)
    async def safe_validation_error_handler(_, exc: RequestValidationError) -> JSONResponse:
        """Avoid echoing submitted fields in validation errors, which may contain secrets."""
        safe_errors = [
            {key: value for key, value in error.items() if key not in {"input", "ctx"}}
            for error in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": safe_errors})

    return application


settings = get_settings()
app = create_app(settings)
