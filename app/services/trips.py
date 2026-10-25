"""Trip money: what a trip was budgeted, what was actually spent and by whom, and a forecast
for the next trip from past ones.

Actual cost has two sources and nothing else. The DC pays from its accounts: ledger expenses
tagged with `Transaction.trip_id` (a pending reimbursement counts, since the money is spent
even if the person who fronted it hasn't been paid back). Families pay some costs directly:
`TripDirectCost` rows, which never enter the ledger because the money never touched a DC
account. Amounts are LKR `Decimal`s throughout.
"""

from collections import defaultdict
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlmodel import Session, SQLModel, col, func, select

from app.models import (
    Transaction,
    TransactionKind,
    Trip,
    TripBudgetLine,
    TripCostCategory,
    TripDirectCost,
    TripStatus,
    TripTraveller,
)

ZERO = Decimal("0")
CENT = Decimal("0.01")
SHARED = "Shared"
CATEGORY_ORDER = list(TripCostCategory)


class TripInvalid(ValueError):
    """The request breaks a trip rule (→ 422)."""


class TripConflict(ValueError):
    """The action doesn't fit the trip's current state (→ 409)."""


def _cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


# --- Costs --------------------------------------------------------------------------------


class CostFigures(SQLModel):
    budget_lkr: Decimal = ZERO
    dc_lkr: Decimal = ZERO
    direct_lkr: Decimal = ZERO
    # DC + direct.
    actual_lkr: Decimal = ZERO
    # Actual minus budget: positive means over budget.
    variance_lkr: Decimal = ZERO


class CategoryCosts(CostFigures):
    category: TripCostCategory


class TravellerCosts(CostFigures):
    # None for the "Shared" row: lines that name no traveller.
    traveller_id: int | None
    name: str


class TripCosts(CostFigures):
    trip_id: int
    travellers: int
    per_person_actual_lkr: Decimal | None
    per_person_budget_lkr: Decimal | None
    by_category: list[CategoryCosts]
    by_traveller: list[TravellerCosts]


class _Totals:
    def __init__(self) -> None:
        self.budget = ZERO
        self.dc = ZERO
        self.direct = ZERO

    def figures(self) -> dict[str, Decimal]:
        actual = self.dc + self.direct
        # Two decimal places even when nothing was added (Decimal(0) would serialise as "0").
        return {
            "budget_lkr": _cents(self.budget),
            "dc_lkr": _cents(self.dc),
            "direct_lkr": _cents(self.direct),
            "actual_lkr": _cents(actual),
            "variance_lkr": _cents(actual - self.budget),
        }

    def empty(self) -> bool:
        return not (self.budget or self.dc or self.direct)


def trip_costs(session: Session, trip: Trip) -> TripCosts:
    """Budget, DC-paid and directly-paid amounts for `trip`, by category and by traveller
    (plus a Shared row for lines naming nobody), with totals and per-person figures."""
    travellers = session.exec(
        select(TripTraveller).where(TripTraveller.trip_id == trip.id).order_by(TripTraveller.id)
    ).all()
    by_category: dict[TripCostCategory, _Totals] = defaultdict(_Totals)
    by_traveller: dict[int | None, _Totals] = defaultdict(_Totals)
    total = _Totals()

    def add(category: TripCostCategory, traveller_id: int | None, amount, field: str) -> None:
        amount = Decimal(amount)
        for bucket in (by_category[category], by_traveller[traveller_id], total):
            setattr(bucket, field, getattr(bucket, field) + amount)

    for line in session.exec(select(TripBudgetLine).where(TripBudgetLine.trip_id == trip.id)).all():
        add(line.category, line.traveller_id, line.amount_lkr, "budget")
    for cost in session.exec(select(TripDirectCost).where(TripDirectCost.trip_id == trip.id)).all():
        add(cost.category, cost.traveller_id, cost.amount_lkr, "direct")
    ledger = session.exec(
        select(Transaction).where(Transaction.trip_id == trip.id, Transaction.kind == TransactionKind.EXPENSE)
    ).all()
    for txn in ledger:
        add(txn.trip_cost_category or TripCostCategory.OTHER, txn.trip_traveller_id, txn.amount_lkr, "dc")

    count = len(travellers)
    figures = total.figures()
    rows = [
        CategoryCosts(category=c, **by_category[c].figures())
        for c in CATEGORY_ORDER
        if c in by_category and not by_category[c].empty()
    ]
    people = [
        TravellerCosts(traveller_id=t.id, name=t.name, **by_traveller[t.id].figures()) for t in travellers
    ]
    shared = by_traveller.get(None)
    if shared is not None and not shared.empty():
        people.append(TravellerCosts(traveller_id=None, name=SHARED, **shared.figures()))
    return TripCosts(
        trip_id=trip.id,
        travellers=count,
        per_person_actual_lkr=_cents(figures["actual_lkr"] / count) if count else None,
        per_person_budget_lkr=_cents(figures["budget_lkr"] / count) if count else None,
        by_category=rows,
        by_traveller=people,
        **figures,
    )


