"""Raw score and participation history queries for debaters and judges, grouped per tournament.

Built on `core.round_scores_with_iron` (tab-normalized scores), `core.participation`
(side/outcome records) and `core.display_name`. Like the tournament stats these are computed
fresh per request. "Counted" scores are those on non-discarded, non-forfeit ballots.
"""

from collections import defaultdict
from datetime import date
from typing import Literal

from sqlmodel import Session, SQLModel, func, select

from app.models import (
    Ballot,
    Debate,
    Debater,
    DebateJudge,
    Round,
    SpeakerPosition,
    SpeakerScore,
    Team,
    TeamMember,
    Tournament,
)
from app.services.stats import core

Stage = Literal["prelim", "elim"]


def _stage(is_elimination: bool) -> str:
    return "elim" if is_elimination else "prelim"


class TournamentRef(SQLModel):
    tournament_id: int
    tournament_name: str
    tournament_abbr: str | None
    tournament_date: date | None


def _tournament_ref(tournament: Tournament) -> TournamentRef:
    return TournamentRef(
        tournament_id=tournament.id,
        tournament_name=tournament.name,
        tournament_abbr=tournament.abbr,
        tournament_date=tournament.date,
    )


def _tournament_order(tournament: Tournament) -> tuple[date, int]:
    return (tournament.date or date.min, tournament.id)


# --- Debater scores ---


class RawScore(SQLModel):
    round_id: int
    round_name: str | None
    round_seq: int
    stage: str
    debate_id: int
    judge_id: int
    side: str
    position: int
    score: float


class NormalizedScore(SQLModel):
    round_id: int
    round_name: str | None
    round_seq: int
    stage: str
    score: float
    is_iron: bool


class TournamentScores(TournamentRef):
    """`raw` is filled in raw mode and `normalized` in normalized mode; the other is empty."""

    raw: list[RawScore] = []
    normalized: list[NormalizedScore] = []


class DebaterScores(SQLModel):
    debater_id: int
    name: str
    normalized: bool
    tournaments: list[TournamentScores]


def debater_scores(
    session: Session,
    debater_id: int | None = None,
    *,
    normalized: bool = False,
    include_replies: bool = False,
) -> list[DebaterScores]:
    """Per-debater score history. `debater_id=None` is the bulk form: every debater with
    counted scores, one pass over the scores (raw) or over each tournament (normalized)."""
    tournaments = {t.id: t for t in session.exec(select(Tournament)).all()}
    by_debater: dict[int, dict[int, TournamentScores]] = defaultdict(dict)

    def counted(statement):
        statement = (
            statement.join(Ballot, SpeakerScore.ballot_id == Ballot.id)
            .join(Debate, Ballot.debate_id == Debate.id)
            .join(Round, Debate.round_id == Round.id)
            .where(
                Ballot.discarded == False,  # noqa: E712
                Ballot.forfeit == False,  # noqa: E712
            )
        )
        if debater_id is not None:
            statement = statement.where(SpeakerScore.debater_id == debater_id)
        return statement

    if normalized:
        tournament_ids = session.exec(counted(select(Round.tournament_id).select_from(SpeakerScore)).distinct()).all()
        round_meta = {r.id: r for r in session.exec(select(Round)).all()}
        for tournament_id in tournament_ids:
            for elimination in (False, True):
                detailed = core.round_scores_with_iron(
                    session, tournament_id, elimination=elimination
                )
                for did, per_round in detailed.items():
                    if debater_id is not None and did != debater_id:
                        continue
                    group = by_debater[did].setdefault(
                        tournament_id,
                        TournamentScores(**_tournament_ref(tournaments[tournament_id]).model_dump()),
                    )
                    for round_id, (score, is_iron) in per_round.items():
                        round_ = round_meta[round_id]
                        group.normalized.append(
                            NormalizedScore(
                                round_id=round_id,
                                round_name=round_.name,
                                round_seq=round_.seq,
                                stage=_stage(round_.isElimination),
                                score=score,
                                is_iron=is_iron,
                            )
                        )
    else:
        query = counted(select(SpeakerScore, Ballot, Debate, Round))
        if not include_replies:
            query = query.where(SpeakerScore.position != SpeakerPosition.REPLY)
        for score, ballot, debate, round_ in session.exec(query).all():
            group = by_debater[score.debater_id].setdefault(
                round_.tournament_id,
                TournamentScores(**_tournament_ref(tournaments[round_.tournament_id]).model_dump()),
            )
            group.raw.append(
                RawScore(
                    round_id=round_.id,
                    round_name=round_.name,
                    round_seq=round_.seq,
                    stage=_stage(round_.isElimination),
                    debate_id=debate.id,
                    judge_id=ballot.judge_id,
                    side=score.side.value,
                    position=int(score.position),
                    score=score.final_score,
                )
            )

    names = {
        d.id: core.display_name(d.full_name, d.first_name, d.last_name)
        for d in session.exec(
            select(Debater).where(Debater.id.in_(by_debater.keys()))
        ).all()
    }
    results = []
    for did in sorted(by_debater):
        groups = sorted(
            by_debater[did].values(),
            key=lambda g: (g.tournament_date or date.min, g.tournament_id),
        )
        for group in groups:
            group.raw.sort(key=lambda s: (s.round_seq, s.debate_id, s.judge_id, s.position))
            group.normalized.sort(key=lambda s: s.round_seq)
        results.append(
            DebaterScores(
                debater_id=did, name=names.get(did, ""), normalized=normalized, tournaments=groups
            )
        )
    return results


