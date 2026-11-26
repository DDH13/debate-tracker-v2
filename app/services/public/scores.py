"""Per-debate margins and panel votes. Each is one grouped query that leaves out discarded
ballots (and, for margins, forfeits, which carry no scores)."""

from collections import defaultdict
from collections.abc import Collection

from sqlmodel import Session, func, select

from app.models import Ballot, Debate, Round, Side, SpeakerScore


def debate_margins(
    session: Session,
    *,
    debate_ids: Collection[int] | None = None,
    tournament_id: int | None = None,
) -> dict[int, float]:
    """{debate_id: panel-averaged (prop team total - opp team total)}. Positive favours
    Proposition. Debates with no counted scores are absent."""
    if debate_ids is not None and not debate_ids:
        return {}
    statement = (
        select(Ballot.debate_id, Ballot.id, SpeakerScore.side, func.sum(SpeakerScore.final_score))
        .select_from(SpeakerScore)
        .join(Ballot, SpeakerScore.ballot_id == Ballot.id)
        .where(Ballot.discarded == False, Ballot.forfeit == False)  # noqa: E712
        .group_by(Ballot.debate_id, Ballot.id, SpeakerScore.side)
    )
    if debate_ids is not None:
        statement = statement.where(Ballot.debate_id.in_(list(debate_ids)))
    if tournament_id is not None:
        statement = (
            statement.join(Debate, Ballot.debate_id == Debate.id)
            .join(Round, Debate.round_id == Round.id)
            .where(Round.tournament_id == tournament_id)
        )
    per_ballot: dict[tuple[int, int], float] = defaultdict(float)
    for debate_id, ballot_id, side, total in session.exec(statement).all():
        per_ballot[(debate_id, ballot_id)] += float(total) if side == Side.PROP else -float(total)
    by_debate: dict[int, list[float]] = defaultdict(list)
    for (debate_id, _ballot_id), diff in per_ballot.items():
        by_debate[debate_id].append(diff)
    return {d: round(sum(v) / len(v), 4) for d, v in by_debate.items()}


def debate_votes(
    session: Session,
    *,
    debate_ids: Collection[int] | None = None,
    tournament_id: int | None = None,
) -> dict[int, tuple[int, int]]:
    """{debate_id: (prop votes, opp votes)} over non-discarded ballots."""
    if debate_ids is not None and not debate_ids:
        return {}
    statement = (
        select(Ballot.debate_id, Ballot.winner, func.count())
        .where(Ballot.discarded == False)  # noqa: E712
        .group_by(Ballot.debate_id, Ballot.winner)
    )
    if debate_ids is not None:
        statement = statement.where(Ballot.debate_id.in_(list(debate_ids)))
    if tournament_id is not None:
        statement = (
            statement.join(Debate, Ballot.debate_id == Debate.id)
            .join(Round, Debate.round_id == Round.id)
            .where(Round.tournament_id == tournament_id)
        )
    votes: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    for debate_id, winner, count in session.exec(statement).all():
        votes[debate_id][0 if winner == Side.PROP else 1] += count
    return {d: (v[0], v[1]) for d, v in votes.items()}
