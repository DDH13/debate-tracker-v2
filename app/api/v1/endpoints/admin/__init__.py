from fastapi import APIRouter

from app.api.v1.endpoints.admin import feedback, outreach, schools, users

# Board-only management console. The require_board guard is attached where this router is
# included (app/api/v1/router.py), so every sub-router here inherits it.
router = APIRouter(prefix="/admin")
router.include_router(users.router)
router.include_router(schools.router)
router.include_router(feedback.router)
router.include_router(outreach.router)
