from typing import Annotated, TypeVar

from fastapi import Depends, HTTPException, status
from sqlmodel import Session, SQLModel

from app.db.session import get_session
from app.models import DebateFormat, Tournament

SessionDep = Annotated[Session, Depends(get_session)]

ModelType = TypeVar("ModelType", bound=SQLModel)


def get_or_404(session: Session, model: type[ModelType], id: int) -> ModelType:
    obj = session.get(model, id)
    if obj is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{model.__name__} {id} not found",
        )
    return obj


def require_format(tournament: Tournament, expected: DebateFormat) -> None:
    if tournament.format != expected:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Tournament {tournament.id} is {tournament.format.value}, not {expected.value}",
        )
