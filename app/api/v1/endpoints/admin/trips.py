from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlmodel import Session, SQLModel, col

from app.api.crud import ListParams, add_crud_routes
from app.api.deps import SessionDep, get_or_404
from app.models import (
    Squad,
    Tournament,
    Trip,
    TripBudgetLine,
    TripBudgetLineCreate,
    TripBudgetLinePublic,
    TripBudgetLineUpdate,
    TripCreate,
    TripDirectCost,
    TripDirectCostCreate,
    TripDirectCostPublic,
    TripDirectCostUpdate,
    TripPublic,
    TripStatus,
    TripTraveller,
    TripTravellerCreate,
    TripTravellerPublic,
    TripTravellerUpdate,
    TripUpdate,
    User,
)
from app.services import trips as trip_service
from app.services.trips import TripConflict, TripInvalid

router = APIRouter()


@contextmanager
def trip_errors() -> Iterator[None]:
    try:
        yield
    except TripInvalid as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except TripConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


# --- Trips ----------------------------------------------------------------------------


class TripFilters(ListParams):
    status: TripStatus | None = None
    event: str | None = None
    squad_id: int | None = None


class CopyBudget(SQLModel):
    from_trip_id: int
    # Multiply by this trip's travellers ÷ the source trip's.
    scale: bool = False
    # Delete this trip's existing budget lines first.
    replace: bool = False


class ApplyForecast(SQLModel):
    replace: bool = False


def _check_trip(_session: Session, trip: Trip, _user: User) -> None:
    if trip.start_on and trip.end_on and trip.end_on < trip.start_on:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="end_on must be on or after start_on"
        )


def _guard_trip_delete(session: Session, trip: Trip) -> None:
    with trip_errors():
        trip_service.ensure_deletable(session, trip)


trips = APIRouter(prefix="/trips", tags=["admin: trips"])


# Static paths must come before `/{item_id}`, which the generated routes register.
@trips.get("/forecast", response_model=trip_service.TripForecast)
def forecast(
    session: SessionDep,
    event: Annotated[str, Query(min_length=1)],
    travellers: Annotated[int, Query(ge=0)] = 1,
    exclude_trip_id: int | None = None,
) -> trip_service.TripForecast:
    """Per-person costs of past completed trips with the same event, and the average scaled
    to `travellers`."""
    return trip_service.trip_forecast(session, event, travellers, exclude_trip_id)


@trips.get("/{trip_id}/costs", response_model=trip_service.TripCosts)
def costs(trip_id: int, session: SessionDep) -> trip_service.TripCosts:
    """Budget vs actual (DC-paid + paid directly) by category and by traveller."""
    return trip_service.trip_costs(session, get_or_404(session, Trip, trip_id))


@trips.post("/{trip_id}/copy-budget", response_model=list[TripBudgetLinePublic], status_code=status.HTTP_201_CREATED)
def copy_budget(trip_id: int, body: CopyBudget, session: SessionDep) -> list[TripBudgetLine]:
    trip = get_or_404(session, Trip, trip_id)
    source = get_or_404(session, Trip, body.from_trip_id)
    with trip_errors():
        return trip_service.copy_budget(session, trip, source, body.scale, body.replace)


@trips.post("/{trip_id}/apply-forecast", response_model=list[TripBudgetLinePublic], status_code=status.HTTP_201_CREATED)
def apply_forecast(trip_id: int, session: SessionDep, body: ApplyForecast | None = None) -> list[TripBudgetLine]:
    """Adds a budget line per forecast category, sized for this trip's travellers."""
    trip = get_or_404(session, Trip, trip_id)
    with trip_errors():
        return trip_service.apply_forecast(session, trip, (body or ApplyForecast()).replace)


add_crud_routes(
    trips,
    noun="trip",
    model=Trip,
    create_schema=TripCreate,
    public_schema=TripPublic,
    update_schema=TripUpdate,
    filters=TripFilters,
    order_by=(col(Trip.start_on).desc(),),
    parents={"squad_id": Squad, "tournament_id": Tournament},
    on_save=_check_trip,
    before_delete=_guard_trip_delete,
)
router.include_router(trips)

# --- Travellers -----------------------------------------------------------------------


class TripChildFilters(ListParams):
    trip_id: int | None = None


travellers = APIRouter(prefix="/trip-travellers", tags=["admin: trips"])
add_crud_routes(
    travellers,
    noun="trip_traveller",
    model=TripTraveller,
    create_schema=TripTravellerCreate,
    public_schema=TripTravellerPublic,
    update_schema=TripTravellerUpdate,
    filters=TripChildFilters,
    order_by=(TripTraveller.trip_id,),
    parents={"trip_id": Trip},
)
router.include_router(travellers)

# --- Budget lines and direct costs ----------------------------------------------------


def _check_line_traveller(session: Session, line: TripBudgetLine | TripDirectCost, _user: User) -> None:
    with trip_errors():
        trip_service.check_traveller(session, line.trip_id, line.traveller_id)


budget_lines = APIRouter(prefix="/trip-budget-lines", tags=["admin: trips"])
add_crud_routes(
    budget_lines,
    noun="trip_budget_line",
    model=TripBudgetLine,
    create_schema=TripBudgetLineCreate,
    public_schema=TripBudgetLinePublic,
    update_schema=TripBudgetLineUpdate,
    filters=TripChildFilters,
    order_by=(TripBudgetLine.trip_id,),
    parents={"trip_id": Trip, "traveller_id": TripTraveller},
    on_save=_check_line_traveller,
)
router.include_router(budget_lines)

direct_costs = APIRouter(prefix="/trip-direct-costs", tags=["admin: trips"])
add_crud_routes(
    direct_costs,
    noun="trip_direct_cost",
    model=TripDirectCost,
    create_schema=TripDirectCostCreate,
    public_schema=TripDirectCostPublic,
    update_schema=TripDirectCostUpdate,
    filters=TripChildFilters,
    order_by=(TripDirectCost.trip_id, col(TripDirectCost.paid_on).desc()),
    parents={"trip_id": Trip, "traveller_id": TripTraveller},
    on_save=_check_line_traveller,
)
router.include_router(direct_costs)
