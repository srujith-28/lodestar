"""Workspace-safe guest demo controls."""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.security import WorkspaceContext, require_workspace_access
from app.database import get_db
from app.services.demo_workspace import reset_guest_workspace


router = APIRouter(
    prefix="/api/demo",
    tags=["guest demo"],
    dependencies=[Depends(require_workspace_access)],
)


class DemoResetResponse(BaseModel):
    reset: bool
    sample_data_restored: bool


@router.post("/reset", response_model=DemoResetResponse)
def reset_demo_workspace(
    workspace: WorkspaceContext = Depends(require_workspace_access),
    db: Session = Depends(get_db),
) -> DemoResetResponse:
    if not workspace["is_guest"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Reset is available only inside an isolated guest demo workspace.",
        )
    try:
        reset_guest_workspace(db, workspace["id"])
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The guest workspace could not be reset.",
        ) from None
    return DemoResetResponse(reset=True, sample_data_restored=True)