# --- Debater debates and teams ---


class DebaterDebate(SQLModel):
    debate_id: int
    round_id: int
    round_name: str | None
    round_seq: int
    stage: str
    team_id: int
    team_name: str
    side: str
    opponent_team_id: int
    opponent_team_name: str
    won: bool | None


class TournamentDebates(TournamentRef):
    debates: list[DebaterDebate]


def debater_debates(
    session: Session, debater_id: int, stage: Stage | None = None
) -> list[TournamentDebates]:
    tournament_ids = set(
        session.exec(
            select(Team.tournament_id)
            .join(TeamMember, TeamMember.team_id == Team.id)
            .where(TeamMember.debater_id == debater_id)
        ).all()
    )
    if not tournament_ids:
        return []

    tournaments = {
        t.id: t
        for t in session.exec(select(Tournament).where(Tournament.id.in_(tournament_ids))).all()
    }
    results: list[TournamentDebates] = []
    for tournament_id in sorted(tournaments, key=lambda tid: _tournament_order(tournaments[tid])):
        records = [
            r
            for r in core.participation(session, tournament_id)
            if r.debater_id == debater_id
            and (stage is None or r.is_elimination == (stage == "elim"))
        ]
        if not records:
            continue
        debates = {
            d.id: d
            for d in session.exec(
                select(Debate).where(Debate.id.in_({r.debate_id for r in records}))
            ).all()
        }
        rounds = {
            r.id: r
            for r in session.exec(
                select(Round).where(Round.id.in_({r.round_id for r in records}))
            ).all()
        }
        team_ids = {debates[r.debate_id].prop_team_id for r in records} | {
            debates[r.debate_id].opp_team_id for r in records
        }
        team_names = {
            t.id: t.name for t in session.exec(select(Team).where(Team.id.in_(team_ids))).all()
        }
        entries = []
        for record in sorted(records, key=lambda r: (r.round_seq, r.debate_id)):
            debate = debates[record.debate_id]
            opponent_id = (
                debate.opp_team_id if debate.prop_team_id == record.team_id else debate.prop_team_id
            )
            entries.append(
                DebaterDebate(
                    debate_id=record.debate_id,
                    round_id=record.round_id,
                    round_name=rounds[record.round_id].name,
                    round_seq=record.round_seq,
                    stage=_stage(record.is_elimination),
                    team_id=record.team_id,
                    team_name=team_names[record.team_id],
                    side=record.side.value,
                    opponent_team_id=opponent_id,
                    opponent_team_name=team_names[opponent_id],
                    won=record.won,
                )
            )
        results.append(
            TournamentDebates(**_tournament_ref(tournaments[tournament_id]).model_dump(), debates=entries)
        )
    return results


class DebaterTeam(TournamentRef):
    team_id: int
    team_name: str
    institution_id: int | None


def debater_teams(session: Session, debater_id: int) -> list[DebaterTeam]:
    rows = session.exec(
        select(Team, Tournament)
        .join(TeamMember, TeamMember.team_id == Team.id)
        .join(Tournament, Team.tournament_id == Tournament.id)
        .where(TeamMember.debater_id == debater_id)
    ).all()
    rows = sorted(rows, key=lambda row: (*_tournament_order(row[1]), row[0].id))
    return [
        DebaterTeam(
            **_tournament_ref(tournament).model_dump(),
            team_id=team.id,
            team_name=team.name,
            institution_id=team.institution_id,
        )
        for team, tournament in rows
    ]


# --- Judge scores and debates ---


class JudgeScore(SQLModel):
    round_id: int
    round_name: str | None
    round_seq: int
    stage: str
    debate_id: int
    debater_id: int
    side: str
    position: int
    score: float


class TournamentJudgeScores(TournamentRef):
    scores: list[JudgeScore]


