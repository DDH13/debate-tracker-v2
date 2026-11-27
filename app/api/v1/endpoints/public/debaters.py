from fastapi import APIRouter, Query

from app.api.deps import SessionDep, get_or_404
from app.models import Debater
from app.services.public import debater as debater_page
from app.services.public import lists

router = APIRouter(prefix="/debaters", tags=["public"])


# Static before `/{debater_id}`; the list is the root of this router.
@router.get(
    "",
    response_model=lists.DebaterList,
    description=(
        "Paged debater list. `order=desc` means best first for every sort, so `sort=rank` "
        "(lower is better) lists rank 1 first with `desc` and the worst rank first with `asc`."
    ),
)
def list_public_debaters(
    session: SessionDep,
    q: str | None = None,
    institution_id: int | None = None,
    min_rounds: int = Query(default=0, ge=0),
    sort: lists.DebaterSort = "average",
    order: lists.Order = "desc",
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=25, ge=1, le=100),
) -> lists.DebaterList:
    return lists.list_debaters(
        session, q=q, institution_id=institution_id, min_rounds=min_rounds, sort=sort, order=order, offset=offset, limit=limit
    )


@router.get("/{debater_id}", response_model=debater_page.PublicDebater)
def get_public_debater(debater_id: int, session: SessionDep) -> debater_page.PublicDebater:
    return debater_page.build_public_debater(session, get_or_404(session, Debater, debater_id))
