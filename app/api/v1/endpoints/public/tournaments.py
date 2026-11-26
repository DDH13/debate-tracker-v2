from fastapi import APIRouter, HTTPException, status

from app.api.deps import SessionDep
from app.services.public import tournament as tournament_page
from app.services.public.snapshots import tournament_snapshot

router = APIRouter(prefix="/tournaments", tags=["public"])


@router.get("/{tournament_id}", response_model=tournament_page.PublicTournament)
def get_public_tournament(tournament_id: int, session: SessionDep) -> tournament_page.PublicTournament:
    snapshot = tournament_snapshot(session, tournament_id)
    if snapshot is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Tournament {tournament_id} not found")
    return tournament_page.build_public_tournament(session, snapshot)
