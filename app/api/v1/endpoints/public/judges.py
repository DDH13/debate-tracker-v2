from fastapi import APIRouter, Query

from app.api.deps import SessionDep, get_or_404
from app.models import Judge
from app.services.public import judge as judge_page
from app.services.public import lists

router = APIRouter(prefix="/judges", tags=["public"])


# Static before `/{judge_id}`; the list is the root of this router.
@router.get("", response_model=lists.JudgeList)
def list_public_judges(
    session: SessionDep,
    q: str | None = None,
    institution_id: int | None = None,
    min_debates: int = Query(default=0, ge=0),
    sort: lists.JudgeSort = "debates",
    order: lists.Order = "desc",
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=25, ge=1, le=100),
) -> lists.JudgeList:
    return lists.list_judges(
        session, q=q, institution_id=institution_id, min_debates=min_debates, sort=sort, order=order, offset=offset, limit=limit
    )


@router.get("/{judge_id}", response_model=judge_page.PublicJudge)
def get_public_judge(judge_id: int, session: SessionDep) -> judge_page.PublicJudge:
    return judge_page.build_public_judge(session, get_or_404(session, Judge, judge_id))
