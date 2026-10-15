"""FastAPI application entry point."""

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.agent_routes import router as agent_router, skills_router
from app.api.memory_routes import router as memory_router
from app.api.overview_routes import router as overview_router
from app.api.routes import api_router, router
from app.core.config import get_settings
from app.database import initialize_database


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    initialize_database()
    yield


settings = get_settings()
app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)

app.include_router(router)
app.include_router(api_router)
app.include_router(memory_router)
app.include_router(overview_router)
app.include_router(agent_router)
app.include_router(skills_router)


@app.exception_handler(RequestValidationError)
async def safe_validation_error_handler(_, exc: RequestValidationError) -> JSONResponse:
    """Avoid echoing submitted fields in validation errors, which may contain secrets."""
    safe_errors = [
        {key: value for key, value in error.items() if key not in {"input", "ctx"}}
        for error in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": safe_errors})
