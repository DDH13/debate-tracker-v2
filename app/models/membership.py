from datetime import date
from decimal import Decimal

from pydantic import field_validator
from sqlmodel import Field, SQLModel, UniqueConstraint

YEAR_MIN = 1990
YEAR_MAX = 2100

_LINK_FIELDS = ("payment_proof_url", "request_letter_url")


def http_url(value: str | None) -> str | None:
    """Blank → None; anything else must be an http(s) URL. The console renders these as
    links, so a `javascript:` value must never get in."""
    if value is None or not value.strip():
        return None
    value = value.strip()
    if not value.lower().startswith(("http://", "https://")):
        raise ValueError("must be an http(s) URL")
    return value


class MembershipBase(SQLModel):
    year: int = Field(ge=YEAR_MIN, le=YEAR_MAX, index=True)
    registered_on: date | None = None
    reason: str | None = None
    fee_lkr: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    paid_on: date | None = None
    # Links to the registration documents (e.g. the Drive files a Google Form upload makes).
    payment_proof_url: str | None = None
    request_letter_url: str | None = None
    notes: str | None = None

    _check_links = field_validator(*_LINK_FIELDS)(http_url)


class Membership(MembershipBase, table=True):
    """One school's DC membership for one (calendar) year."""

    __table_args__ = (UniqueConstraint("institution_id", "year"),)

    id: int | None = Field(default=None, primary_key=True)
    institution_id: int = Field(foreign_key="institution.id", index=True)


class MembershipCreate(MembershipBase):
    institution_id: int


class MembershipPublic(MembershipBase):
    id: int
    institution_id: int


class MembershipUpdate(SQLModel):
    year: int | None = Field(default=None, ge=YEAR_MIN, le=YEAR_MAX)
    registered_on: date | None = None
    reason: str | None = None
    fee_lkr: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    paid_on: date | None = None
    payment_proof_url: str | None = None
    request_letter_url: str | None = None
    notes: str | None = None

    _check_links = field_validator(*_LINK_FIELDS)(http_url)
