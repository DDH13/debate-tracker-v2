from fastapi import APIRouter

from app.api.deps import SessionDep, get_or_404
from app.models import Judge
from app.services.public import judge as judge_page

router = APIRouter(prefix="/judges", tags=["public"])


@router.get("/{judge_id}", response_model=judge_page.PublicJudge)
def get_public_judge(judge_id: int, session: SessionDep) -> judge_page.PublicJudge:
    return judge_page.build_public_judge(session, get_or_404(session, Judge, judge_id))
