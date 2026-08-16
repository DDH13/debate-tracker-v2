"""Per-judge career aggregates and leave-one-judge-out sentiment analysis."""

from collections import defaultdict
from dataclasses import dataclass, field

from sqlmodel import Session, SQLModel, select

from app.models import Ballot, Debate, DebateFormat, Judge, Round, SpeakerPosition, SpeakerScore, Tournament
from app.services.stats import core

DEFAULT_SENTIMENT_DEVIATION = 0.5
MIN_SENTIMENT_BASELINE = 5


@dataclass
class JudgeAggregate:
    tournament_ids: set[int] = field(default_factory=set)
    debate_ids_prelim: set[int] = field(default_factory=set)
    debate_ids_elim: set[int] = field(default_factory=set)
    first_scores: list[float] = field(default_factory=list)
    second_scores: list[float] = field(default_factory=list)
    third_scores: list[float] = field(default_factory=list)
    reply_scores: list[float] = field(default_factory=list)
    ballots_cast: int = 0
    dissents: int = 0
    round_preferences: dict[str, set[int]] = field(default_factory=lambda: defaultdict(set))

    @property
    def prelims_judged(self) -> int:
        return len(self.debate_ids_prelim)

    @property
    def elims_judged(self) -> int:
        return len(self.debate_ids_elim)

    @property
    def total_judged(self) -> int:
        return self.prelims_judged + self.elims_judged

    @property
    def tournaments_judged(self) -> int:
        return len(self.tournament_ids)

    @property
    def substantive_scores(self) -> list[float]:
        return self.first_scores + self.second_scores + self.third_scores

    @property
    def all_scores(self) -> list[float]:
        return self.substantive_scores + self.reply_scores

    @property
    def dissent_rate(self) -> float | None:
        return (self.dissents / self.ballots_cast * 100) if self.ballots_cast else None

    @property
    def round_preference_counts(self) -> dict[str, int]:
        return {key: len(tournament_ids) for key, tournament_ids in self.round_preferences.items()}


@dataclass
class SentimentResult:
    leniency_count: int = 0
    harshness_count: int = 0
    neutral_count: int = 0
    leniency: float | None = None
    harshness: float | None = None
    overall_sentiment: float | None = None
    speeches_considered: int = 0


class JudgeSentiment(SQLModel):
    judge_id: int
    name: str
    leniency_count: int
    harshness_count: int
    neutral_count: int
    leniency: float | None
    harshness: float | None
    overall_sentiment: float | None
    speeches_considered: int


def compute_judge_aggregates(session: Session) -> dict[int, JudgeAggregate]:
    """Career aggregates over two-team ballots only. BP tournaments are explicitly
    excluded (see `RefreshResult.bp_tournaments_excluded`), matching
    `debater_stats.compute_debater_aggregates`, since career profiles don't yet unify
    across formats."""
    aggregates: dict[int, JudgeAggregate] = defaultdict(JudgeAggregate)

    ballot_rows = session.exec(
        select(
            Ballot.judge_id,
            Ballot.winner,
            Ballot.discarded,
            Ballot.debate_id,
            Round.tournament_id,
            Round.isElimination,
            Round.name,
            Round.abbr,
            Round.seq,
            Debate.winner,
        )
        .join(Debate, Ballot.debate_id == Debate.id)
        .join(Round, Debate.round_id == Round.id)
        .join(Tournament, Round.tournament_id == Tournament.id)
        .where(Tournament.format == DebateFormat.TWO_TEAM)
    ).all()

    for (
        judge_id,
        ballot_winner,
        discarded,
        debate_id,
        tournament_id,
        is_elim,
        round_name,
        round_abbr,
        round_seq,
        debate_winner,
    ) in ballot_rows:
        agg = aggregates[judge_id]
        agg.tournament_ids.add(tournament_id)
        if is_elim:
            agg.debate_ids_elim.add(debate_id)
        else:
            agg.debate_ids_prelim.add(debate_id)

        key = round_name or round_abbr or f"Round {round_seq}"
        agg.round_preferences[key].add(tournament_id)

        if not discarded:
            agg.ballots_cast += 1
            if (
                ballot_winner is not None
                and debate_winner is not None
                and ballot_winner != debate_winner
            ):
                agg.dissents += 1

    score_rows = session.exec(
        select(SpeakerScore.position, SpeakerScore.final_score, Ballot.judge_id)
        .join(Ballot, SpeakerScore.ballot_id == Ballot.id)
        .where(Ballot.discarded == False, Ballot.forfeit == False)  # noqa: E712
    ).all()
    for position, final_score, judge_id in score_rows:
        agg = aggregates[judge_id]
        if position == SpeakerPosition.FIRST:
            agg.first_scores.append(final_score)
        elif position == SpeakerPosition.SECOND:
            agg.second_scores.append(final_score)
        elif position == SpeakerPosition.THIRD:
            agg.third_scores.append(final_score)
        elif position == SpeakerPosition.REPLY:
            agg.reply_scores.append(final_score)

    return dict(aggregates)


