from fastapi import APIRouter

from app.api.crud import ListParams, add_crud_routes
from app.models import (
    Feedback,
    FeedbackCreate,
    FeedbackPublic,
    FeedbackStatus,
    FeedbackUpdate,
    Institution,
    Judge,
    Tournament,
    Workshop,
)


class FeedbackFilters(ListParams):
    institution_id: int | None = None
    tournament_id: int | None = None
    judge_id: int | None = None
    workshop_id: int | None = None
    status: FeedbackStatus | None = None


router = APIRouter(prefix="/feedback", tags=["admin: feedback"])
add_crud_routes(
    router,
    noun="feedback",
    model=Feedback,
    create_schema=FeedbackCreate,
    public_schema=FeedbackPublic,
    update_schema=FeedbackUpdate,
    filters=FeedbackFilters,
    order_by=(Feedback.created_at.desc(),),
    parents={
        "institution_id": Institution,
        "tournament_id": Tournament,
        "judge_id": Judge,
        "workshop_id": Workshop,
    },
)
