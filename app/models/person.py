from sqlmodel import Field, SQLModel

from app.models.enums import PersonKind


class PersonBase(SQLModel):
    name: str = Field(min_length=1)
    kind: PersonKind = PersonKind.VOLUNTEER
    email: str | None = None
    phone: str | None = None
    active: bool = True
    notes: str | None = None


class Person(PersonBase, table=True):
    """Someone in the DC itself: a board member, volunteer or staff member. Debaters on the
    squads are `Debater`s, and school contacts are stored per school, so neither is here."""

    id: int | None = Field(default=None, primary_key=True)


class PersonCreate(PersonBase):
    pass


class PersonPublic(PersonBase):
    id: int


class PersonUpdate(SQLModel):
    name: str | None = Field(default=None, min_length=1)
    kind: PersonKind | None = None
    email: str | None = None
    phone: str | None = None
    active: bool | None = None
    notes: str | None = None
