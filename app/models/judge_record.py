from sqlmodel import Field, SQLModel

from app.models.enums import AffiliationRole, JudgeLevel
from app.models.membership import YEAR_MAX, YEAR_MIN


class JudgeRecordBase(SQLModel):
    level: JudgeLevel | None = None
    experience_notes: str | None = None
    notes: str | None = None


class JudgeRecord(JudgeRecordBase, table=True):
    """The board's view of a judge. Admin-only, so it isn't on `Judge` (whose
    `JudgePublic` is served by the open API)."""

    id: int | None = Field(default=None, primary_key=True)
    judge_id: int = Field(foreign_key="judge.id", unique=True, ondelete="CASCADE")


class JudgeRecordUpdate(JudgeRecordBase):
    pass


class JudgeRecordPublic(JudgeRecordBase):
    id: int
    judge_id: int


class JudgeAffiliationBase(SQLModel):
    role: AffiliationRole = AffiliationRole.ALUMNUS
    from_year: int | None = Field(default=None, ge=YEAR_MIN, le=YEAR_MAX)
    to_year: int | None = Field(default=None, ge=YEAR_MIN, le=YEAR_MAX)
    notes: str | None = None


class JudgeAffiliation(JudgeAffiliationBase, table=True):
    """A school a judge is connected to (alumnus of, coach at, ...), for spotting conflicts
    and seeing who judges for whom."""

    id: int | None = Field(default=None, primary_key=True)
    judge_id: int = Field(foreign_key="judge.id", index=True, ondelete="CASCADE")
    institution_id: int = Field(foreign_key="institution.id", index=True)


class JudgeAffiliationCreate(JudgeAffiliationBase):
    judge_id: int
    institution_id: int


class JudgeAffiliationPublic(JudgeAffiliationBase):
    id: int
    judge_id: int
    institution_id: int


class JudgeAffiliationUpdate(SQLModel):
    role: AffiliationRole | None = None
    from_year: int | None = Field(default=None, ge=YEAR_MIN, le=YEAR_MAX)
    to_year: int | None = Field(default=None, ge=YEAR_MIN, le=YEAR_MAX)
    notes: str | None = None
    institution_id: int | None = None