# --- Forecast -----------------------------------------------------------------------------


class ForecastTrip(SQLModel):
    trip_id: int
    name: str
    start_on: date | None = None
    end_on: date | None = None
    travellers: int
    actual_lkr: Decimal
    per_person_lkr: Decimal


class ForecastCategory(SQLModel):
    category: TripCostCategory
    per_person_lkr: Decimal
    suggested_lkr: Decimal


class TripForecast(SQLModel):
    event: str
    travellers: int
    trips: list[ForecastTrip]
    by_category: list[ForecastCategory]
    per_person_lkr: Decimal
    suggested_total_lkr: Decimal


def trip_forecast(
    session: Session, event: str, travellers: int, exclude_trip_id: int | None = None
) -> TripForecast:
    """Per-person costs from completed trips with the same `event` (case-insensitive) that
    had travellers and some actual cost, averaged by category, and that average scaled to
    `travellers`. A category a past trip didn't spend on counts as zero for that trip."""
    event = event.strip()
    statement = select(Trip).where(
        func.lower(Trip.event) == event.lower(), Trip.status == TripStatus.COMPLETED
    )
    if exclude_trip_id is not None:
        statement = statement.where(Trip.id != exclude_trip_id)
    past: list[ForecastTrip] = []
    per_person_by_category: list[dict[TripCostCategory, Decimal]] = []
    for trip in session.exec(statement.order_by(col(Trip.start_on).desc(), col(Trip.id).desc())).all():
        costs = trip_costs(session, trip)
        if costs.travellers == 0 or costs.actual_lkr <= 0:
            continue
        past.append(
            ForecastTrip(
                trip_id=trip.id,
                name=trip.name,
                start_on=trip.start_on,
                end_on=trip.end_on,
                travellers=costs.travellers,
                actual_lkr=costs.actual_lkr,
                per_person_lkr=costs.per_person_actual_lkr,
            )
        )
        per_person_by_category.append({c.category: c.actual_lkr / costs.travellers for c in costs.by_category})

    categories: list[ForecastCategory] = []
    for category in CATEGORY_ORDER:
        average = sum((p.get(category, ZERO) for p in per_person_by_category), ZERO) / len(past) if past else ZERO
        suggested = _cents(average * travellers)
        if average > 0:
            categories.append(
                ForecastCategory(category=category, per_person_lkr=_cents(average), suggested_lkr=suggested)
            )
    return TripForecast(
        event=event,
        travellers=travellers,
        trips=past,
        by_category=categories,
        per_person_lkr=sum((c.per_person_lkr for c in categories), ZERO),
        suggested_total_lkr=sum((c.suggested_lkr for c in categories), ZERO),
    )


