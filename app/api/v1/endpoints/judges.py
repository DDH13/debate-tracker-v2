from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import SQLModel, func, select

from app.api.deps import SessionDep, get_or_404, merge_errors
from app.models import (
    Ballot,
    DebateJudge,
    Institution,
    Judge,
    JudgeCreate,
    JudgeProfile,
    JudgePublic,
    JudgeUpdate,
)
from app.services import merge

router = APIRouter(prefix="/judges", tags=["judges"])


class JudgeMergeRequest(SQLModel):
    source_id: int
    target_id: int


@router.post("", response_model=JudgePublic, status_code=status.HTTP_201_CREATED)
def create_judge(judge_in: JudgeCreate, session: SessionDep) -> Judge:
    if judge_in.institution_id is not None:
        get_or_404(session, Institution, judge_in.institution_id)
    judge = Judge.model_validate(judge_in)
    session.add(judge)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Judge already exists"
        ) from exc
    session.refresh(judge)
    return judge


@router.get("", response_model=list[JudgePublic])
def list_judges(
    session: SessionDep,
    institution_id: int | None = None,
    offset: int = 0,
    limit: int = Query(default=100, le=100),
) -> list[Judge]:
    query = select(Judge)
    if institution_id is not None:
        query = query.where(Judge.institution_id == institution_id)
    return session.exec(query.offset(offset).limit(limit)).all()


# Static paths must be declared before `/{judge_id}`, or the int path parameter claims them.


@router.post("/merge", response_model=JudgePublic)
def merge_judges(merge_in: JudgeMergeRequest, session: SessionDep) -> Judge:
    """Fold `source_id` into `target_id`: ballots and panel links move across, null fields
    on the target are filled from the source, and the source is deleted."""
    with merge_errors():
        return merge.merge_judges(session, merge_in.source_id, merge_in.target_id)


@router.get("/duplicates", response_model=list[merge.DuplicateGroup])
def find_duplicate_judges(session: SessionDep) -> list[merge.DuplicateGroup]:
    return merge.find_duplicates(session, Judge)


@router.get("/merge-info", response_model=list[merge.JudgeMergeInfo])
def judge_merge_info(session: SessionDep) -> list[merge.JudgeMergeInfo]:
    return merge.judge_merge_info(session)


@router.get("/{judge_id}", response_model=JudgePublic)
def get_judge(judge_id: int, session: SessionDep) -> Judge:
    return get_or_404(session, Judge, judge_id)


@router.patch("/{judge_id}", response_model=JudgePublic)
def update_judge(judge_id: int, judge_in: JudgeUpdate, session: SessionDep) -> Judge:
    judge = get_or_404(session, Judge, judge_id)
    update_data = judge_in.model_dump(exclude_unset=True)
    if update_data.get("institution_id") is not None:
        get_or_404(session, Institution, update_data["institution_id"])
    judge.sqlmodel_update(update_data)
    session.add(judge)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Judge already exists"
        ) from exc
    session.refresh(judge)
    return judge


@router.delete("/{judge_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_judge(judge_id: int, session: SessionDep) -> None:
    judge = get_or_404(session, Judge, judge_id)

    ballot_count = session.exec(
        select(func.count()).select_from(Ballot).where(Ballot.judge_id == judge_id)
    ).one()
    panel_count = session.exec(
        select(func.count()).select_from(DebateJudge).where(DebateJudge.judge_id == judge_id)
    ).one()
    if ballot_count or panel_count:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cannot delete judge: referenced by {ballot_count} ballot(s), "
                f"{panel_count} panel assignment(s)"
            ),
        )

    # The profile FK has no cascade, so a refreshed judge would otherwise 500 here.
    for profile in session.exec(select(JudgeProfile).where(JudgeProfile.judge_id == judge_id)).all():
        session.delete(profile)
    session.delete(judge)
    session.commit()
