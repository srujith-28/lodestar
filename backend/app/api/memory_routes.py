"""Local CRUD and keyword search endpoints for user-controlled memories."""

from collections.abc import Callable
from typing import TypeVar

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.memory_schemas import MemoryCreate, MemoryResponse, MemoryUpdate
from app.database import get_db
from app.models import Memory, utc_now
from app.services.memory_search import search_memory_rows


router = APIRouter(prefix="/api/memories", tags=["memory"])
Result = TypeVar("Result")


def _database_operation(db: Session, operation: Callable[[Session], Result]) -> Result:
    """Map database failures to a safe response without exposing local details."""
    try:
        return operation(db)
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Local memory storage is unavailable.",
        ) from None


def _memory_or_404(db: Session, memory_id: int) -> Memory:
    memory = db.get(Memory, memory_id)
    if memory is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Memory not found.",
        )
    return memory


def _memory_sort():
    return (Memory.updated_at.desc(), Memory.id.desc())


@router.get("/search", response_model=list[MemoryResponse])
def search_memories(
    q: str = Query(min_length=1, max_length=100),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[MemoryResponse]:
    """Search content, category, or source locally; no external service is used."""
    terms = q.strip().split()
    if not terms:
        raise HTTPException(status_code=422, detail="Search query cannot be blank.")
    if len(terms) > 10:
        raise HTTPException(status_code=422, detail="Search accepts at most 10 keywords.")

    memories = _database_operation(db, lambda session: search_memory_rows(session, q, limit))
    return [MemoryResponse.model_validate(memory) for memory in memories]


@router.get("", response_model=list[MemoryResponse])
def list_memories(
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[MemoryResponse]:
    statement = select(Memory).order_by(*_memory_sort()).offset(offset).limit(limit)
    memories = _database_operation(db, lambda session: session.scalars(statement).all())
    return [MemoryResponse.model_validate(memory) for memory in memories]


@router.post("", response_model=MemoryResponse, status_code=status.HTTP_201_CREATED)
def create_memory(
    request: MemoryCreate,
    db: Session = Depends(get_db),
) -> MemoryResponse:
    def save(session: Session) -> Memory:
        memory = Memory(**request.model_dump())
        session.add(memory)
        session.commit()
        session.refresh(memory)
        return memory

    memory = _database_operation(db, save)
    return MemoryResponse.model_validate(memory)


@router.get("/{memory_id}", response_model=MemoryResponse)
def get_memory(
    memory_id: int = Path(gt=0),
    db: Session = Depends(get_db),
) -> MemoryResponse:
    memory = _database_operation(db, lambda session: _memory_or_404(session, memory_id))
    return MemoryResponse.model_validate(memory)


@router.put("/{memory_id}", response_model=MemoryResponse)
def update_memory(
    request: MemoryUpdate,
    memory_id: int = Path(gt=0),
    db: Session = Depends(get_db),
) -> MemoryResponse:
    def update(session: Session) -> Memory:
        memory = _memory_or_404(session, memory_id)
        for field, value in request.model_dump().items():
            setattr(memory, field, value)
        memory.updated_at = utc_now()
        session.commit()
        session.refresh(memory)
        return memory

    memory = _database_operation(db, update)
    return MemoryResponse.model_validate(memory)


@router.delete(
    "/{memory_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def delete_memory(
    memory_id: int = Path(gt=0),
    db: Session = Depends(get_db),
) -> Response:
    def delete(session: Session) -> None:
        memory = _memory_or_404(session, memory_id)
        session.delete(memory)
        session.commit()

    _database_operation(db, delete)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