# --- Budget actions -----------------------------------------------------------------------


def _traveller_count(session: Session, trip: Trip) -> int:
    return session.exec(select(func.count()).select_from(TripTraveller).where(TripTraveller.trip_id == trip.id)).one()


def _clear_budget(session: Session, trip: Trip) -> None:
    for line in session.exec(select(TripBudgetLine).where(TripBudgetLine.trip_id == trip.id)).all():
        session.delete(line)


def copy_budget(
    session: Session, trip: Trip, source: Trip, scale: bool = False, replace: bool = False
) -> list[TripBudgetLine]:
    """Copies `source`'s budget lines onto `trip` (without traveller links). `scale`
    multiplies each amount by this trip's travellers ÷ the source's; `replace` deletes this
    trip's existing lines first."""
    if source.id == trip.id:
        raise TripInvalid("A trip can't copy its own budget")
    lines = session.exec(
        select(TripBudgetLine).where(TripBudgetLine.trip_id == source.id).order_by(TripBudgetLine.id)
    ).all()
    if not lines:
        raise TripInvalid(f"{source.name!r} has no budget lines to copy")
    factor = Decimal(1)
    if scale:
        mine, theirs = _traveller_count(session, trip), _traveller_count(session, source)
        if mine == 0 or theirs == 0:
            raise TripInvalid("Scaling needs travellers on both trips")
        factor = Decimal(mine) / Decimal(theirs)
    try:
        if replace:
            _clear_budget(session, trip)
        created = []
        for line in lines:
            amount = _cents(Decimal(line.amount_lkr) * factor)
            if amount <= 0:
                continue
            created.append(
                TripBudgetLine(
                    trip_id=trip.id,
                    category=line.category,
                    description=line.description,
                    amount_lkr=amount,
                    notes=line.notes,
                )
            )
        session.add_all(created)
        session.commit()
    except Exception:
        session.rollback()
        raise
    for line in created:
        session.refresh(line)
    return created


def apply_forecast(session: Session, trip: Trip, replace: bool = False) -> list[TripBudgetLine]:
    """Adds one budget line per forecast category, sized for this trip's travellers."""
    count = _traveller_count(session, trip)
    if count == 0:
        raise TripInvalid("Add the travellers first: the forecast is scaled by their number")
    forecast = trip_forecast(session, trip.event, count, exclude_trip_id=trip.id)
    if not forecast.trips:
        raise TripInvalid(f"No completed {trip.event} trips with costs to forecast from")
    n = len(forecast.trips)
    description = f"Forecast from {n} past trip{'s' if n != 1 else ''}"
    try:
        if replace:
            _clear_budget(session, trip)
        created = [
            TripBudgetLine(trip_id=trip.id, category=c.category, description=description, amount_lkr=c.suggested_lkr)
            for c in forecast.by_category
            if c.suggested_lkr > 0
        ]
        session.add_all(created)
        session.commit()
    except Exception:
        session.rollback()
        raise
    for line in created:
        session.refresh(line)
    return created


# --- Deleting -----------------------------------------------------------------------------


def ensure_deletable(session: Session, trip: Trip) -> None:
    """A trip's budget, travellers and direct costs go with it, but ledger expenses are real
    money history and block the delete."""
    ledger = session.exec(select(func.count()).select_from(Transaction).where(Transaction.trip_id == trip.id)).one()
    if ledger:
        raise TripConflict(
            f"Trip {trip.id} has {ledger} ledger expense{'s' if ledger != 1 else ''}: "
            "unlink or delete them first"
        )


def check_traveller(session: Session, trip_id: int, traveller_id: int | None) -> None:
    """A budget line, direct cost or expense may only name a traveller of its own trip."""
    if traveller_id is None:
        return
    traveller = session.get(TripTraveller, traveller_id)
    if traveller is not None and traveller.trip_id != trip_id:
        raise TripInvalid("That traveller is not on this trip")
