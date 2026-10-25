from datetime import date

from sqlmodel import Field, SQLModel, UniqueConstraint

from app.models.enums import WorkshopMode, WorkshopStatus


class WorkshopBase(SQLModel):
    title: str = Field(min_length=1)
    held_on: date = Field(index=True)
    end_on: date | None = None
    venue: str | None = None
    mode: WorkshopMode = WorkshopMode.IN_PERSON
    status: WorkshopStatus = WorkshopStatus.PLANNED
    description: str | None = None
    # Attendees not counted under a listed school (teachers, walk-ins, ...).
    other_attendees: int = Field(default=0, ge=0)
    notes: str | None = None


class Workshop(WorkshopBase, table=True):
    """A workshop the DC ran. Money is ledger transactions tagged with
    `Transaction.workshop_id` (fees in, costs out), so a workshop with ledger entries can't
    be deleted (409)."""

    id: int | None = Field(default=None, primary_key=True)


class WorkshopCreate(WorkshopBase):
    pass


class WorkshopPublic(WorkshopBase):
    id: int


class WorkshopUpdate(SQLModel):
    title: str | None = Field(default=None, min_length=1)
    held_on: date | None = None
    end_on: date | None = None
    venue: str | None = None
    mode: WorkshopMode | None = None
    status: WorkshopStatus | None = None
    description: str | None = None
    other_attendees: int | None = Field(default=None, ge=0)
    notes: str | None = None


class WorkshopSchoolBase(SQLModel):
    students: int | None = Field(default=None, ge=0)
    notes: str | None = None


class WorkshopSchool(WorkshopSchoolBase, table=True):
    """A school that attended a workshop, with how many of its students came."""

    __table_args__ = (UniqueConstraint("workshop_id", "institution_id"),)

    id: int | None = Field(default=None, primary_key=True)
    workshop_id: int = Field(foreign_key="workshop.id", index=True, ondelete="CASCADE")
    institution_id: int = Field(foreign_key="institution.id", index=True)


class WorkshopSchoolCreate(WorkshopSchoolBase):
    workshop_id: int
    institution_id: int


class WorkshopSchoolPublic(WorkshopSchoolBase):
    id: int
    workshop_id: int
    institution_id: int


class WorkshopSchoolUpdate(SQLModel):
    students: int | None = Field(default=None, ge=0)
    notes: str | None = None
    institution_id: int | None = None


class WorkshopFacilitatorBase(SQLModel):
    # Free text: lead, assistant, guest speaker, ...
    role: str | None = None


class WorkshopFacilitator(WorkshopFacilitatorBase, table=True):
    __table_args__ = (UniqueConstraint("workshop_id", "person_id"),)

    id: int | None = Field(default=None, primary_key=True)
    workshop_id: int = Field(foreign_key="workshop.id", index=True, ondelete="CASCADE")
    person_id: int = Field(foreign_key="person.id", index=True, ondelete="CASCADE")


class WorkshopFacilitatorCreate(WorkshopFacilitatorBase):
    workshop_id: int
    person_id: int


class WorkshopFacilitatorPublic(WorkshopFacilitatorBase):
    id: int
    workshop_id: int
    person_id: int


class WorkshopFacilitatorUpdate(SQLModel):
    role: str | None = None
