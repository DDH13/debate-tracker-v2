from datetime import date
from decimal import Decimal

from pydantic import field_validator
from sqlmodel import Field, SQLModel

from app.models.enums import BillingCycle, PaymentMethod
from app.models.membership import http_url
from app.models.transaction import currency_code


def _currency_or_lkr(value: str | None) -> str | None:
    # Blank means LKR rather than null: the column is NOT NULL. An explicit null in a PATCH
    # still reaches the factory's NOT NULL guard.
    if value is None:
        return None
    return currency_code(value) or "LKR"


class SubscriptionBase(SQLModel):
    name: str = Field(min_length=1)
    vendor: str | None = None
    # The bill as charged, in its own currency.
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    currency: str = "LKR"
    # What one cycle costs in LKR: used for the monthly-recurring estimate and to prefill
    # the expense when a payment is recorded.
    amount_lkr: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    billing_cycle: BillingCycle = BillingCycle.MONTHLY
    next_renewal_on: date | None = None
    payment_method: PaymentMethod | None = None
    url: str | None = None
    active: bool = True
    notes: str | None = None

    _check_currency = field_validator("currency")(_currency_or_lkr)
    _check_url = field_validator("url")(http_url)


class Subscription(SubscriptionBase, table=True):
    """A recurring bill (Zoom, CapCut, ...). Each payment is a ledger expense linked back to
    it via `Transaction.subscription_id`."""

    id: int | None = Field(default=None, primary_key=True)
    category_id: int = Field(foreign_key="financecategory.id", index=True)
    account_id: int | None = Field(default=None, foreign_key="account.id", index=True)
    # Set when someone pays it on their own card: each recorded payment is then a pending
    # reimbursement to them.
    paid_by_person_id: int | None = Field(default=None, foreign_key="person.id", index=True)


class SubscriptionCreate(SubscriptionBase):
    category_id: int
    account_id: int | None = None
    paid_by_person_id: int | None = None


class SubscriptionPublic(SubscriptionBase):
    id: int
    category_id: int
    account_id: int | None
    paid_by_person_id: int | None


class SubscriptionUpdate(SQLModel):
    name: str | None = Field(default=None, min_length=1)
    vendor: str | None = None
    amount: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    currency: str | None = None
    amount_lkr: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    billing_cycle: BillingCycle | None = None
    next_renewal_on: date | None = None
    payment_method: PaymentMethod | None = None
    url: str | None = None
    active: bool | None = None
    notes: str | None = None
    category_id: int | None = None
    account_id: int | None = None
    paid_by_person_id: int | None = None

    _check_currency = field_validator("currency")(_currency_or_lkr)
    _check_url = field_validator("url")(http_url)
