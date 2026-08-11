from fastapi import APIRouter, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import SQLModel, select

from app.api.deps import SessionDep, get_or_404
from app.models import (
    Ballot,
    BallotCreate,
    BallotPublicWithScores,
    BallotUpdate,
    Debate,
    DebateJudge,
    DebateJudgeCreate,
    DebateJudgePublic,
    Judge,
    Side,
    SpeakerPosition,
    SpeakerScore,
    SpeakerScoreCreate,
    TeamMember,
)

router = APIRouter(tags=["ballots"])


def _validate_judge_on_panel(session: SessionDep, debate_id: int, judge_id: int) -> None:
    link = session.exec(
        select(DebateJudge).where(
            DebateJudge.debate_id == debate_id, DebateJudge.judge_id == judge_id
        )
    ).first()
    if link is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Judge {judge_id} is not on the panel for debate {debate_id}",
        )


def _validate_debater_on_side(
    session: SessionDep, debate: Debate, score: SpeakerScoreCreate
) -> None:
    team_id = debate.prop_team_id if score.side == Side.PROP else debate.opp_team_id
    member = session.exec(
        select(TeamMember).where(
            TeamMember.team_id == team_id, TeamMember.debater_id == score.debater_id
        )
    ).first()
    if member is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Debater {score.debater_id} is not a member of the {score.side.value} team",
        )


def _set_ballot_scores(
    session: SessionDep,
    ballot: Ballot,
    debate: Debate,
    scores: list[SpeakerScoreCreate],
) -> None:
    if len(scores) != 8:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="A score sheet must have exactly 8 rows (positions 1-4 for both sides)",
        )
    for score in scores:
        _validate_debater_on_side(session, debate, score)

    for existing in list(ballot.scores):
        session.delete(existing)
    session.flush()

    rows = [
        SpeakerScore.model_validate(score, update={"ballot_id": ballot.id}) for score in scores
    ]
    session.add_all(rows)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Duplicate (side, position) row on this ballot",
        ) from exc

    for side in (Side.PROP, Side.OPP):
        substantive_debaters = {
            row.debater_id
            for row in rows
            if row.side == side and row.position != SpeakerPosition.REPLY
        }
        if len(substantive_debaters) != 3:
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"{side.value} side must have 3 distinct debaters across positions 1-3",
            )


def _recompute_debate_winner(session: SessionDep, debate: Debate) -> None:
    ballots = session.exec(select(Ballot).where(Ballot.debate_id == debate.id)).all()
    prop_votes = sum(1 for b in ballots if b.winner == Side.PROP)
    opp_votes = sum(1 for b in ballots if b.winner == Side.OPP)
    if prop_votes > opp_votes:
        debate.winner = Side.PROP
        session.add(debate)
    elif opp_votes > prop_votes:
        debate.winner = Side.OPP
        session.add(debate)


# --- Panel allocation ---


@router.post(
    "/debates/{debate_id}/judges",
    response_model=DebateJudgePublic,
    status_code=status.HTTP_201_CREATED,
)
def add_debate_judge(
    debate_id: int, judge_in: DebateJudgeCreate, session: SessionDep
) -> DebateJudge:
    get_or_404(session, Debate, debate_id)
    get_or_404(session, Judge, judge_in.judge_id)
    link = DebateJudge.model_validate(judge_in, update={"debate_id": debate_id})
    session.add(link)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Judge is already on this panel",
        ) from exc
    session.refresh(link)
    return link


@router.get("/debates/{debate_id}/judges", response_model=list[DebateJudgePublic])
def list_debate_judges(debate_id: int, session: SessionDep) -> list[DebateJudge]:
    get_or_404(session, Debate, debate_id)
    return session.exec(select(DebateJudge).where(DebateJudge.debate_id == debate_id)).all()


@router.delete(
    "/debates/{debate_id}/judges/{judge_id}", status_code=status.HTTP_204_NO_CONTENT
)
def remove_debate_judge(debate_id: int, judge_id: int, session: SessionDep) -> None:
    get_or_404(session, Debate, debate_id)
    link = session.exec(
        select(DebateJudge).where(
            DebateJudge.debate_id == debate_id, DebateJudge.judge_id == judge_id
        )
    ).first()
    if link is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Judge {judge_id} is not on the panel for debate {debate_id}",
        )
    ballot = session.exec(
        select(Ballot).where(Ballot.debate_id == debate_id, Ballot.judge_id == judge_id)
    ).first()
    if ballot is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot remove judge from panel: they have submitted a ballot",
        )
    session.delete(link)
    session.commit()