def judge_scores(
    session: Session, judge_id: int, include_replies: bool = False
) -> list[TournamentJudgeScores]:
    query = (
        select(SpeakerScore, Debate, Round, Tournament)
        .join(Ballot, SpeakerScore.ballot_id == Ballot.id)
        .join(Debate, Ballot.debate_id == Debate.id)
        .join(Round, Debate.round_id == Round.id)
        .join(Tournament, Round.tournament_id == Tournament.id)
        .where(
            Ballot.judge_id == judge_id,
            Ballot.discarded == False,  # noqa: E712
            Ballot.forfeit == False,  # noqa: E712
        )
    )
    if not include_replies:
        query = query.where(SpeakerScore.position != SpeakerPosition.REPLY)

    groups: dict[int, TournamentJudgeScores] = {}
    tournaments: dict[int, Tournament] = {}
    for score, debate, round_, tournament in session.exec(query).all():
        tournaments[tournament.id] = tournament
        group = groups.setdefault(
            tournament.id,
            TournamentJudgeScores(**_tournament_ref(tournament).model_dump(), scores=[]),
        )
        group.scores.append(
            JudgeScore(
                round_id=round_.id,
                round_name=round_.name,
                round_seq=round_.seq,
                stage=_stage(round_.isElimination),
                debate_id=debate.id,
                debater_id=score.debater_id,
                side=score.side.value,
                position=int(score.position),
                score=score.final_score,
            )
        )
    for group in groups.values():
        group.scores.sort(key=lambda s: (s.round_seq, s.debate_id, s.position))
    return [groups[tid] for tid in sorted(groups, key=lambda tid: _tournament_order(tournaments[tid]))]


class JudgeDebate(SQLModel):
    debate_id: int
    round_id: int
    round_name: str | None
    round_seq: int
    stage: str
    is_chair: bool
    is_trainee: bool
    ballot_submitted: bool


class TournamentJudgeDebates(TournamentRef):
    debates: list[JudgeDebate]


def judge_debates(
    session: Session, judge_id: int, stage: Stage | None = None
) -> list[TournamentJudgeDebates]:
    query = (
        select(DebateJudge, Round, Tournament)
        .join(Debate, DebateJudge.debate_id == Debate.id)
        .join(Round, Debate.round_id == Round.id)
        .join(Tournament, Round.tournament_id == Tournament.id)
        .where(DebateJudge.judge_id == judge_id)
    )
    if stage is not None:
        query = query.where(Round.isElimination == (stage == "elim"))
    rows = session.exec(query).all()

    submitted = set(session.exec(select(Ballot.debate_id).where(Ballot.judge_id == judge_id)).all())
    groups: dict[int, TournamentJudgeDebates] = {}
    tournaments: dict[int, Tournament] = {}
    for link, round_, tournament in rows:
        tournaments[tournament.id] = tournament
        group = groups.setdefault(
            tournament.id,
            TournamentJudgeDebates(**_tournament_ref(tournament).model_dump(), debates=[]),
        )
        group.debates.append(
            JudgeDebate(
                debate_id=link.debate_id,
                round_id=round_.id,
                round_name=round_.name,
                round_seq=round_.seq,
                stage=_stage(round_.isElimination),
                is_chair=link.is_chair,
                is_trainee=link.is_trainee,
                ballot_submitted=link.debate_id in submitted,
            )
        )
    for group in groups.values():
        group.debates.sort(key=lambda d: (d.round_seq, d.debate_id))
    return [groups[tid] for tid in sorted(groups, key=lambda tid: _tournament_order(tournaments[tid]))]


# --- Tournament averages ---


class TournamentAverage(TournamentRef):
    average: float | None
    scores: int


def tournament_averages(session: Session) -> list[TournamentAverage]:
    """Per tournament, the mean and count of counted substantive prelim scores."""
    rows = session.exec(
        select(Round.tournament_id, func.avg(SpeakerScore.final_score), func.count())
        .select_from(SpeakerScore)
        .join(Ballot, SpeakerScore.ballot_id == Ballot.id)
        .join(Debate, Ballot.debate_id == Debate.id)
        .join(Round, Debate.round_id == Round.id)
        .where(
            Ballot.discarded == False,  # noqa: E712
            Ballot.forfeit == False,  # noqa: E712
            Round.isElimination == False,  # noqa: E712
            SpeakerScore.position != SpeakerPosition.REPLY,
        )
        .group_by(Round.tournament_id)
    ).all()
    stats = {tournament_id: (avg, count) for tournament_id, avg, count in rows}

    results = []
    for tournament in sorted(session.exec(select(Tournament)).all(), key=_tournament_order):
        avg, count = stats.get(tournament.id, (None, 0))
        results.append(
            TournamentAverage(
                **_tournament_ref(tournament).model_dump(),
                average=round(float(avg), 4) if avg is not None else None,
                scores=count,
            )
        )
    return results