def compute_sentiment(
    session: Session,
    allowed_deviation: float = DEFAULT_SENTIMENT_DEVIATION,
    *,
    min_baseline: int = MIN_SENTIMENT_BASELINE,
) -> dict[int, SentimentResult]:
    """Leave-one-judge-out sentiment: for each speech, compare the judge's score to that
    debater's average excluding every score that judge gave them. Skips a speech if fewer
    than `min_baseline` other scores remain for the baseline."""
    rows = session.exec(
        select(SpeakerScore.debater_id, Ballot.judge_id, SpeakerScore.final_score)
        .join(Ballot, SpeakerScore.ballot_id == Ballot.id)
        .where(
            Ballot.discarded == False,  # noqa: E712
            Ballot.forfeit == False,  # noqa: E712
            SpeakerScore.position != SpeakerPosition.REPLY,
        )
    ).all()

    by_debater: dict[int, list[tuple[int, float]]] = defaultdict(list)
    for debater_id, judge_id, final_score in rows:
        by_debater[debater_id].append((judge_id, final_score))

    buckets: dict[int, dict[str, float]] = defaultdict(
        lambda: {"leniency_sum": 0.0, "harshness_sum": 0.0, "leniency_count": 0, "harshness_count": 0, "neutral_count": 0}
    )

    for entries in by_debater.values():
        by_judge: dict[int, list[float]] = defaultdict(list)
        for judge_id, score in entries:
            by_judge[judge_id].append(score)

        total_sum = sum(score for _, score in entries)
        total_count = len(entries)

        for judge_id, own_scores in by_judge.items():
            remaining_count = total_count - len(own_scores)
            if remaining_count < min_baseline:
                continue
            baseline = (total_sum - sum(own_scores)) / remaining_count
            bucket = buckets[judge_id]
            for score in own_scores:
                delta = score - baseline
                if delta >= allowed_deviation:
                    bucket["leniency_sum"] += delta
                    bucket["leniency_count"] += 1
                elif delta <= -allowed_deviation:
                    bucket["harshness_sum"] += delta
                    bucket["harshness_count"] += 1
                else:
                    bucket["neutral_count"] += 1

    results: dict[int, SentimentResult] = {}
    for judge_id, bucket in buckets.items():
        leniency_count = int(bucket["leniency_count"])
        harshness_count = int(bucket["harshness_count"])
        neutral_count = int(bucket["neutral_count"])
        speeches_considered = leniency_count + harshness_count + neutral_count
        results[judge_id] = SentimentResult(
            leniency_count=leniency_count,
            harshness_count=harshness_count,
            neutral_count=neutral_count,
            leniency=(bucket["leniency_sum"] / leniency_count) if leniency_count else None,
            harshness=(bucket["harshness_sum"] / harshness_count) if harshness_count else None,
            overall_sentiment=(
                (bucket["leniency_sum"] + bucket["harshness_sum"]) / speeches_considered
            )
            if speeches_considered
            else None,
            speeches_considered=speeches_considered,
        )
    return results


def judge_sentiment(
    session: Session, allowed_deviation: float = DEFAULT_SENTIMENT_DEVIATION
) -> list[JudgeSentiment]:
    sentiments = compute_sentiment(session, allowed_deviation)
    if not sentiments:
        return []
    judges = {
        j.id: j for j in session.exec(select(Judge).where(Judge.id.in_(sentiments.keys()))).all()
    }
    results = []
    for judge_id, result in sentiments.items():
        judge = judges.get(judge_id)
        name = core.display_name(judge.full_name, judge.first_name, judge.last_name) if judge else ""
        results.append(
            JudgeSentiment(
                judge_id=judge_id,
                name=name,
                leniency_count=result.leniency_count,
                harshness_count=result.harshness_count,
                neutral_count=result.neutral_count,
                leniency=round(result.leniency, 4) if result.leniency is not None else None,
                harshness=round(result.harshness, 4) if result.harshness is not None else None,
                overall_sentiment=round(result.overall_sentiment, 4)
                if result.overall_sentiment is not None
                else None,
                speeches_considered=result.speeches_considered,
            )
        )
    results.sort(key=lambda r: r.name)
    return results
