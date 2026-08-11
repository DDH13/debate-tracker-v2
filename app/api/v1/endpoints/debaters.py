from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import func, select

from app.api.deps import SessionDep, get_or_404
from app.models import (
    Debater,
    DebaterCreate,
    DebaterPublic,
    DebaterUpdate,
    Institution,
    SpeakerScore,
)

router = APIRouter(prefix="/debaters", tags=["debaters"])


@router.post("", response_model=DebaterPublic, status_code=status.HTTP_201_CREATED)
def create_debater(debater_in: DebaterCreate, session: SessionDep) -> Debater:
    if debater_in.institution_id is not None:
        get_or_404(session, Institution, debater_in.institution_id)
    debater = Debater.model_validate(debater_in)
    session.add(debater)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Debater already exists"
        ) from exc
    session.refresh(debater)
    return debater


@router.get("", response_model=list[DebaterPublic])
def list_debaters(
    session: SessionDep,
    institution_id: int | None = None,
    offset: int = 0,
    limit: int = Query(default=100, le=100),
) -> list[Debater]:
    query = select(Debater)
    if institution_id is not None:
        query = query.where(Debater.institution_id == institution_id)
    return session.exec(query.offset(offset).limit(limit)).all()


@router.get("/{debater_id}", response_model=DebaterPublic)
def get_debater(debater_id: int, session: SessionDep) -> Debater:
    return get_or_404(session, Debater, debater_id)


@router.patch("/{debater_id}", response_model=DebaterPublic)
def update_debater(debater_id: int, debater_in: DebaterUpdate, session: SessionDep) -> Debater:
    debater = get_or_404(session, Debater, debater_id)
    update_data = debater_in.model_dump(exclude_unset=True)
    if update_data.get("institution_id") is not None:
        get_or_404(session, Institution, update_data["institution_id"])
    debater.sqlmodel_update(update_data)
    session.add(debater)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Debater already exists"
        ) from exc
    session.refresh(debater)
    return debater


@router.delete("/{debater_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_debater(debater_id: int, session: SessionDep) -> None:
    debater = get_or_404(session, Debater, debater_id)

    score_count = session.exec(
        select(func.count()).select_from(SpeakerScore).where(SpeakerScore.debater_id == debater_id)
    ).one()
    if score_count:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot delete debater: referenced by {score_count} speaker score(s)",
        )

    session.delete(debater)
    session.commit()
