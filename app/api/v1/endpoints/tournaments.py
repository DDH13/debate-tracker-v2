from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.api.deps import SessionDep, get_or_404
from app.models import Tournament, TournamentCreate, TournamentPublic, TournamentUpdate

router = APIRouter(prefix="/tournaments", tags=["tournaments"])


@router.post("", response_model=TournamentPublic, status_code=status.HTTP_201_CREATED)
def create_tournament(tournament_in: TournamentCreate, session: SessionDep) -> Tournament:
    tournament = Tournament.model_validate(tournament_in)
    session.add(tournament)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tournament with this slug already exists",
        ) from exc
    session.refresh(tournament)
    return tournament


@router.get("", response_model=list[TournamentPublic])
def list_tournaments(
    session: SessionDep,
    offset: int = 0,
    limit: int = Query(default=100, le=100),
) -> list[Tournament]:
    return session.exec(select(Tournament).offset(offset).limit(limit)).all()


@router.get("/{tournament_id}", response_model=TournamentPublic)
def get_tournament(tournament_id: int, session: SessionDep) -> Tournament:
    return get_or_404(session, Tournament, tournament_id)


@router.patch("/{tournament_id}", response_model=TournamentPublic)
def update_tournament(
    tournament_id: int, tournament_in: TournamentUpdate, session: SessionDep
) -> Tournament:
    tournament = get_or_404(session, Tournament, tournament_id)
    tournament.sqlmodel_update(tournament_in.model_dump(exclude_unset=True))
    session.add(tournament)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tournament with this slug already exists",
        ) from exc
    session.refresh(tournament)
    return tournament


@router.delete("/{tournament_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_tournament(tournament_id: int, session: SessionDep) -> None:
    tournament = get_or_404(session, Tournament, tournament_id)
    session.delete(tournament)
    session.commit()
