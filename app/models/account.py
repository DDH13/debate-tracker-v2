from datetime import date
from decimal import Decimal

from sqlmodel import Field, SQLModel

from app.models.enums import AccountKind


class AccountBase(SQLModel):
    name: str = Field(min_length=1, unique=True)
    kind: AccountKind = AccountKind.BANK
    # May be negative (an overdrawn account). Balances count this plus every transaction
    # dated on or after `opening_date`.
    opening_balance_lkr: Decimal = Field(default=Decimal("0"), max_digits=12, decimal_places=2)
    opening_date: date
    active: bool = True
    notes: str | None = None


class Account(AccountBase, table=True):
    """Somewhere the DC holds money: a bank account, the cash box, a mobile wallet."""

    id: int | None = Field(default=None, primary_key=True)


class AccountCreate(AccountBase):
    pass


class AccountPublic(AccountBase):
    id: int


class AccountUpdate(SQLModel):
    name: str | None = Field(default=None, min_length=1)
    kind: AccountKind | None = None
    opening_balance_lkr: Decimal | None = Field(default=None, max_digits=12, decimal_places=2)
    opening_date: date | None = None
    active: bool | None = None
    notes: str | None = None
