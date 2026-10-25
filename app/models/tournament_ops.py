from sqlmodel import Field, SQLModel, UniqueConstraint


class TournamentOpsBase(SQLModel):
    # The judge quota rule: each school owes 1 judge per this many teams (rounded up).
    teams_per_judge: int = Field(default=2, ge=1)
    hosted_by_dc: bool = False
    notes: str | None = None


class TournamentOps(TournamentOpsBase, table=True):
    """Admin-only settings for a tournament. Kept off `Tournament` because
    `TournamentPublic` is served by the open API. No row means the defaults."""

    id: int | None = Field(default=None, primary_key=True)
    tournament_id: int = Field(foreign_key="tournament.id", unique=True, ondelete="CASCADE")


class TournamentOpsUpdate(SQLModel):
    teams_per_judge: int = Field(default=2, ge=1)
    hosted_by_dc: bool = False
    notes: str | None = None


class TournamentRegistrationBase(SQLModel):
    teams_registered: int | None = Field(default=None, ge=0)
    judges_promised: int | None = Field(default=None, ge=0)
    # The tab gives teams fielded and judges provided, and the rule gives judges required;
    # these replace them when the tab is missing or wrong.
    teams_fielded_override: int | None = Field(default=None, ge=0)
    judges_provided_override: int | None = Field(default=None, ge=0)
    judges_required_override: int | None = Field(default=None, ge=0)
    dropout_notes: str | None = None
    notes: str | None = None


class TournamentRegistration(TournamentRegistrationBase, table=True):
    """What one school signed up for at one tournament."""

    __table_args__ = (UniqueConstraint("tournament_id", "institution_id"),)

    id: int | None = Field(default=None, primary_key=True)
    tournament_id: int = Field(foreign_key="tournament.id", index=True, ondelete="CASCADE")
    institution_id: int = Field(foreign_key="institution.id", index=True)


class TournamentRegistrationCreate(TournamentRegistrationBase):
    tournament_id: int
    institution_id: int


class TournamentRegistrationPublic(TournamentRegistrationBase):
    id: int
    tournament_id: int
    institution_id: int


class TournamentRegistrationUpdate(SQLModel):
    teams_registered: int | None = Field(default=None, ge=0)
    judges_promised: int | None = Field(default=None, ge=0)
    teams_fielded_override: int | None = Field(default=None, ge=0)
    judges_provided_override: int | None = Field(default=None, ge=0)
    judges_required_override: int | None = Field(default=None, ge=0)
    dropout_notes: str | None = None
    notes: str | None = None
    institution_id: int | None = None
