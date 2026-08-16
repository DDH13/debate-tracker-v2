from typing import TYPE_CHECKING

from sqlmodel import Field, Relationship, SQLModel, UniqueConstraint

from app.models.enums import BPSide

if TYPE_CHECKING:
    from app.models.bp_ballot import BPBallot
    from app.models.bp_debate_judge import BPDebateJudge
    from app.models.round import Round
    from app.models.team import Team


class BPDebateBase(SQLModel):
    room: str | None = None


class BPDebate(BPDebateBase, table=True):
    id: int | None = Field(default=None, primary_key=True)
    round_id: int = Field(foreign_key="round.id")

    round: "Round" = Relationship(back_populates="bp_debates")
    teams: list["BPDebateTeam"] = Relationship(
        back_populates="bp_debate",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )
    judge_links: list["BPDebateJudge"] = Relationship(
        back_populates="bp_debate",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )
    ballots: list["BPBallot"] = Relationship(
        back_populates="bp_debate",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


class BPDebateTeamBase(SQLModel):
    side: BPSide
    rank: int | None = None
    points: int | None = None
    # Set instead of rank/points for elimination debates where Tabbycat only records
    # advance/eliminate (no full 1-4 ranking) — see BPBallotTeam.advanced.
    advanced: bool | None = None


class BPDebateTeam(BPDebateTeamBase, table=True):
    __table_args__ = (
        UniqueConstraint("bp_debate_id", "side"),
        UniqueConstraint("bp_debate_id", "team_id"),
    )

    id: int | None = Field(default=None, primary_key=True)
    bp_debate_id: int = Field(foreign_key="bpdebate.id")
    team_id: int = Field(foreign_key="team.id")

    bp_debate: "BPDebate" = Relationship(back_populates="teams")
    team: "Team" = Relationship(back_populates="bp_debate_links")


class BPDebateTeamCreate(SQLModel):
    team_id: int
    side: BPSide


class BPDebateTeamPublic(BPDebateTeamBase):
    id: int
    bp_debate_id: int
    team_id: int


class BPDebateCreate(SQLModel):
    room: str | None = None
    teams: list[BPDebateTeamCreate]


class BPDebatePublic(BPDebateBase):
    id: int
    round_id: int


class BPDebateUpdate(SQLModel):
    room: str | None = None
