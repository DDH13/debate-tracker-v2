from collections import defaultdict

from fastapi import APIRouter, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import SQLModel, select

from app.api.deps import SessionDep, get_or_404
from app.models import (
    BPBallot,
    BPBallotCreate,
    BPBallotPublicWithDetail,
    BPBallotTeam,
    BPBallotTeamCreate,
    BPBallotUpdate,
    BPDebate,
    BPDebateJudge,
    BPDebateTeam,
    BPPosition,
    BPSide,
    BPSpeakerScore,
    BPSpeakerScoreCreate,
    TeamMember,
)
from app.services.stats.bp import bp_points_for_rank

router = APIRouter(tags=["bp-ballots"])


def _validate_judge_on_panel(session: SessionDep, debate_id: int, judge_id: int) -> None:
    link = session.exec(
        select(BPDebateJudge).where(
            BPDebateJudge.bp_debate_id == debate_id, BPDebateJudge.judge_id == judge_id
        )
    ).first()
    if link is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Judge {judge_id} is not on the panel for debate {debate_id}",
        )


def _validate_rankings(
    rankings: list[BPBallotTeamCreate],
) -> tuple[dict[BPSide, int] | None, dict[BPSide, bool] | None]:
    """Accepts two mutually-exclusive sheet shapes: a full 1-4 ranking (prelim-style), or
    an advance/eliminate flag per side (elimination rounds where Tabbycat itself only
    records who went through, not a full placement). Returns `(rank_by_side, None)` or
    `(None, advanced_by_side)`."""
    if len(rankings) != 4:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="A ranking sheet must have exactly 4 rows (one per side)",
        )
    sides = {r.side for r in rankings}
    if len(sides) != 4:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Each of OG/OO/CG/CO must appear exactly once",
        )

    has_rank = all(r.rank is not None for r in rankings)
    has_advanced = all(r.advanced is not None for r in rankings)

    if has_rank and not any(r.advanced is not None for r in rankings):
        if sorted(r.rank for r in rankings) != [1, 2, 3, 4]:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="rank values must be a permutation of 1-4",
            )
        return {r.side: r.rank for r in rankings}, None

    if has_advanced and not any(r.rank is not None for r in rankings):
        if not any(r.advanced for r in rankings):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="at least one side must have advanced=true",
            )
        return None, {r.side: r.advanced for r in rankings}

    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail="each row must set exactly one of rank (all 4, a 1-4 permutation) or advanced (all 4)",
    )


def _validate_debater_on_side(
    session: SessionDep, debate_id: int, score: BPSpeakerScoreCreate
) -> None:
    team_row = session.exec(
        select(BPDebateTeam).where(
            BPDebateTeam.bp_debate_id == debate_id, BPDebateTeam.side == score.side
        )
    ).first()
    if team_row is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"No team on side {score.side.value} for this debate",
        )
    member = session.exec(
        select(TeamMember).where(
            TeamMember.team_id == team_row.team_id, TeamMember.debater_id == score.debater_id
        )
    ).first()
    if member is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Debater {score.debater_id} is not a member of the {score.side.value} team",
        )


def _set_bp_ballot_scores(
    session: SessionDep,
    ballot: BPBallot,
    scores: list[BPSpeakerScoreCreate],
) -> None:
    if len(scores) != 8:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="A BP score sheet must have exactly 8 rows (positions 1-2 for all four sides)",
        )
    for score in scores:
        _validate_debater_on_side(session, ballot.bp_debate_id, score)

    for existing in list(ballot.scores):
        session.delete(existing)
    session.flush()

    rows = [
        BPSpeakerScore.model_validate(score, update={"bp_ballot_id": ballot.id}) for score in scores
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

    for side in BPSide:
        debaters = {row.debater_id for row in rows if row.side == side}
        if len(debaters) != 2:
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"{side.value} side must have 2 distinct debaters across positions 1-2",
            )


