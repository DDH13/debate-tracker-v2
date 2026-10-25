from datetime import date

from sqlmodel import Field, SQLModel, UniqueConstraint

from app.models.enums import SquadKind, SquadRole


class SquadBase(SQLModel):
    kind: SquadKind
    # Free text, e.g. "2025/26": squads don't follow calendar years.
    season: str = Field(min_length=1)
    notes: str | None = None


class Squad(SquadBase, table=True):
    """One season's National Team or Development Squad."""

    __table_args__ = (UniqueConstraint("kind", "season", name="uq_squad_kind_season"),)

    id: int | None = Field(default=None, primary_key=True)


class SquadCreate(SquadBase):
    pass


class SquadPublic(SquadBase):
    id: int


class SquadUpdate(SQLModel):
    kind: SquadKind | None = None
    season: str | None = Field(default=None, min_length=1)
    notes: str | None = None


class SquadMemberBase(SQLModel):
    role: SquadRole = SquadRole.MEMBER
    joined_on: date | None = None
    left_on: date | None = None
    notes: str | None = None


class SquadMember(SquadMemberBase, table=True):
    __table_args__ = (UniqueConstraint("squad_id", "debater_id", name="uq_squadmember_squad_debater"),)

    id: int | None = Field(default=None, primary_key=True)
    squad_id: int = Field(foreign_key="squad.id", index=True, ondelete="CASCADE")
    # Plain FK: a debater on a squad can't be deleted, and a merge moves the row.
    debater_id: int = Field(foreign_key="debater.id", index=True)


class SquadMemberCreate(SquadMemberBase):
    squad_id: int
    debater_id: int


class SquadMemberPublic(SquadMemberBase):
    id: int
    squad_id: int
    debater_id: int


class SquadMemberUpdate(SQLModel):
    role: SquadRole | None = None
    joined_on: date | None = None
    left_on: date | None = None
    notes: str | None = None


class SquadStaffBase(SQLModel):
    name: str = Field(min_length=1)
    # Free text: coach, manager, ...
    role: str | None = None
    notes: str | None = None


class SquadStaff(SquadStaffBase, table=True):
    id: int | None = Field(default=None, primary_key=True)
    squad_id: int = Field(foreign_key="squad.id", index=True, ondelete="CASCADE")
    # Optional link when the staff member is also one of the DC's people.
    person_id: int | None = Field(default=None, foreign_key="person.id", index=True, ondelete="SET NULL")


class SquadStaffCreate(SquadStaffBase):
    squad_id: int
    person_id: int | None = None


class SquadStaffPublic(SquadStaffBase):
    id: int
    squad_id: int
    person_id: int | None


class SquadStaffUpdate(SQLModel):
    name: str | None = Field(default=None, min_length=1)
    role: str | None = None
    notes: str | None = None
    person_id: int | None = None
