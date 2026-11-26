from typing import Literal

from fastapi import APIRouter, Query
from sqlmodel import col, func, or_, select

from app.api.deps import SessionDep, get_or_404
from app.models import Debater, DebaterAdmin
from app.services import merge

router = APIRouter(prefix="/debaters", tags=["admin: debaters"])


# Static paths before `/{debater_id}`, or the int path parameter claims them. The duplicate
# and merge-info lists moved here from the public /debaters router: they carry personal fields.


@router.get("/duplicates", response_model=list[merge.DuplicateGroup])
def find_duplicate_debaters(
    session: SessionDep, by: Literal["name", "name_birthdate"] = "name"
) -> list[merge.DuplicateGroup]:
    return merge.find_duplicates(session, Debater, by_birthdate=by == "name_birthdate")


@router.get("/merge-info", response_model=list[merge.DebaterMergeInfo])
def debater_merge_info(session: SessionDep) -> list[merge.DebaterMergeInfo]:
    return merge.debater_merge_info(session)


@router.get("", response_model=list[DebaterAdmin])
def list_debater_records(
    session: SessionDep,
    institution_id: int | None = None,
    q: str | None = None,
    offset: int = 0,
    limit: int = Query(default=100, le=100),
) -> list[Debater]:
    """Full debater records, personal fields included."""
    query = select(Debater).order_by(col(Debater.id))
    if institution_id is not None:
        query = query.where(Debater.institution_id == institution_id)
    if q and q.strip():
        first_last = func.trim(
            func.coalesce(Debater.first_name, "") + " " + func.coalesce(Debater.last_name, "")
        )
        query = query.where(
            or_(
                col(Debater.full_name).icontains(q.strip(), autoescape=True),
                first_last.icontains(q.strip(), autoescape=True),
            )
        )
    return list(session.exec(query.offset(offset).limit(limit)).all())


@router.get("/{debater_id}", response_model=DebaterAdmin)
def get_debater_record(debater_id: int, session: SessionDep) -> Debater:
    return get_or_404(session, Debater, debater_id)
