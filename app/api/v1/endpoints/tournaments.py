import httpx
from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import SQLModel, select

from app.api.deps import SessionDep, get_or_404
from app.models import Tournament, TournamentCreate, TournamentPublic, TournamentUpdate
from app.services.tabbycat import (
    ImportReport,
    TabbycatImportError,
    TournamentAlreadyExists,
    import_tournament,
)

router = APIRouter(prefix="/tournaments", tags=["tournaments"])


class TabbycatImportRequest(SQLModel):
    base_url: str
    slug: str
    api_key: str | None = None
    include_ballots: bool = True


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


@router.post("/import", response_model=ImportReport, status_code=status.HTTP_201_CREATED)
def import_tabbycat_tournament(import_in: TabbycatImportRequest, session: SessionDep) -> ImportReport:
    """Import a tournament from a Tabbycat instance.

    Fetches the tournament, institutions, teams, adjudicators, rounds and (unless
    `include_ballots=False`) ballots from the Tabbycat REST API at `base_url` and
    creates the equivalent rows locally. This is a synchronous, blocking call: with
    `include_ballots=True` it makes `1 + rounds + debates` upstream requests and can
    take a while for a large tournament.
    """
    try:
        return import_tournament(
            import_in.base_url,
            import_in.slug,
            import_in.api_key,
            session=session,
            include_ballots=import_in.include_ballots,
        )
    except TournamentAlreadyExists as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except TabbycatImportError as exc:
        if exc.status_code == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="tournament not found on Tabbycat instance",
            ) from exc
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    except httpx.TransportError as exc:
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=str(exc)) from exc
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Import conflicts with existing data"
        ) from exc


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
