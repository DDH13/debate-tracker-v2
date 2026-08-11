from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.api.deps import SessionDep, get_or_404
from app.models import Institution, Judge, JudgeCreate, JudgePublic, JudgeUpdate

router = APIRouter(prefix="/judges", tags=["judges"])


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
    session.delete(judge)
    session.commit()
