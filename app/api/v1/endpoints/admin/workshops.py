from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date

from fastapi import APIRouter, HTTPException, Query, status
from sqlmodel import Session, col

from app.api.crud import ListParams, add_crud_routes
from app.api.deps import SessionDep, get_or_404
from app.models import (
    Institution,
    Person,
    User,
    Workshop,
    WorkshopCreate,
    WorkshopFacilitator,
    WorkshopFacilitatorCreate,
    WorkshopFacilitatorPublic,
    WorkshopFacilitatorUpdate,
    WorkshopPublic,
    WorkshopSchool,
    WorkshopSchoolCreate,
    WorkshopSchoolPublic,
    WorkshopSchoolUpdate,
    WorkshopStatus,
    WorkshopUpdate,
)
from app.models.membership import YEAR_MAX, YEAR_MIN
from app.services import workshops as workshop_service
from app.services.workshops import WorkshopConflict

router = APIRouter()


@contextmanager
def workshop_errors() -> Iterator[None]:
    try:
        yield
    except WorkshopConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


# --- Workshops ------------------------------------------------------------------------


class WorkshopFilters(ListParams):
    custom = frozenset({"year"})

    status: WorkshopStatus | None = None
    year: int | None = None


def _year_filter(statement, params: WorkshopFilters):
    if params.year is None:
        return statement
    return statement.where(
        Workshop.held_on >= date(params.year, 1, 1), Workshop.held_on <= date(params.year, 12, 31)
    )


def _check_workshop(_session: Session, workshop: Workshop, _user: User) -> None:
    if workshop.end_on is not None and workshop.end_on < workshop.held_on:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="end_on must be on or after held_on"
        )


def _guard_workshop_delete(session: Session, workshop: Workshop) -> None:
    with workshop_errors():
        workshop_service.ensure_deletable(session, workshop)


workshops = APIRouter(prefix="/workshops", tags=["admin: workshops"])


# Static paths must come before `/{item_id}`, which the generated routes register.
@workshops.get("/summary", response_model=list[workshop_service.WorkshopSummary])
def summary(
    session: SessionDep, year: int | None = Query(default=None, ge=YEAR_MIN, le=YEAR_MAX)
) -> list[workshop_service.WorkshopSummary]:
    """Every workshop (in `year` when given), newest first: schools, headcount,
    facilitators and money."""
    return workshop_service.workshop_summaries(session, year)


@workshops.get("/{workshop_id}/detail", response_model=workshop_service.WorkshopDetail)
def detail(workshop_id: int, session: SessionDep) -> workshop_service.WorkshopDetail:
    return workshop_service.workshop_detail(session, get_or_404(session, Workshop, workshop_id))


add_crud_routes(
    workshops,
    noun="workshop",
    model=Workshop,
    create_schema=WorkshopCreate,
    public_schema=WorkshopPublic,
    update_schema=WorkshopUpdate,
    filters=WorkshopFilters,
    order_by=(col(Workshop.held_on).desc(),),
    on_save=_check_workshop,
    filter_hook=_year_filter,
    before_delete=_guard_workshop_delete,
)
router.include_router(workshops)

# --- Attending schools and facilitators -----------------------------------------------


class WorkshopChildFilters(ListParams):
    workshop_id: int | None = None


schools = APIRouter(prefix="/workshop-schools", tags=["admin: workshops"])
add_crud_routes(
    schools,
    noun="workshop_school",
    model=WorkshopSchool,
    create_schema=WorkshopSchoolCreate,
    public_schema=WorkshopSchoolPublic,
    update_schema=WorkshopSchoolUpdate,
    filters=WorkshopChildFilters,
    order_by=(WorkshopSchool.workshop_id,),
    parents={"workshop_id": Workshop, "institution_id": Institution},
    conflict_detail="That school is already listed for this workshop",
)
router.include_router(schools)

facilitators = APIRouter(prefix="/workshop-facilitators", tags=["admin: workshops"])
add_crud_routes(
    facilitators,
    noun="workshop_facilitator",
    model=WorkshopFacilitator,
    create_schema=WorkshopFacilitatorCreate,
    public_schema=WorkshopFacilitatorPublic,
    update_schema=WorkshopFacilitatorUpdate,
    filters=WorkshopChildFilters,
    order_by=(WorkshopFacilitator.workshop_id,),
    parents={"workshop_id": Workshop, "person_id": Person},
    conflict_detail="That person is already a facilitator of this workshop",
)
router.include_router(facilitators)
