"""Materialized all-time profile refresh, plus other cross-tournament ("global") stats."""

import time
from datetime import datetime, timezone
from statistics import quantiles

from sqlmodel import Session, SQLModel, select

from app.models import (
    Ballot,
    Debate,
    Debater,
    DebaterProfile,
    Institution,
    Judge,
    JudgeProfile,
    Side,
    SpeakerPosition,
    SpeakerScore,
    Team,
    Tournament,
)
from app.services.stats import core
from app.services.stats import debater as debater_stats
from app.services.stats import judge as judge_stats
from app.services.stats import tournament as tournament_stats


class RefreshResult(SQLModel):
    debater_profiles: int
    judge_profiles: int
    computed_at: datetime
    duration_ms: int


class GlobalDistribution(SQLModel):
    count: int
    mean: float | None
    stdev: float | None
    q1: float | None
    median: float | None
    q3: float | None


class LeaderboardEntry(SQLModel):
    tournament_id: int
    tournament_name: str
    debater_id: int
    name: str
    average: float
    speeches: int
    rank: int


class InstitutionStats(SQLModel):
    institution_id: int
    name: str
    teams: int
    debaters: int
    judges: int
    win_rate: float | None
    mean_speaker_score: float | None


def refresh_profiles(session: Session) -> RefreshResult:
    """Recomputes both `DebaterProfile` and `JudgeProfile` from scratch: per-entity aggregates
    first, then a single population pass for percentiles and ranks. Delete-and-reinsert inside
    one transaction, mirroring v1's `initializeAllDebaterProfiles`."""
    start = time.monotonic()
    computed_at = datetime.now(timezone.utc)

    debater_aggregates = debater_stats.compute_debater_aggregates(session)
    judge_aggregates = judge_stats.compute_judge_aggregates(session)
    sentiments = judge_stats.compute_sentiment(session)

    activity_percentiles = core.percentile_ranks(
        {d: agg.total_rounds for d, agg in debater_aggregates.items()}
    )
    prelim_wr_percentiles = core.percentile_ranks(
        {d: agg.win_rate_prelims for d, agg in debater_aggregates.items() if agg.win_rate_prelims is not None}
    )
    elim_wr_percentiles = core.percentile_ranks(
        {d: agg.win_rate_elims for d, agg in debater_aggregates.items() if agg.win_rate_elims is not None}
    )
    speaker_avg_population: dict[int, float] = {}
    for debater_id, agg in debater_aggregates.items():
        avg, _stdev = core.mean_and_stdev(agg.speech_scores)
        if avg is not None:
            speaker_avg_population[debater_id] = avg
    speaker_ranks = core.competition_ranks(speaker_avg_population)
    speaker_percentiles = core.percentile_ranks(speaker_avg_population)

    judge_activity_percentiles = core.percentile_ranks(
        {j: agg.total_judged for j, agg in judge_aggregates.items()}
    )

    for existing in session.exec(select(DebaterProfile)).all():
        session.delete(existing)
    for existing in session.exec(select(JudgeProfile)).all():
        session.delete(existing)
    session.flush()

    debater_count = 0
    for debater_id, agg in debater_aggregates.items():
        avg, stdev = core.mean_and_stdev(agg.speech_scores)
        content_avg, _ = core.mean_and_stdev(agg.content_scores)
        style_avg, _ = core.mean_and_stdev(agg.style_scores)
        strategy_avg, _ = core.mean_and_stdev(agg.strategy_scores)
        session.add(
            DebaterProfile(
                debater_id=debater_id,
                computed_at=computed_at,
                tournaments_debated=agg.tournaments_debated,
                prelims_debated=agg.prelims_debated,
                elims_debated=agg.elims_debated,
                total_rounds=agg.total_rounds,
                activity_percentile=_round_or_none(activity_percentiles.get(debater_id)),
                prelim_wins=agg.prelim_wins,
                prelim_losses=agg.prelim_losses,
                win_rate_prelims=_round_or_none(agg.win_rate_prelims),
                win_rate_prelims_percentile=_round_or_none(prelim_wr_percentiles.get(debater_id)),
                elim_wins=agg.elim_wins,
                elim_losses=agg.elim_losses,
                win_rate_elims=_round_or_none(agg.win_rate_elims),
                win_rate_elims_percentile=_round_or_none(elim_wr_percentiles.get(debater_id)),
                average_speaker_score=_round_or_none(avg),
                speaker_score_stdev=_round_or_none(stdev),
                total_speeches=len(agg.speech_scores),
                speaker_rank=speaker_ranks.get(debater_id),
                speaker_score_percentile=_round_or_none(speaker_percentiles.get(debater_id)),
                iron_person_count=agg.iron_person_count,
                average_content=_round_or_none(content_avg),
                average_style=_round_or_none(style_avg),
                average_strategy=_round_or_none(strategy_avg),
                furthest_rounds=agg.furthest_rounds,
                speaker_performances=agg.speaker_performances,
            )
        )
        debater_count += 1

    judge_count = 0
    for judge_id, agg in judge_aggregates.items():
        first_avg, _ = core.mean_and_stdev(agg.first_scores)
        second_avg, _ = core.mean_and_stdev(agg.second_scores)
        third_avg, _ = core.mean_and_stdev(agg.third_scores)
        reply_avg, _ = core.mean_and_stdev(agg.reply_scores)
        substantive_avg, _ = core.mean_and_stdev(agg.substantive_scores)
        _, score_stdev = core.mean_and_stdev(agg.all_scores)
        sentiment = sentiments.get(judge_id, judge_stats.SentimentResult())

        session.add(
            JudgeProfile(
                judge_id=judge_id,
                computed_at=computed_at,
                prelims_judged=agg.prelims_judged,
                elims_judged=agg.elims_judged,
                total_judged=agg.total_judged,
                tournaments_judged=agg.tournaments_judged,
                activity_percentile=_round_or_none(judge_activity_percentiles.get(judge_id)),
                average_first=_round_or_none(first_avg),
                average_second=_round_or_none(second_avg),
                average_third=_round_or_none(third_avg),
                average_reply=_round_or_none(reply_avg),
                average_substantive=_round_or_none(substantive_avg),
                score_stdev=_round_or_none(score_stdev),
                leniency_count=sentiment.leniency_count,
                harshness_count=sentiment.harshness_count,
                neutral_count=sentiment.neutral_count,
                leniency=_round_or_none(sentiment.leniency),
                harshness=_round_or_none(sentiment.harshness),
                overall_sentiment=_round_or_none(sentiment.overall_sentiment),
                speeches_considered=sentiment.speeches_considered,
                ballots_cast=agg.ballots_cast,
                dissents=agg.dissents,
                dissent_rate=_round_or_none(agg.dissent_rate),
                round_preferences=agg.round_preference_counts,
            )
        )
        judge_count += 1

    session.commit()
    duration_ms = int((time.monotonic() - start) * 1000)
    return RefreshResult(
        debater_profiles=debater_count,
        judge_profiles=judge_count,
        computed_at=computed_at,
        duration_ms=duration_ms,
    )


