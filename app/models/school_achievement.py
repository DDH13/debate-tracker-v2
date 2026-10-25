from sqlmodel import Field, SQLModel

from app.models.membership import YEAR_MAX, YEAR_MIN


class SchoolAchievementBase(SQLModel):
    year: int = Field(ge=YEAR_MIN, le=YEAR_MAX)
    title: str = Field(min_length=1)
    event_name: str | None = None
    notes: str | None = None


class SchoolAchievement(SchoolAchievementBase, table=True):
    id: int | None = Field(default=None, primary_key=True)
    institution_id: int = Field(foreign_key="institution.id", index=True)
    tournament_id: int | None = Field(
        default=None, foreign_key="tournament.id", ondelete="SET NULL"
    )


class SchoolAchievementCreate(SchoolAchievementBase):
    institution_id: int
    tournament_id: int | None = None


class SchoolAchievementPublic(SchoolAchievementBase):
    id: int
    institution_id: int
    tournament_id: int | None


class SchoolAchievementUpdate(SQLModel):
    year: int | None = Field(default=None, ge=YEAR_MIN, le=YEAR_MAX)
    title: str | None = Field(default=None, min_length=1)
    event_name: str | None = None
    notes: str | None = None
    tournament_id: int | None = None
