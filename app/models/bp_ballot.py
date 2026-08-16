from typing import TYPE_CHECKING

from sqlmodel import CheckConstraint, Field, Relationship, SQLModel, UniqueConstraint

from app.models.bp_speaker_score import BPSpeakerScoreCreate, BPSpeakerScorePublic
from app.models.enums import BPSide

if TYPE_CHECKING:
    from app.models.bp_debate import BPDebate
    from app.models.bp_speaker_score import BPSpeakerScore
    from app.models.judge import Judge


class BPBallot(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("bp_debate_id", "judge_id"),)

    id: int | None = Field(default=None, primary_key=True)
    bp_debate_id: int = Field(foreign_key="bpdebate.id")
    judge_id: int = Field(foreign_key="judge.id")
    discarded: bool = False
    forfeit: bool = False

    bp_debate: "BPDebate" = Relationship(back_populates="ballots")
    judge: "Judge" = Relationship(back_populates="bp_ballots")
    rankings: list["BPBallotTeam"] = Relationship(
        back_populates="ballot",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )
    scores: list["BPSpeakerScore"] = Relationship(
        back_populates="ballot",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


class BPBallotTeamBase(SQLModel):
    side: BPSide
    # Prelim-style sheets set `rank` (a 1-4 permutation across the four rows). Some
    # elimination sheets report only advance/eliminate — no full ranking — in which case
    # `advanced` is set instead and `rank` stays None. Exactly one of the two is set.
    rank: int | None = None
    advanced: bool | None = None


class BPBallotTeam(BPBallotTeamBase, table=True):
    __table_args__ = (
        UniqueConstraint("bp_ballot_id", "side"),
        UniqueConstraint("bp_ballot_id", "rank"),
        CheckConstraint("rank IS NOT NULL OR advanced IS NOT NULL"),
    )

    id: int | None = Field(default=None, primary_key=True)
    bp_ballot_id: int = Field(foreign_key="bpballot.id")

    ballot: "BPBallot" = Relationship(back_populates="rankings")


class BPBallotTeamCreate(BPBallotTeamBase):
    pass


class BPBallotTeamPublic(BPBallotTeamBase):
    id: int
    bp_ballot_id: int


class BPBallotCreate(SQLModel):
    judge_id: int
    rankings: list[BPBallotTeamCreate]
    scores: list[BPSpeakerScoreCreate] | None = None


class BPBallotPublic(SQLModel):
    id: int
    bp_debate_id: int
    judge_id: int
    discarded: bool = False
    forfeit: bool = False


class BPBallotPublicWithDetail(BPBallotPublic):
    rankings: list[BPBallotTeamPublic]
    scores: list[BPSpeakerScorePublic]


class BPBallotUpdate(SQLModel):
    discarded: bool | None = None
    forfeit: bool | None = None
