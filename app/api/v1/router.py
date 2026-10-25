from fastapi import APIRouter, Depends

from app.api.deps import require_board, require_board_for_writes
from app.api.v1.endpoints import (
    admin,
    auth,
    ballots,
    debaters,
    debates,
    institutions,
    judges,
    motions,
    rounds,
    stats,
    teams,
    tournaments,
)

api_router = APIRouter()
api_router.include_router(auth.router)

# Public reads; every write needs a board user.
for public_router in (
    tournaments.router,
    institutions.router,
    debaters.router,
    judges.router,
    teams.router,
    rounds.router,
    motions.router,
    debates.router,
    ballots.router,
    stats.router,
):
    api_router.include_router(public_router, dependencies=[Depends(require_board_for_writes)])

api_router.include_router(admin.router, dependencies=[Depends(require_board)])
