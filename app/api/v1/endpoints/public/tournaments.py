from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import SessionDep
from app.services.public import lists
from app.services.public import tournament as tournament_page
from app.services.public.snapshots import tournament_snapshot

router = APIRouter(prefix="/tournaments", tags=["public"])


# Static before `/{tournament_id}`; the list is the root of this router.
@router.get("", response_model=lists.TournamentList)
def list_public_tournaments(
    session: SessionDep,
    q: str | None = None,
    sort: lists.TournamentSort = "date",
    order: lists.Order = "desc",
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=25, ge=1, le=100),
) -> lists.TournamentList:
    return lists.list_tournaments(session, q=q, sort=sort, order=order, offset=offset, limit=limit)


@router.get("/{tournament_id}", response_model=tournament_page.PublicTournament)
def get_public_tournament(tournament_id: int, session: SessionDep) -> tournament_page.PublicTournament:
    snapshot = tournament_snapshot(session, tournament_id)
    if snapshot is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Tournament {tournament_id} not found")
    return tournament_page.build_public_tournament(session, snapshot)
