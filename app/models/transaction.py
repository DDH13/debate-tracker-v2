from datetime import date, datetime, timezone
from decimal import Decimal

from pydantic import field_validator
from sqlmodel import CheckConstraint, Field, SQLModel

from app.models.enums import PaymentMethod, ReimbursementStatus, TransactionKind
from app.models.membership import http_url


def currency_code(value: str | None) -> str | None:
    """Blank → None; otherwise a three-letter ISO 4217 code, upper-cased."""
    if value is None or not value.strip():
        return None
    value = value.strip().upper()
    if len(value) != 3 or not value.isalpha():
        raise ValueError("must be a three-letter currency code, e.g. USD")
    return value


class TransactionBase(SQLModel):
    kind: TransactionKind
    occurred_on: date = Field(index=True)
    description: str = Field(min_length=1)
    # Always LKR and always positive; `kind` gives the direction.
    amount_lkr: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    # What was actually paid, when it wasn't LKR (e.g. a USD Zoom bill).
    original_amount: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    original_currency: str | None = None
    counterparty: str | None = None
    payment_method: PaymentMethod | None = None
    reference: str | None = None
    receipt_url: str | None = None
    notes: str | None = None
    reimbursement_status: ReimbursementStatus = ReimbursementStatus.NONE
    reimbursed_on: date | None = None

    _check_currency = field_validator("original_currency")(currency_code)
    _check_receipt = field_validator("receipt_url")(http_url)


class Transaction(TransactionBase, table=True):
    """One line in the DC's ledger. The cross-field rules (a transfer needs two accounts, an
    expense needs an expense category, ...) live in `app/services/finance.py`
    `validate_transaction`, since they depend on other rows."""

    __table_args__ = (CheckConstraint("amount_lkr > 0", name="ck_transaction_amount_positive"),)

    id: int | None = Field(default=None, primary_key=True)
    # The account money left (expense, transfer) or arrived in (income). Empty only for a
    # pending reimbursement: the money came out of someone's own pocket.
    account_id: int | None = Field(default=None, foreign_key="account.id", index=True)
    to_account_id: int | None = Field(default=None, foreign_key="account.id", index=True)
    category_id: int | None = Field(default=None, foreign_key="financecategory.id", index=True)
    membership_id: int | None = Field(
        default=None, foreign_key="membership.id", index=True, ondelete="SET NULL"
    )
    subscription_id: int | None = Field(
        default=None, foreign_key="subscription.id", index=True, ondelete="SET NULL"
    )
    tournament_id: int | None = Field(
        default=None, foreign_key="tournament.id", index=True, ondelete="SET NULL"
    )
    institution_id: int | None = Field(
        default=None, foreign_key="institution.id", index=True, ondelete="SET NULL"
    )
    paid_by_person_id: int | None = Field(default=None, foreign_key="person.id", index=True)
    created_by_id: int | None = Field(default=None, foreign_key="user.id", ondelete="SET NULL")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TransactionCreate(TransactionBase):
    account_id: int | None = None
    to_account_id: int | None = None
    category_id: int | None = None
    membership_id: int | None = None
    subscription_id: int | None = None
    tournament_id: int | None = None
    institution_id: int | None = None
    paid_by_person_id: int | None = None


class TransactionPublic(TransactionBase):
    id: int
    account_id: int | None
    to_account_id: int | None
    category_id: int | None
    membership_id: int | None
    subscription_id: int | None
    tournament_id: int | None
    institution_id: int | None
    paid_by_person_id: int | None
    created_by_id: int | None
    created_at: datetime
    updated_at: datetime


class TransactionUpdate(SQLModel):
    kind: TransactionKind | None = None
    occurred_on: date | None = None
    description: str | None = Field(default=None, min_length=1)
    amount_lkr: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    original_amount: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    original_currency: str | None = None
    counterparty: str | None = None
    payment_method: PaymentMethod | None = None
    reference: str | None = None
    receipt_url: str | None = None
    notes: str | None = None
    reimbursement_status: ReimbursementStatus | None = None
    reimbursed_on: date | None = None
    account_id: int | None = None
    to_account_id: int | None = None
    category_id: int | None = None
    membership_id: int | None = None
    subscription_id: int | None = None
    tournament_id: int | None = None
    institution_id: int | None = None
    paid_by_person_id: int | None = None

    _check_currency = field_validator("original_currency")(currency_code)
    _check_receipt = field_validator("receipt_url")(http_url)