# --- Ballots ---


@router.post(
    "/debates/{debate_id}/ballots",
    response_model=BallotPublicWithScores,
    status_code=status.HTTP_201_CREATED,
)
def create_ballot(debate_id: int, ballot_in: BallotCreate, session: SessionDep) -> Ballot:
    debate = get_or_404(session, Debate, debate_id)
    _validate_judge_on_panel(session, debate_id, ballot_in.judge_id)

    ballot = Ballot(debate_id=debate_id, judge_id=ballot_in.judge_id, winner=ballot_in.winner)
    session.add(ballot)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Judge {ballot_in.judge_id} has already submitted a ballot for this debate",
        ) from exc

    if ballot_in.scores is not None:
        _set_ballot_scores(session, ballot, debate, ballot_in.scores)

    _recompute_debate_winner(session, debate)
    session.commit()
    session.refresh(ballot)
    return ballot


@router.get("/debates/{debate_id}/ballots", response_model=list[BallotPublicWithScores])
def list_ballots(debate_id: int, session: SessionDep) -> list[Ballot]:
    get_or_404(session, Debate, debate_id)
    return session.exec(select(Ballot).where(Ballot.debate_id == debate_id)).all()


@router.get("/ballots/{ballot_id}", response_model=BallotPublicWithScores)
def get_ballot(ballot_id: int, session: SessionDep) -> Ballot:
    return get_or_404(session, Ballot, ballot_id)


@router.patch("/ballots/{ballot_id}", response_model=BallotPublicWithScores)
def update_ballot(ballot_id: int, ballot_in: BallotUpdate, session: SessionDep) -> Ballot:
    ballot = get_or_404(session, Ballot, ballot_id)
    debate = get_or_404(session, Debate, ballot.debate_id)
    ballot.sqlmodel_update(ballot_in.model_dump(exclude_unset=True))
    session.add(ballot)
    _recompute_debate_winner(session, debate)
    session.commit()
    session.refresh(ballot)
    return ballot


@router.delete("/ballots/{ballot_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_ballot(ballot_id: int, session: SessionDep) -> None:
    ballot = get_or_404(session, Ballot, ballot_id)
    debate = get_or_404(session, Debate, ballot.debate_id)
    session.delete(ballot)
    session.flush()
    _recompute_debate_winner(session, debate)
    session.commit()


@router.put("/ballots/{ballot_id}/scores", response_model=BallotPublicWithScores)
def replace_ballot_scores(
    ballot_id: int, scores_in: list[SpeakerScoreCreate], session: SessionDep
) -> Ballot:
    ballot = get_or_404(session, Ballot, ballot_id)
    debate = get_or_404(session, Debate, ballot.debate_id)
    _set_ballot_scores(session, ballot, debate, scores_in)
    session.commit()
    session.refresh(ballot)
    return ballot


# --- Result ---


class BallotVerdict(SQLModel):
    ballot_id: int
    judge_id: int
    winner: Side


class SpeakerAverage(SQLModel):
    debater_id: int
    side: Side
    position: SpeakerPosition
    average_score: float


class DebateResult(SQLModel):
    debate_id: int
    winner: Side | None
    ballots: list[BallotVerdict]
    speakers: list[SpeakerAverage]


@router.get("/debates/{debate_id}/result", response_model=DebateResult)
def get_debate_result(debate_id: int, session: SessionDep) -> DebateResult:
    debate = get_or_404(session, Debate, debate_id)
    ballots = session.exec(select(Ballot).where(Ballot.debate_id == debate_id)).all()
    verdicts = [
        BallotVerdict(ballot_id=b.id, judge_id=b.judge_id, winner=b.winner) for b in ballots
    ]

    scores = session.exec(
        select(SpeakerScore)
        .join(Ballot, SpeakerScore.ballot_id == Ballot.id)
        .where(Ballot.debate_id == debate_id)
    ).all()

    grouped: dict[tuple[int, Side, SpeakerPosition], list[float]] = {}
    for score in scores:
        key = (score.debater_id, score.side, score.position)
        grouped.setdefault(key, []).append(score.score)

    speakers = [
        SpeakerAverage(
            debater_id=debater_id,
            side=side,
            position=position,
            average_score=sum(values) / len(values),
        )
        for (debater_id, side, position), values in grouped.items()
    ]
    speakers.sort(key=lambda s: (s.side, s.position, s.debater_id))

    return DebateResult(
        debate_id=debate_id, winner=debate.winner, ballots=verdicts, speakers=speakers
    )
