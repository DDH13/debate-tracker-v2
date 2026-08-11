from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import func, select

from app.api.deps import SessionDep, get_or_404
from app.models import (
    Debater,
    Institution,
    InstitutionCreate,
    InstitutionPublic,
    InstitutionUpdate,
    Judge,
    Team,
)

router = APIRouter(prefix="/institutions", tags=["institutions"])


@router.post("", response_model=InstitutionPublic, status_code=status.HTTP_201_CREATED)
def create_institution(institution_in: InstitutionCreate, session: SessionDep) -> Institution:
    institution = Institution.model_validate(institution_in)
    session.add(institution)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Institution with this name already exists",
        ) from exc
    session.refresh(institution)
    return institution


@router.get("", response_model=list[InstitutionPublic])
def list_institutions(
    session: SessionDep,
    offset: int = 0,
    limit: int = Query(default=100, le=100),
) -> list[Institution]:
    return session.exec(select(Institution).offset(offset).limit(limit)).all()


@router.get("/{institution_id}", response_model=InstitutionPublic)
def get_institution(institution_id: int, session: SessionDep) -> Institution:
    return get_or_404(session, Institution, institution_id)


@router.patch("/{institution_id}", response_model=InstitutionPublic)
def update_institution(
    institution_id: int, institution_in: InstitutionUpdate, session: SessionDep
) -> Institution:
    institution = get_or_404(session, Institution, institution_id)
    institution.sqlmodel_update(institution_in.model_dump(exclude_unset=True))
    session.add(institution)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Institution with this name already exists",
        ) from exc
    session.refresh(institution)
    return institution


@router.delete("/{institution_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_institution(institution_id: int, session: SessionDep) -> None:
    institution = get_or_404(session, Institution, institution_id)

    team_count = session.exec(
        select(func.count()).select_from(Team).where(Team.institution_id == institution_id)
    ).one()
    debater_count = session.exec(
        select(func.count()).select_from(Debater).where(Debater.institution_id == institution_id)
    ).one()
    judge_count = session.exec(
        select(func.count()).select_from(Judge).where(Judge.institution_id == institution_id)
    ).one()

    if team_count or debater_count or judge_count:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cannot delete institution: referenced by {team_count} team(s), "
                f"{debater_count} debater(s), {judge_count} judge(s)"
            ),
        )

    session.delete(institution)
    session.commit()
