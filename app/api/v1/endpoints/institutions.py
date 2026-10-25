from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import SQLModel, func, select

from app.api.deps import SessionDep, get_or_404, merge_errors
from app.models import (
    Debater,
    Feedback,
    Institution,
    InstitutionCreate,
    InstitutionPublic,
    InstitutionUpdate,
    Judge,
    Membership,
    SchoolAchievement,
    SchoolContact,
    SchoolParticipation,
    Team,
    WorkshopSchool,
)
from app.services import institutions as institution_service
from app.services import merge

router = APIRouter(prefix="/institutions", tags=["institutions"])


class InstitutionMergeRequest(SQLModel):
    target_id: int
    source_ids: list[int]


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


# Static paths must be declared before `/{institution_id}`, or the int path parameter claims them.


@router.post("/merge", response_model=InstitutionPublic)
def merge_institutions(merge_in: InstitutionMergeRequest, session: SessionDep) -> Institution:
    """Fold every institution in `source_ids` into `target_id`, reassigning their teams,
    debaters and judges, then delete the sources."""
    with merge_errors():
        return merge.merge_institutions(session, merge_in.target_id, merge_in.source_ids)


@router.get("/similar", response_model=list[institution_service.SimilarInstitution])
def similar_institutions(
    session: SessionDep, name: str, threshold: float = Query(default=0.8, ge=0, le=1)
) -> list[institution_service.SimilarInstitution]:
    return institution_service.similar_institutions(session, name, threshold)


@router.get("/merge-info", response_model=list[merge.InstitutionMergeInfo])
def institution_merge_info(session: SessionDep) -> list[merge.InstitutionMergeInfo]:
    return merge.institution_merge_info(session)


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

    # Outreach leads aren't listed: their link is ON DELETE SET NULL and they keep the
    # school name, so they survive the institution going away.
    references = {
        "team(s)": Team,
        "debater(s)": Debater,
        "judge(s)": Judge,
        "membership(s)": Membership,
        "contact(s)": SchoolContact,
        "achievement(s)": SchoolAchievement,
        "participation record(s)": SchoolParticipation,
        "feedback item(s)": Feedback,
        "workshop attendance(s)": WorkshopSchool,
    }
    counts = {
        label: session.exec(
            select(func.count()).select_from(model).where(model.institution_id == institution_id)
        ).one()
        for label, model in references.items()
    }
    in_use = [f"{n} {label}" for label, n in counts.items() if n]
    if in_use:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot delete institution: referenced by {', '.join(in_use)}",
        )

    session.delete(institution)
    session.commit()
