from fastapi import APIRouter

from app.api.v1.endpoints.admin import (
    board,
    feedback,
    finance,
    judges,
    outreach,
    people,
    schools,
    squads,
    tasks,
    tournament_ops,
    trips,
    users,
    workshops,
)

# Board-only management console. The require_board guard is attached where this router is
# included (app/api/v1/router.py), so every sub-router here inherits it.
router = APIRouter(prefix="/admin")
router.include_router(users.router)
router.include_router(schools.router)
router.include_router(feedback.router)
router.include_router(outreach.router)
router.include_router(people.router)
router.include_router(finance.router)
router.include_router(squads.router)
router.include_router(trips.router)
router.include_router(board.router)
router.include_router(tasks.router)
router.include_router(workshops.router)
router.include_router(tournament_ops.router)
router.include_router(judges.router)
