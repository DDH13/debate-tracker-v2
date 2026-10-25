from datetime import date, datetime, timezone

from sqlmodel import Field, SQLModel

from app.models.enums import LeadChannel, LeadStatus


class OutreachLeadBase(SQLModel):
    school_name: str | None = None
    contact_name: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    first_contact_on: date | None = None
    channel: LeadChannel | None = None
    status: LeadStatus = LeadStatus.NEW
    next_follow_up_on: date | None = None
    notes: str | None = None


class OutreachLead(OutreachLeadBase, table=True):
    """A non-member school that reached out to the DC. Either `school_name` or a link to an
    existing institution identifies it."""

    id: int | None = Field(default=None, primary_key=True)
    institution_id: int | None = Field(
        default=None, foreign_key="institution.id", index=True, ondelete="SET NULL"
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class OutreachLeadCreate(OutreachLeadBase):
    institution_id: int | None = None


class OutreachLeadPublic(OutreachLeadBase):
    id: int
    institution_id: int | None
    created_at: datetime


class OutreachLeadUpdate(SQLModel):
    school_name: str | None = None
    contact_name: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    first_contact_on: date | None = None
    channel: LeadChannel | None = None
    status: LeadStatus | None = None
    next_follow_up_on: date | None = None
    notes: str | None = None
    institution_id: int | None = None
