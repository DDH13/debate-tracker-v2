from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, TypeVar

from fastapi import Depends, HTTPException, status
from sqlmodel import Session, SQLModel

from app.db.session import get_session
from app.services.merge import MergeConflict, MergeInvalid, MergeNotFound

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


@contextmanager
def merge_errors() -> Iterator[None]:
    """Translate the merge service's errors into 404 / 422 / 409 responses."""
    try:
        yield
    except MergeNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except MergeInvalid as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except MergeConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
