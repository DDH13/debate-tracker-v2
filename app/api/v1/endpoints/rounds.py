from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.api.deps import SessionDep, get_or_404
from app.models import Round, RoundCreate, RoundPublic, RoundUpdate, Tournament

router = APIRouter(tags=["rounds"])


@router.post(
    "/tournaments/{tournament_id}/rounds",
    response_model=RoundPublic,
    status_code=status.HTTP_201_CREATED,
)
def create_round(tournament_id: int, round_in: RoundCreate, session: SessionDep) -> Round:
    get_or_404(session, Tournament, tournament_id)
    round_ = Round.model_validate(round_in, update={"tournament_id": tournament_id})
    session.add(round_)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Round with this sequence number already exists in this tournament",
        ) from exc
    session.refresh(round_)
    return round_


@router.get("/tournaments/{tournament_id}/rounds", response_model=list[RoundPublic])
def list_rounds(
    tournament_id: int,
    session: SessionDep,
    offset: int = 0,
    limit: int = Query(default=100, le=100),
) -> list[Round]:
    get_or_404(session, Tournament, tournament_id)
    return session.exec(
        select(Round).where(Round.tournament_id == tournament_id).offset(offset).limit(limit)
    ).all()


@router.get("/rounds/{round_id}", response_model=RoundPublic)
def get_round(round_id: int, session: SessionDep) -> Round:
    return get_or_404(session, Round, round_id)


@router.patch("/rounds/{round_id}", response_model=RoundPublic)
def update_round(round_id: int, round_in: RoundUpdate, session: SessionDep) -> Round:
    round_ = get_or_404(session, Round, round_id)
    round_.sqlmodel_update(round_in.model_dump(exclude_unset=True))
    session.add(round_)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Round with this sequence number already exists in this tournament",
        ) from exc
    session.refresh(round_)
    return round_


@router.delete("/rounds/{round_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_round(round_id: int, session: SessionDep) -> None:
    round_ = get_or_404(session, Round, round_id)
    session.delete(round_)
    session.commit()
