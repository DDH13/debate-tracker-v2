from datetime import date

from fastapi import APIRouter, Query
from sqlmodel import Session, update

from app.api.crud import ListParams, add_crud_routes
from app.api.deps import SessionDep, get_or_404
from app.models import (
    Institution,
    Membership,
    MembershipCreate,
    MembershipPublic,
    MembershipUpdate,
    SchoolAchievement,
    SchoolAchievementCreate,
    SchoolAchievementPublic,
    SchoolAchievementUpdate,
    SchoolContact,
    SchoolContactCreate,
    SchoolContactPublic,
    SchoolContactUpdate,
    SchoolParticipation,
    SchoolParticipationCreate,
    SchoolParticipationPublic,
    SchoolParticipationUpdate,
    Tournament,
)
from app.models.membership import YEAR_MAX, YEAR_MIN
from app.services import schools as school_service

router = APIRouter()

# --- Read views ----------------------------------------------------------------------

views = APIRouter(prefix="/schools", tags=["admin: schools"])

YearQuery = Query(default=None, ge=YEAR_MIN, le=YEAR_MAX, description="Defaults to this year")


@views.get("", response_model=list[school_service.SchoolSummary])
def list_schools(session: SessionDep, year: int | None = YearQuery) -> list[school_service.SchoolSummary]:
    """Every institution with its membership standing for `year`. Unpaginated: the console
    filters client-side."""
    return school_service.school_summaries(session, year or date.today().year)


@views.get("/{institution_id}", response_model=school_service.SchoolDetail)
def get_school(
    institution_id: int, session: SessionDep, year: int | None = YearQuery
) -> school_service.SchoolDetail:
    institution = get_or_404(session, Institution, institution_id)
    return school_service.school_detail(session, institution, year or date.today().year)


router.include_router(views)

# --- Memberships ---------------------------------------------------------------------


class MembershipFilters(ListParams):
    institution_id: int | None = None
    year: int | None = None


memberships = APIRouter(prefix="/memberships", tags=["admin: schools"])
add_crud_routes(
    memberships,
    noun="membership",
    model=Membership,
    create_schema=MembershipCreate,
    public_schema=MembershipPublic,
    update_schema=MembershipUpdate,
    filters=MembershipFilters,
    order_by=(Membership.year.desc(),),
    parents={"institution_id": Institution},
    conflict_detail="This school already has a membership for that year",
)
router.include_router(memberships)

# --- Contacts ------------------------------------------------------------------------


class SchoolContactFilters(ListParams):
    institution_id: int | None = None
    active: bool | None = None


def _keep_one_primary(session: Session, contact: SchoolContact) -> None:
    """A school has at most one primary contact: making one primary demotes the rest."""
    if not contact.is_primary:
        return
    statement = update(SchoolContact).where(SchoolContact.institution_id == contact.institution_id)
    if contact.id is not None:
        statement = statement.where(SchoolContact.id != contact.id)
    session.exec(statement.values(is_primary=False))


contacts = APIRouter(prefix="/school-contacts", tags=["admin: schools"])
add_crud_routes(
    contacts,
    noun="school_contact",
    model=SchoolContact,
    create_schema=SchoolContactCreate,
    public_schema=SchoolContactPublic,
    update_schema=SchoolContactUpdate,
    filters=SchoolContactFilters,
    order_by=(SchoolContact.name,),
    parents={"institution_id": Institution},
    on_save=_keep_one_primary,
)
router.include_router(contacts)

# --- Achievements and manual tournament history ----------------------------------------


class InstitutionFilter(ListParams):
    institution_id: int | None = None


achievements = APIRouter(prefix="/school-achievements", tags=["admin: schools"])
add_crud_routes(
    achievements,
    noun="school_achievement",
    model=SchoolAchievement,
    create_schema=SchoolAchievementCreate,
    public_schema=SchoolAchievementPublic,
    update_schema=SchoolAchievementUpdate,
    filters=InstitutionFilter,
    order_by=(SchoolAchievement.year.desc(),),
    parents={"institution_id": Institution, "tournament_id": Tournament},
)
router.include_router(achievements)

participations = APIRouter(prefix="/school-participations", tags=["admin: schools"])
add_crud_routes(
    participations,
    noun="school_participation",
    model=SchoolParticipation,
    create_schema=SchoolParticipationCreate,
    public_schema=SchoolParticipationPublic,
    update_schema=SchoolParticipationUpdate,
    filters=InstitutionFilter,
    order_by=(SchoolParticipation.year.desc(),),
    parents={"institution_id": Institution},
)
router.include_router(participations)
