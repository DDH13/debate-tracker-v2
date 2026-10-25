from fastapi import APIRouter, HTTPException, status
from sqlmodel import Session, col

from app.api.crud import ListParams, add_crud_routes
from app.api.deps import SessionDep, get_or_404
from app.models import (
    Debater,
    Person,
    Squad,
    SquadCreate,
    SquadKind,
    SquadMember,
    SquadMemberCreate,
    SquadMemberPublic,
    SquadMemberUpdate,
    SquadPublic,
    SquadStaff,
    SquadStaffCreate,
    SquadStaffPublic,
    SquadStaffUpdate,
    SquadUpdate,
    User,
)
from app.services import squads as squad_service

router = APIRouter()

# --- Squads ---------------------------------------------------------------------------


class SquadFilters(ListParams):
    kind: SquadKind | None = None
    season: str | None = None


squads = APIRouter(prefix="/squads", tags=["admin: squads"])


@squads.get("/{squad_id}/roster", response_model=squad_service.SquadRoster)
def squad_roster(squad_id: int, session: SessionDep) -> squad_service.SquadRoster:
    """The squad with its members (debater and school names) and staff, in one call."""
    return squad_service.squad_roster(session, get_or_404(session, Squad, squad_id))


add_crud_routes(
    squads,
    noun="squad",
    model=Squad,
    create_schema=SquadCreate,
    public_schema=SquadPublic,
    update_schema=SquadUpdate,
    filters=SquadFilters,
    order_by=(col(Squad.season).desc(),),
    conflict_detail="A squad for that kind and season already exists",
)
router.include_router(squads)

# --- Members --------------------------------------------------------------------------


class SquadMemberFilters(ListParams):
    squad_id: int | None = None
    debater_id: int | None = None


def _check_member_dates(_session: Session, member: SquadMember, _user: User) -> None:
    if member.joined_on and member.left_on and member.left_on < member.joined_on:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="left_on must be on or after joined_on"
        )


members = APIRouter(prefix="/squad-members", tags=["admin: squads"])
add_crud_routes(
    members,
    noun="squad_member",
    model=SquadMember,
    create_schema=SquadMemberCreate,
    public_schema=SquadMemberPublic,
    update_schema=SquadMemberUpdate,
    filters=SquadMemberFilters,
    order_by=(SquadMember.squad_id,),
    parents={"squad_id": Squad, "debater_id": Debater},
    conflict_detail="That debater is already on this squad",
    on_save=_check_member_dates,
)
router.include_router(members)

# --- Staff ----------------------------------------------------------------------------


class SquadStaffFilters(ListParams):
    squad_id: int | None = None


staff = APIRouter(prefix="/squad-staff", tags=["admin: squads"])
add_crud_routes(
    staff,
    noun="squad_staff",
    model=SquadStaff,
    create_schema=SquadStaffCreate,
    public_schema=SquadStaffPublic,
    update_schema=SquadStaffUpdate,
    filters=SquadStaffFilters,
    order_by=(SquadStaff.squad_id, SquadStaff.name),
    parents={"squad_id": Squad, "person_id": Person},
)
router.include_router(staff)
