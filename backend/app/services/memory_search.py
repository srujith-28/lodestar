"""Shared local keyword search used by the memory API and agent tools."""

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.models import Memory


def search_memory_rows(
    db: Session, query: str, limit: int, *, workspace_id: str = "personal"
) -> list[Memory]:
    """Return memories containing every query term in content, category, or source."""
    term_conditions = []
    for term in query.strip().split():
        escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        term_conditions.append(
            or_(
                Memory.content.ilike(pattern, escape="\\"),
                Memory.category.ilike(pattern, escape="\\"),
                Memory.source.ilike(pattern, escape="\\"),
            )
        )

    statement = (
        select(Memory)
        .where(Memory.workspace_id == workspace_id, and_(*term_conditions))
        .order_by(Memory.updated_at.desc(), Memory.id.desc())
        .limit(limit)
    )
    return list(db.scalars(statement).all())
