from fastapi import APIRouter

from app.api.crud import ListParams, add_crud_routes
from app.api.deps import SessionDep, get_or_404
from app.models import (
    Institution,
    Tournament,
    TournamentOpsUpdate,
    TournamentRegistration,
    TournamentRegistrationCreate,
    TournamentRegistrationPublic,
    TournamentRegistrationUpdate,
)
from app.services import tournament_ops as ops_service

router = APIRouter()

views = APIRouter(tags=["admin: tournament ops"])


@views.get("/tournament-ops", response_model=list[ops_service.OpsSummaryRow])
def ops_summary(session: SessionDep) -> list[ops_service.OpsSummaryRow]:
    """Every tournament, newest first, with its dropout and judge shortfall totals."""
    return ops_service.ops_summary(session)


@views.get("/tournaments/{tournament_id}/ops", response_model=ops_service.TournamentOpsView)
def tournament_ops(tournament_id: int, session: SessionDep) -> ops_service.TournamentOpsView:
    """One row per school (registered or with imported teams): registered vs fielded teams,
    judges required by the quota rule vs provided, with overrides marked."""
    return ops_service.tournament_ops(session, get_or_404(session, Tournament, tournament_id))


@views.put("/tournaments/{tournament_id}/ops-settings", response_model=ops_service.OpsSettings)
def update_settings(
    tournament_id: int, body: TournamentOpsUpdate, session: SessionDep
) -> ops_service.OpsSettings:
    return ops_service.upsert_settings(session, get_or_404(session, Tournament, tournament_id), body)


router.include_router(views)


class RegistrationFilters(ListParams):
    tournament_id: int | None = None
    institution_id: int | None = None


registrations = APIRouter(prefix="/tournament-registrations", tags=["admin: tournament ops"])
add_crud_routes(
    registrations,
    noun="tournament_registration",
    model=TournamentRegistration,
    create_schema=TournamentRegistrationCreate,
    public_schema=TournamentRegistrationPublic,
    update_schema=TournamentRegistrationUpdate,
    filters=RegistrationFilters,
    order_by=(TournamentRegistration.tournament_id,),
    parents={"tournament_id": Tournament, "institution_id": Institution},
    conflict_detail="That school is already registered for this tournament",
)
router.include_router(registrations)