def _recompute_bp_debate_result(session: SessionDep, bp_debate: BPDebate) -> None:
    """Sum each side's rank across non-discarded, fully-ranked ballots, re-rank ascending
    (lowest total wins), and write `rank`/`points` onto the `BPDebateTeam` rows. If no
    ballot has a full ranking but some record advance/eliminate instead (elimination
    rounds where only who-went-through is tracked), majority-vote `advanced` per side and
    write that instead, leaving `rank`/`points` `None`. Mirrors
    `_recompute_bp_debate_result` in app/services/tabbycat.py (kept separate since
    services shouldn't import from the API layer)."""
    ballots = session.exec(select(BPBallot).where(BPBallot.bp_debate_id == bp_debate.id)).all()
    counted_ballot_ids = [b.id for b in ballots if not b.discarded]
    if not counted_ballot_ids:
        return

    rankings = session.exec(
        select(BPBallotTeam).where(BPBallotTeam.bp_ballot_id.in_(counted_ballot_ids))
    ).all()
    debate_teams = session.exec(
        select(BPDebateTeam).where(BPDebateTeam.bp_debate_id == bp_debate.id)
    ).all()

    total_by_side: dict[BPSide, int] = defaultdict(int)
    for ranking in rankings:
        if ranking.rank is not None:
            total_by_side[ranking.side] += ranking.rank
    if len(total_by_side) == 4:
        rank_by_side = {
            side: rank
            for rank, (side, _total) in enumerate(sorted(total_by_side.items(), key=lambda kv: kv[1]), start=1)
        }
        for debate_team in debate_teams:
            rank = rank_by_side.get(debate_team.side)
            if rank is not None:
                debate_team.rank = rank
                debate_team.points = bp_points_for_rank(rank)
                session.add(debate_team)
        return

    votes_by_side: dict[BPSide, list[bool]] = defaultdict(list)
    for ranking in rankings:
        if ranking.advanced is not None:
            votes_by_side[ranking.side].append(ranking.advanced)
    for debate_team in debate_teams:
        votes = votes_by_side.get(debate_team.side)
        if votes:
            debate_team.advanced = sum(votes) > len(votes) / 2
            session.add(debate_team)


@router.post(
    "/bp-debates/{debate_id}/ballots",
    response_model=BPBallotPublicWithDetail,
    status_code=status.HTTP_201_CREATED,
)
def create_bp_ballot(debate_id: int, ballot_in: BPBallotCreate, session: SessionDep) -> BPBallot:
    debate = get_or_404(session, BPDebate, debate_id)
    _validate_judge_on_panel(session, debate_id, ballot_in.judge_id)
    rank_by_side, advanced_by_side = _validate_rankings(ballot_in.rankings)

    ballot = BPBallot(bp_debate_id=debate_id, judge_id=ballot_in.judge_id)
    session.add(ballot)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Judge {ballot_in.judge_id} has already submitted a ballot for this debate",
        ) from exc

    if rank_by_side is not None:
        for side, rank in rank_by_side.items():
            session.add(BPBallotTeam(bp_ballot_id=ballot.id, side=side, rank=rank))
    else:
        for side, advanced in advanced_by_side.items():
            session.add(BPBallotTeam(bp_ballot_id=ballot.id, side=side, advanced=advanced))
    session.flush()

    if ballot_in.scores is not None:
        _set_bp_ballot_scores(session, ballot, ballot_in.scores)

    _recompute_bp_debate_result(session, debate)
    session.commit()
    session.refresh(ballot)
    return ballot


@router.get("/bp-debates/{debate_id}/ballots", response_model=list[BPBallotPublicWithDetail])
def list_bp_ballots(debate_id: int, session: SessionDep) -> list[BPBallot]:
    get_or_404(session, BPDebate, debate_id)
    return session.exec(select(BPBallot).where(BPBallot.bp_debate_id == debate_id)).all()


@router.get("/bp-ballots/{ballot_id}", response_model=BPBallotPublicWithDetail)
def get_bp_ballot(ballot_id: int, session: SessionDep) -> BPBallot:
    return get_or_404(session, BPBallot, ballot_id)


@router.patch("/bp-ballots/{ballot_id}", response_model=BPBallotPublicWithDetail)
def update_bp_ballot(ballot_id: int, ballot_in: BPBallotUpdate, session: SessionDep) -> BPBallot:
    ballot = get_or_404(session, BPBallot, ballot_id)
    debate = get_or_404(session, BPDebate, ballot.bp_debate_id)
    ballot.sqlmodel_update(ballot_in.model_dump(exclude_unset=True))
    session.add(ballot)
    _recompute_bp_debate_result(session, debate)
    session.commit()
    session.refresh(ballot)
    return ballot


