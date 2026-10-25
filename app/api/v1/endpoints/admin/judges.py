from fastapi import APIRouter, HTTPException, status
from sqlmodel import Session

from app.api.crud import ListParams, add_crud_routes
from app.api.deps import SessionDep, get_or_404
from app.models import (
    Institution,
    Judge,
    JudgeAffiliation,
    JudgeAffiliationCreate,
    JudgeAffiliationPublic,
    JudgeAffiliationUpdate,
    JudgeLevel,
    JudgeRecordPublic,
    JudgeRecordUpdate,
    User,
)
from app.services import judges as judge_service

router = APIRouter()

views = APIRouter(prefix="/judges", tags=["admin: judges"])


# Static before the `/{judge_id}/...` routes.
@views.get("/registry", response_model=list[judge_service.JudgeRegistryRow])
def registry(
    session: SessionDep,
    q: str | None = None,
    level: JudgeLevel | None = None,
    institution_id: int | None = None,
) -> list[judge_service.JudgeRegistryRow]:
    """Every judge with their level, affiliations and involvement (from the imported
    panels), most recently active first. Unpaginated."""
    return judge_service.judge_registry(session, q, level, institution_id)


@views.get("/{judge_id}/detail", response_model=judge_service.JudgeDetail)
def detail(judge_id: int, session: SessionDep) -> judge_service.JudgeDetail:
    return judge_service.judge_detail(session, get_or_404(session, Judge, judge_id))


@views.put("/{judge_id}/record", response_model=JudgeRecordPublic)
def update_record(judge_id: int, body: JudgeRecordUpdate, session: SessionDep):
    """Sets the board's level and notes for a judge (created on first save)."""
    return judge_service.upsert_record(session, get_or_404(session, Judge, judge_id), body)


router.include_router(views)


class AffiliationFilters(ListParams):
    judge_id: int | None = None
    institution_id: int | None = None


def _check_years(_session: Session, affiliation: JudgeAffiliation, _user: User) -> None:
    if affiliation.from_year and affiliation.to_year and affiliation.to_year < affiliation.from_year:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="to_year must not be before from_year"
        )


affiliations = APIRouter(prefix="/judge-affiliations", tags=["admin: judges"])
add_crud_routes(
    affiliations,
    noun="judge_affiliation",
    model=JudgeAffiliation,
    create_schema=JudgeAffiliationCreate,
    public_schema=JudgeAffiliationPublic,
    update_schema=JudgeAffiliationUpdate,
    filters=AffiliationFilters,
    order_by=(JudgeAffiliation.judge_id,),
    parents={"judge_id": Judge, "institution_id": Institution},
    on_save=_check_years,
)
router.include_router(affiliations)
