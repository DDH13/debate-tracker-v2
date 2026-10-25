from datetime import date
from decimal import Decimal

from pydantic import field_validator
from sqlmodel import CheckConstraint, Field, SQLModel

from app.models.enums import TripCostCategory, TripStatus
from app.models.membership import http_url
from app.models.transaction import currency_code


class TripBase(SQLModel):
    name: str = Field(min_length=1)
    # Free-text grouping used by forecasts, e.g. "WSDC".
    event: str = Field(min_length=1, index=True)
    country: str | None = None
    city: str | None = None
    start_on: date | None = None
    end_on: date | None = None
    status: TripStatus = TripStatus.PLANNING
    result_summary: str | None = None
    notes: str | None = None


class Trip(TripBase, table=True):
    """A trip abroad. Money lives in three places: budget lines (the plan), ledger expenses
    tagged with `Transaction.trip_id` (paid by the DC) and `TripDirectCost` (paid directly by
    families, never in the ledger)."""

    id: int | None = Field(default=None, primary_key=True)
    squad_id: int | None = Field(default=None, foreign_key="squad.id", index=True, ondelete="SET NULL")
    tournament_id: int | None = Field(default=None, foreign_key="tournament.id", index=True, ondelete="SET NULL")


class TripCreate(TripBase):
    squad_id: int | None = None
    tournament_id: int | None = None


class TripPublic(TripBase):
    id: int
    squad_id: int | None
    tournament_id: int | None


class TripUpdate(SQLModel):
    name: str | None = Field(default=None, min_length=1)
    event: str | None = Field(default=None, min_length=1)
    country: str | None = None
    city: str | None = None
    start_on: date | None = None
    end_on: date | None = None
    status: TripStatus | None = None
    result_summary: str | None = None
    notes: str | None = None
    squad_id: int | None = None
    tournament_id: int | None = None


class TripTravellerBase(SQLModel):
    name: str = Field(min_length=1)
    role: str | None = None
    notes: str | None = None


class TripTraveller(TripTravellerBase, table=True):
    """Just a name: travellers aren't linked to debaters, judges or people."""

    id: int | None = Field(default=None, primary_key=True)
    trip_id: int = Field(foreign_key="trip.id", index=True, ondelete="CASCADE")


class TripTravellerCreate(TripTravellerBase):
    trip_id: int


class TripTravellerPublic(TripTravellerBase):
    id: int
    trip_id: int


class TripTravellerUpdate(SQLModel):
    name: str | None = Field(default=None, min_length=1)
    role: str | None = None
    notes: str | None = None


class TripBudgetLineBase(SQLModel):
    category: TripCostCategory
    description: str | None = None
    amount_lkr: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    notes: str | None = None


class TripBudgetLine(TripBudgetLineBase, table=True):
    __table_args__ = (CheckConstraint("amount_lkr > 0", name="ck_tripbudgetline_amount_positive"),)

    id: int | None = Field(default=None, primary_key=True)
    trip_id: int = Field(foreign_key="trip.id", index=True, ondelete="CASCADE")
    traveller_id: int | None = Field(
        default=None, foreign_key="triptraveller.id", index=True, ondelete="SET NULL"
    )


class TripBudgetLineCreate(TripBudgetLineBase):
    trip_id: int
    traveller_id: int | None = None


class TripBudgetLinePublic(TripBudgetLineBase):
    id: int
    trip_id: int
    traveller_id: int | None


class TripBudgetLineUpdate(SQLModel):
    category: TripCostCategory | None = None
    description: str | None = None
    amount_lkr: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    notes: str | None = None
    traveller_id: int | None = None


class TripDirectCostBase(SQLModel):
    category: TripCostCategory
    description: str | None = None
    amount_lkr: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    original_amount: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    original_currency: str | None = None
    paid_on: date | None = None
    # Free text, e.g. "Parent of A. Perera".
    paid_by: str | None = None
    receipt_url: str | None = None
    notes: str | None = None

    _check_currency = field_validator("original_currency")(currency_code)
    _check_receipt = field_validator("receipt_url")(http_url)


class TripDirectCost(TripDirectCostBase, table=True):
    """A cost a family (or anyone outside the DC) paid directly. Deliberately not a ledger
    transaction: the money never touched a DC account."""

    __table_args__ = (CheckConstraint("amount_lkr > 0", name="ck_tripdirectcost_amount_positive"),)

    id: int | None = Field(default=None, primary_key=True)
    trip_id: int = Field(foreign_key="trip.id", index=True, ondelete="CASCADE")
    traveller_id: int | None = Field(
        default=None, foreign_key="triptraveller.id", index=True, ondelete="SET NULL"
    )


class TripDirectCostCreate(TripDirectCostBase):
    trip_id: int
    traveller_id: int | None = None


class TripDirectCostPublic(TripDirectCostBase):
    id: int
    trip_id: int
    traveller_id: int | None


class TripDirectCostUpdate(SQLModel):
    category: TripCostCategory | None = None
    description: str | None = None
    amount_lkr: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    original_amount: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    original_currency: str | None = None
    paid_on: date | None = None
    paid_by: str | None = None
    receipt_url: str | None = None
    notes: str | None = None
    traveller_id: int | None = None

    _check_currency = field_validator("original_currency")(currency_code)
    _check_receipt = field_validator("receipt_url")(http_url)
