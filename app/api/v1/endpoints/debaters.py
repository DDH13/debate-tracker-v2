from typing import Literal

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import SQLModel, col, func, or_, select

from app.api.deps import SessionDep, get_or_404, merge_errors
from app.models import (
    Debater,
    DebaterCreate,
    DebaterProfile,
    DebaterPublic,
    DebaterUpdate,
    Institution,
    SpeakerScore,
    SquadMember,
)
from app.services import merge

router = APIRouter(prefix="/debaters", tags=["debaters"])


class DebaterMergeRequest(SQLModel):
    source_id: int
    target_id: int


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
    q: str | None = None,
    offset: int = 0,
    limit: int = Query(default=100, le=100),
) -> list[Debater]:
    query = select(Debater)
    if institution_id is not None:
        query = query.where(Debater.institution_id == institution_id)
    if q and q.strip():
        # Case-insensitive "name contains": the full name, or first + last when that's all
        # there is. Lets a picker search thousands of debaters server-side.
        first_last = func.trim(
            func.coalesce(Debater.first_name, "") + " " + func.coalesce(Debater.last_name, "")
        )
        query = query.where(
            or_(
                col(Debater.full_name).icontains(q.strip(), autoescape=True),
                first_last.icontains(q.strip(), autoescape=True),
            )
        )
    return session.exec(query.offset(offset).limit(limit)).all()


# Static paths must be declared before `/{debater_id}`, or the int path parameter claims them.


@router.post("/merge", response_model=DebaterPublic)
def merge_debaters(merge_in: DebaterMergeRequest, session: SessionDep) -> Debater:
    """Fold `source_id` into `target_id`: scores, team and squad memberships move across, null
    fields on the target are filled from the source, and the source is deleted."""
    with merge_errors():
        return merge.merge_debaters(session, merge_in.source_id, merge_in.target_id)


@router.get("/duplicates", response_model=list[merge.DuplicateGroup])
def find_duplicate_debaters(
    session: SessionDep, by: Literal["name", "name_birthdate"] = "name"
) -> list[merge.DuplicateGroup]:
    return merge.find_duplicates(session, Debater, by_birthdate=by == "name_birthdate")


@router.get("/merge-info", response_model=list[merge.DebaterMergeInfo])
def debater_merge_info(session: SessionDep) -> list[merge.DebaterMergeInfo]:
    return merge.debater_merge_info(session)


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

    squad_count = session.exec(
        select(func.count()).select_from(SquadMember).where(SquadMember.debater_id == debater_id)
    ).one()
    if squad_count:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot delete debater: on {squad_count} squad(s)",
        )

    score_count = session.exec(
        select(func.count()).select_from(SpeakerScore).where(SpeakerScore.debater_id == debater_id)
    ).one()
    if score_count:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot delete debater: referenced by {score_count} speaker score(s)",
        )

    # The profile FK has no cascade, so a refreshed debater would otherwise 500 here.
    for profile in session.exec(
        select(DebaterProfile).where(DebaterProfile.debater_id == debater_id)
    ).all():
        session.delete(profile)
    session.delete(debater)
    session.commit()