@router.delete("/bp-ballots/{ballot_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_bp_ballot(ballot_id: int, session: SessionDep) -> None:
    ballot = get_or_404(session, BPBallot, ballot_id)
    debate = get_or_404(session, BPDebate, ballot.bp_debate_id)
    session.delete(ballot)
    session.flush()
    _recompute_bp_debate_result(session, debate)
    session.commit()


@router.put("/bp-ballots/{ballot_id}/scores", response_model=BPBallotPublicWithDetail)
def replace_bp_ballot_scores(
    ballot_id: int, scores_in: list[BPSpeakerScoreCreate], session: SessionDep
) -> BPBallot:
    ballot = get_or_404(session, BPBallot, ballot_id)
    _set_bp_ballot_scores(session, ballot, scores_in)
    session.commit()
    session.refresh(ballot)
    return ballot


# --- Result ---


class BPBallotRanking(SQLModel):
    side: BPSide
    rank: int | None
    advanced: bool | None


class BPBallotVerdict(SQLModel):
    ballot_id: int
    judge_id: int
    rankings: list[BPBallotRanking]


class BPTeamResult(SQLModel):
    team_id: int
    side: BPSide
    rank: int | None
    points: int | None
    advanced: bool | None


class BPSpeakerAverage(SQLModel):
    debater_id: int
    side: BPSide
    position: BPPosition
    average_score: float


class BPDebateResult(SQLModel):
    bp_debate_id: int
    teams: list[BPTeamResult]
    ballots: list[BPBallotVerdict]
    speakers: list[BPSpeakerAverage]


@router.get("/bp-debates/{debate_id}/result", response_model=BPDebateResult)
def get_bp_debate_result(debate_id: int, session: SessionDep) -> BPDebateResult:
    get_or_404(session, BPDebate, debate_id)
    debate_teams = session.exec(
        select(BPDebateTeam).where(BPDebateTeam.bp_debate_id == debate_id)
    ).all()
    teams = [
        BPTeamResult(team_id=dt.team_id, side=dt.side, rank=dt.rank, points=dt.points, advanced=dt.advanced)
        for dt in debate_teams
    ]

    ballots = session.exec(select(BPBallot).where(BPBallot.bp_debate_id == debate_id)).all()
    rankings_by_ballot: dict[int, list[BPBallotTeam]] = defaultdict(list)
    if ballots:
        ballot_ids = [b.id for b in ballots]
        for ranking in session.exec(
            select(BPBallotTeam).where(BPBallotTeam.bp_ballot_id.in_(ballot_ids))
        ).all():
            rankings_by_ballot[ranking.bp_ballot_id].append(ranking)

    verdicts = [
        BPBallotVerdict(
            ballot_id=b.id,
            judge_id=b.judge_id,
            rankings=[
                BPBallotRanking(side=r.side, rank=r.rank, advanced=r.advanced)
                for r in sorted(
                    rankings_by_ballot.get(b.id, []),
                    key=lambda r: (r.rank if r.rank is not None else 0, r.side.value),
                )
            ],
        )
        for b in ballots
    ]

    scores = session.exec(
        select(BPSpeakerScore)
        .join(BPBallot, BPSpeakerScore.bp_ballot_id == BPBallot.id)
        .where(BPBallot.bp_debate_id == debate_id)
    ).all()
    grouped: dict[tuple[int, BPSide, BPPosition], list[float]] = {}
    for score in scores:
        key = (score.debater_id, score.side, score.position)
        grouped.setdefault(key, []).append(score.final_score)

    speakers = [
        BPSpeakerAverage(
            debater_id=debater_id,
            side=side,
            position=position,
            average_score=sum(values) / len(values),
        )
        for (debater_id, side, position), values in grouped.items()
    ]
    speakers.sort(key=lambda s: (s.side, s.position, s.debater_id))

    return BPDebateResult(bp_debate_id=debate_id, teams=teams, ballots=verdicts, speakers=speakers)
