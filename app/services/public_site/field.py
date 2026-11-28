"""Field means: the cached "everyone else" figures behind every "vs field" delta.

Debater and judge means come from the materialized profiles (so they are empty until
`POST /stats/refresh` has run); position means, side rates and institution aggregates are
computed with a handful of grouped queries. All results are plain values held in the TTL cache.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import case
from sqlalchemy.orm import aliased
from sqlmodel import Session, func, select

from app.models import (
    Ballot,
    Debate,
    Debater,
    DebaterProfile,
    Judge,
    JudgeProfile,
    Round,
    Side,
    SpeakerPosition,
    SpeakerScore,
    Team,
)
from app.services.public_site import cache
from app.services.public_site.schools import latest_team_school, school_id
from app.services.public_site.snapshots import elim_rounds_by_tournament, team_finishes
from app.services.public_site.brackets import derive_brackets

MIN_DEBATER_SPEECHES = 3
MIN_JUDGE_BALLOTS = 5
MIN_INSTITUTION_DEBATES = 10


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


@dataclass
class FieldMeans:
    profile_computed_at: datetime | None = None
    debaters: int = 0
    judges: int = 0
    debater_average: float | None = None
    debater_stdev: float | None = None
    debater_prelim_win_rate: float | None = None
    debater_content: float | None = None
    debater_style: float | None = None
    debater_strategy: float | None = None
    judge_average: float | None = None
    judge_stdev: float | None = None
    judge_dissent_rate: float | None = None
    judge_sentiment: float | None = None
    judge_leniency: float | None = None
    judge_harshness: float | None = None
    judge_first: float | None = None
    judge_second: float | None = None
    judge_third: float | None = None
    judge_reply: float | None = None
    # Share of decided debates won by Proposition, and of non-discarded ballots cast for it.
    prop_win_rate: float | None = None
    ballot_prop_rate: float | None = None
    # (side, position) -> mean prelim score, from counted ballots.
    position_means: dict[tuple[str, int], float] = field(default_factory=dict)
    # (position 1-3, category) -> mean prelim mark, one panel mean per speech.
    position_category_means: dict[tuple[int, str], float] = field(default_factory=dict)
    # category -> mean mark in won debates minus mean in lost ones, over prelim speeches.
    category_result_gap: dict[str, float] = field(default_factory=dict)


def _build_field_means(session: Session) -> FieldMeans:
    means = FieldMeans()

    debater_profiles = session.exec(
        select(DebaterProfile).where(DebaterProfile.total_speeches >= MIN_DEBATER_SPEECHES)
    ).all()
    any_profile = session.exec(select(func.max(DebaterProfile.computed_at))).one()
    means.profile_computed_at = any_profile
    means.debaters = len(debater_profiles)
    means.debater_average = _mean([p.average_speaker_score for p in debater_profiles if p.average_speaker_score is not None])
    means.debater_stdev = _mean([p.speaker_score_stdev for p in debater_profiles if p.speaker_score_stdev is not None])
    means.debater_prelim_win_rate = _mean([p.win_rate_prelims for p in debater_profiles if p.win_rate_prelims is not None])
    means.debater_content = _mean([p.average_content for p in debater_profiles if p.average_content is not None])
    means.debater_style = _mean([p.average_style for p in debater_profiles if p.average_style is not None])
    means.debater_strategy = _mean([p.average_strategy for p in debater_profiles if p.average_strategy is not None])

    judge_profiles = session.exec(
        select(JudgeProfile).where(JudgeProfile.ballots_cast >= MIN_JUDGE_BALLOTS)
    ).all()
    means.judges = len(judge_profiles)

    def jmean(attr: str) -> float | None:
        return _mean([getattr(p, attr) for p in judge_profiles if getattr(p, attr) is not None])

    means.judge_average = jmean("average_substantive")
    means.judge_stdev = jmean("score_stdev")
    means.judge_dissent_rate = jmean("dissent_rate")
    means.judge_sentiment = jmean("overall_sentiment")
    means.judge_leniency = jmean("leniency")
    means.judge_harshness = jmean("harshness")
    means.judge_first = jmean("average_first")
    means.judge_second = jmean("average_second")
    means.judge_third = jmean("average_third")
    means.judge_reply = jmean("average_reply")

    wins = dict(
        session.exec(
            select(Debate.winner, func.count()).where(Debate.winner.is_not(None)).group_by(Debate.winner)
        ).all()
    )
    decided = sum(wins.values())
    means.prop_win_rate = wins.get(Side.PROP, 0) / decided * 100 if decided else None
    votes = dict(
        session.exec(
            select(Ballot.winner, func.count()).where(Ballot.discarded == False).group_by(Ballot.winner)  # noqa: E712
        ).all()
    )
    cast = sum(votes.values())
    means.ballot_prop_rate = votes.get(Side.PROP, 0) / cast * 100 if cast else None

    for side, position, avg in session.exec(
        select(SpeakerScore.side, SpeakerScore.position, func.avg(SpeakerScore.final_score))
        .join(Ballot, SpeakerScore.ballot_id == Ballot.id)
        .join(Debate, Ballot.debate_id == Debate.id)
        .join(Round, Debate.round_id == Round.id)
        .where(
            Ballot.discarded == False,  # noqa: E712
            Ballot.forfeit == False,  # noqa: E712
            Round.isElimination == False,  # noqa: E712
        )
        .group_by(SpeakerScore.side, SpeakerScore.position)
    ).all():
        means.position_means[(side.value, int(position))] = round(float(avg), 4)
    _category_field_means(session, means)
    return means


def _category_field_means(session: Session, means: FieldMeans) -> None:
    """Fills the content / style / strategy figures from one grouped query: one panel mean
    per prelim substantive speech, tagged with its position and whether its side won."""
    by_position: dict[int, list[tuple[float, float, float]]] = defaultdict(list)
    results: dict[bool, list[tuple[float, float, float]]] = {True: [], False: []}
    for position, side, winner, content, style, strategy in session.exec(
        select(
            SpeakerScore.position,
            SpeakerScore.side,
            Debate.winner,
            func.avg(SpeakerScore.content),
            func.avg(SpeakerScore.style),
            func.avg(SpeakerScore.strategy),
        )
        .join(Ballot, SpeakerScore.ballot_id == Ballot.id)
        .join(Debate, Ballot.debate_id == Debate.id)
        .join(Round, Debate.round_id == Round.id)
        .where(
            Ballot.discarded == False,  # noqa: E712
            Ballot.forfeit == False,  # noqa: E712
            Round.isElimination == False,  # noqa: E712
            SpeakerScore.position != SpeakerPosition.REPLY,
            SpeakerScore.content.is_not(None),
            SpeakerScore.style.is_not(None),
            SpeakerScore.strategy.is_not(None),
        )
        .group_by(SpeakerScore.debater_id, Ballot.debate_id, SpeakerScore.position, SpeakerScore.side, Debate.winner)
    ).all():
        marks = (float(content), float(style), float(strategy))
        by_position[int(position)].append(marks)
        if winner is not None:
            results[winner == side].append(marks)
    names = ("content", "style", "strategy")
    for position, rows in by_position.items():
        for i, name in enumerate(names):
            means.position_category_means[(position, name)] = round(sum(r[i] for r in rows) / len(rows), 4)
    if results[True] and results[False]:
        for i, name in enumerate(names):
            won = sum(r[i] for r in results[True]) / len(results[True])
            lost = sum(r[i] for r in results[False]) / len(results[False])
            means.category_result_gap[name] = round(won - lost, 4)


def field_means(session: Session) -> FieldMeans:
    return cache.cached("field_means", lambda: _build_field_means(session))


# --- Institutions ---


@dataclass
class InstitutionAggregate:
    institution_id: int
    teams: int = 0
    tournaments: int = 0
    debaters: int = 0
    judges: int = 0
    wins: int = 0
    losses: int = 0
    speaks_sum: float = 0.0
    speaks_count: int = 0
    breaks: int = 0
    finals: int = 0
    titles: int = 0

    @property
    def win_rate(self) -> float | None:
        total = self.wins + self.losses
        return self.wins / total * 100 if total else None

    @property
    def mean_speaks(self) -> float | None:
        return self.speaks_sum / self.speaks_count if self.speaks_count else None


@dataclass
class InstitutionAggregates:
    by_id: dict[int, InstitutionAggregate]
    field_win_rate: float | None
    field_mean_speaks: float | None
    field_breaks: float | None


def _build_institution_aggregates(session: Session) -> InstitutionAggregates:
    by_id: dict[int, InstitutionAggregate] = {}

    def agg(institution_id: int) -> InstitutionAggregate:
        if institution_id not in by_id:
            by_id[institution_id] = InstitutionAggregate(institution_id)
        return by_id[institution_id]

    for iid, teams, tournaments in session.exec(
        select(Team.institution_id, func.count(Team.id), func.count(func.distinct(Team.tournament_id)))
        .where(Team.institution_id.is_not(None))
        .group_by(Team.institution_id)
    ).all():
        agg(iid).teams = teams
        agg(iid).tournaments = tournaments
    latest = latest_team_school()
    effective = school_id(latest)
    for iid, count in session.exec(
        select(effective, func.count())
        .select_from(Debater)
        .join(latest, latest.c.debater_id == Debater.id, isouter=True)
        .where(effective.is_not(None))
        .group_by(effective)
    ).all():
        agg(iid).debaters = count
    for iid, count in session.exec(
        select(Judge.institution_id, func.count()).where(Judge.institution_id.is_not(None)).group_by(Judge.institution_id)
    ).all():
        agg(iid).judges = count

    prop_team, opp_team = aliased(Team), aliased(Team)
    for prop_inst, opp_inst, winner in session.exec(
        select(prop_team.institution_id, opp_team.institution_id, Debate.winner)
        .join(prop_team, Debate.prop_team_id == prop_team.id)
        .join(opp_team, Debate.opp_team_id == opp_team.id)
        .where(Debate.winner.is_not(None))
    ).all():
        if prop_inst is not None and prop_inst == opp_inst:
            continue  # intra-school: no net result for the school
        if prop_inst is not None:
            agg(prop_inst).wins += winner == Side.PROP
            agg(prop_inst).losses += winner != Side.PROP
        if opp_inst is not None:
            agg(opp_inst).wins += winner == Side.OPP
            agg(opp_inst).losses += winner != Side.OPP

    # Speeches are credited to the school of the team represented that day.
    school = case((SpeakerScore.side == Side.PROP, prop_team.institution_id), else_=opp_team.institution_id)
    for iid, total, count in session.exec(
        select(school, func.sum(SpeakerScore.final_score), func.count())
        .select_from(SpeakerScore)
        .join(Ballot, SpeakerScore.ballot_id == Ballot.id)
        .join(Debate, Ballot.debate_id == Debate.id)
        .join(Round, Debate.round_id == Round.id)
        .join(prop_team, Debate.prop_team_id == prop_team.id)
        .join(opp_team, Debate.opp_team_id == opp_team.id)
        .where(
            Ballot.discarded == False,  # noqa: E712
            Ballot.forfeit == False,  # noqa: E712
            Round.isElimination == False,  # noqa: E712
            SpeakerScore.position != SpeakerPosition.REPLY,
        )
        .group_by(school)
    ).all():
        if iid is not None:
            agg(iid).speaks_sum = float(total)
            agg(iid).speaks_count = count

    team_institution = dict(
        session.exec(select(Team.id, Team.institution_id).where(Team.institution_id.is_not(None))).all()
    )
    for rounds in elim_rounds_by_tournament(session).values():
        for bracket in derive_brackets(rounds):
            if not bracket.primary:
                continue  # titles and finals count only the main bracket
            for team_id, finish in team_finishes([bracket]).items():
                iid = team_institution.get(team_id)
                if iid is None:
                    continue
                agg(iid).breaks += 1
                if finish.finish in ("champion", "finalist"):
                    agg(iid).finals += 1
                if finish.finish == "champion":
                    agg(iid).titles += 1

    qualified = [
        a for a in by_id.values() if a.wins + a.losses >= MIN_INSTITUTION_DEBATES
    ]
    return InstitutionAggregates(
        by_id=by_id,
        field_win_rate=_mean([a.win_rate for a in qualified if a.win_rate is not None]),
        field_mean_speaks=_mean([a.mean_speaks for a in qualified if a.mean_speaks is not None]),
        field_breaks=_mean([float(a.breaks) for a in qualified]),
    )


def institution_aggregates(session: Session) -> InstitutionAggregates:
    return cache.cached("institution_aggregates", lambda: _build_institution_aggregates(session))
