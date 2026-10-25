from sqlmodel import Field, SQLModel

from app.models.membership import YEAR_MAX, YEAR_MIN


class SchoolParticipationBase(SQLModel):
    year: int = Field(ge=YEAR_MIN, le=YEAR_MAX)
    event_name: str = Field(min_length=1)
    teams: int | None = Field(default=None, ge=0)
    notes: str | None = None


class SchoolParticipation(SchoolParticipationBase, table=True):
    """A tournament a school attended that was never imported. Imported tournaments are
    derived from Team rows instead, so they don't belong here."""

    id: int | None = Field(default=None, primary_key=True)
    institution_id: int = Field(foreign_key="institution.id", index=True)


class SchoolParticipationCreate(SchoolParticipationBase):
    institution_id: int


class SchoolParticipationPublic(SchoolParticipationBase):
    id: int
    institution_id: int


class SchoolParticipationUpdate(SQLModel):
    year: int | None = Field(default=None, ge=YEAR_MIN, le=YEAR_MAX)
    event_name: str | None = Field(default=None, min_length=1)
    teams: int | None = Field(default=None, ge=0)
    notes: str | None = None
