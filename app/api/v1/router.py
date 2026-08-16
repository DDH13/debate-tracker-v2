from fastapi import APIRouter

from app.api.v1.endpoints import (
    ballots,
    bp_ballots,
    bp_debates,
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
api_router.include_router(tournaments.router)
api_router.include_router(institutions.router)
api_router.include_router(debaters.router)
api_router.include_router(judges.router)
api_router.include_router(teams.router)
api_router.include_router(rounds.router)
api_router.include_router(motions.router)
api_router.include_router(debates.router)
api_router.include_router(ballots.router)
api_router.include_router(bp_debates.router)
api_router.include_router(bp_ballots.router)
api_router.include_router(stats.router)
