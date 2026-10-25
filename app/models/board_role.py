from datetime import date

from sqlmodel import Field, SQLModel


class BoardRoleBase(SQLModel):
    title: str = Field(min_length=1)
    responsibilities: str | None = None
    term_start: date
    # Empty while the term is current.
    term_end: date | None = None
    notes: str | None = None


class BoardRole(BoardRoleBase, table=True):
    """One person's term in one board role. Kept after the term ends, so past boards stay on
    record; that history is also why a person with roles can't be deleted (409)."""

    id: int | None = Field(default=None, primary_key=True)
    person_id: int = Field(foreign_key="person.id", index=True)


class BoardRoleCreate(BoardRoleBase):
    person_id: int


class BoardRolePublic(BoardRoleBase):
    id: int
    person_id: int


class BoardRoleUpdate(SQLModel):
    title: str | None = Field(default=None, min_length=1)
    responsibilities: str | None = None
    term_start: date | None = None
    term_end: date | None = None
    notes: str | None = None
    person_id: int | None = None