def _round_or_none(value: float | None, digits: int = 4) -> float | None:
    return round(value, digits) if value is not None else None


def _quartiles(values: list[float]) -> tuple[float | None, float | None, float | None]:
    if not values:
        return None, None, None
    if len(values) == 1:
        return values[0], values[0], values[0]
    q1, median, q3 = quantiles(values, n=4, method="inclusive")
    return q1, median, q3


def global_distribution(session: Session) -> GlobalDistribution:
    """Mean, population stdev, count and quartiles over all non-discarded, non-forfeit
    substantive scores. v1 declared median/quartiles on `TournamentStatsDTO` but never
    implemented them."""
    values = session.exec(
        select(SpeakerScore.final_score)
        .join(Ballot, SpeakerScore.ballot_id == Ballot.id)
        .where(
            Ballot.discarded == False,  # noqa: E712
            Ballot.forfeit == False,  # noqa: E712
            SpeakerScore.position != SpeakerPosition.REPLY,
        )
    ).all()
    mean, stdev = core.mean_and_stdev(values)
    q1, median, q3 = _quartiles(values)
    return GlobalDistribution(
        count=len(values),
        mean=_round_or_none(mean),
        stdev=_round_or_none(stdev),
        q1=_round_or_none(q1),
        median=_round_or_none(median),
        q3=_round_or_none(q3),
    )


def speaker_leaderboard(session: Session, limit: int = 10) -> list[LeaderboardEntry]:
    """Best single-tournament speaker performances all-time: the top `limit` of every
    tournament's tab, flattened and sorted by average descending."""
    tournaments = session.exec(select(Tournament)).all()
    entries: list[LeaderboardEntry] = []
    for tournament in tournaments:
        tab = tournament_stats.speaker_tab(session, tournament.id)
        for row in tab.rows[:limit]:
            entries.append(
                LeaderboardEntry(
                    tournament_id=tournament.id,
                    tournament_name=tournament.name,
                    debater_id=row.debater_id,
                    name=row.name,
                    average=row.average,
                    speeches=row.speeches,
                    rank=row.rank,
                )
            )
    entries.sort(key=lambda e: -e.average)
    return entries[:limit]


def institution_stats(session: Session, institution_id: int) -> InstitutionStats:
    institution = session.get(Institution, institution_id)
    teams = session.exec(select(Team).where(Team.institution_id == institution_id)).all()
    debaters = session.exec(select(Debater).where(Debater.institution_id == institution_id)).all()
    judges = session.exec(select(Judge).where(Judge.institution_id == institution_id)).all()

    team_ids = {t.id for t in teams}
    wins = losses = 0
    if team_ids:
        debates = session.exec(
            select(Debate).where(
                (Debate.prop_team_id.in_(team_ids)) | (Debate.opp_team_id.in_(team_ids))
            )
        ).all()
        for debate in debates:
            if debate.winner is None:
                continue
            if debate.prop_team_id in team_ids and debate.opp_team_id in team_ids:
                continue
            team_side = Side.PROP if debate.prop_team_id in team_ids else Side.OPP
            if debate.winner == team_side:
                wins += 1
            else:
                losses += 1

    debater_ids = [d.id for d in debaters]
    scores = (
        session.exec(
            select(SpeakerScore.final_score)
            .join(Ballot, SpeakerScore.ballot_id == Ballot.id)
            .where(
                SpeakerScore.debater_id.in_(debater_ids),
                Ballot.discarded == False,  # noqa: E712
                Ballot.forfeit == False,  # noqa: E712
            )
        ).all()
        if debater_ids
        else []
    )
    mean_score, _ = core.mean_and_stdev(scores)
    total = wins + losses

    return InstitutionStats(
        institution_id=institution_id,
        name=institution.name if institution else "",
        teams=len(teams),
        debaters=len(debaters),
        judges=len(judges),
        win_rate=_round_or_none(wins / total * 100) if total else None,
        mean_speaker_score=_round_or_none(mean_score),
    )
