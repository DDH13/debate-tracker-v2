"""Per-debater career aggregates: the building blocks of `DebaterProfile`, plus head-to-head."""

from collections import defaultdict
from dataclasses import dataclass, field

from sqlmodel import Session, SQLModel, select

from app.models import Ballot, DebateFormat, Debater, Round, SpeakerPosition, SpeakerScore, Tournament
from app.services.stats import core
from app.services.stats import tournament as tournament_stats


@dataclass
class DebaterAggregate:
    tournament_ids: set[int] = field(default_factory=set)
    prelims_debated: int = 0
    elims_debated: int = 0
    prelim_wins: int = 0
    prelim_losses: int = 0
    elim_wins: int = 0
    elim_losses: int = 0
    speech_scores: list[float] = field(default_factory=list)
    iron_person_count: int = 0
    content_scores: list[float] = field(default_factory=list)
    style_scores: list[float] = field(default_factory=list)
    strategy_scores: list[float] = field(default_factory=list)
    furthest_rounds: list[dict] = field(default_factory=list)
    speaker_performances: list[dict] = field(default_factory=list)

    @property
    def tournaments_debated(self) -> int:
        return len(self.tournament_ids)

    @property
    def total_rounds(self) -> int:
        return self.prelims_debated + self.elims_debated

    @property
    def win_rate_prelims(self) -> float | None:
        total = self.prelim_wins + self.prelim_losses
        return (self.prelim_wins / total * 100) if total else None

    @property
    def win_rate_elims(self) -> float | None:
        total = self.elim_wins + self.elim_losses
        return (self.elim_wins / total * 100) if total else None


class HeadToHeadRecord(SQLModel):
    opponent_id: int
    name: str
    wins: int
    losses: int


def compute_debater_aggregates(session: Session) -> dict[int, DebaterAggregate]:
    """One pass over every two-team tournament, building career aggregates for every
    debater who has debated at least one round. Debaters with zero rounds are absent from
    the result, which is what excludes them from every downstream win-rate population.
    BP tournaments are explicitly excluded (see `RefreshResult.bp_tournaments_excluded`)
    rather than merely absent-by-construction, since career profiles don't yet unify
    across formats."""
    aggregates: dict[int, DebaterAggregate] = defaultdict(DebaterAggregate)

    tournaments = {
        t.id: t for t in session.exec(select(Tournament)).all() if t.format == DebateFormat.TWO_TEAM
    }
    rounds_by_id = {r.id: r for r in session.exec(select(Round)).all()}

    records_by_tournament: dict[int, list[core.Participation]] = defaultdict(list)
    for record in core.participation(session):
        if record.tournament_id not in tournaments:
            continue
        records_by_tournament[record.tournament_id].append(record)

    for tournament_id, trecs in records_by_tournament.items():
        tournament = tournaments.get(tournament_id)
        by_debater: dict[int, list[core.Participation]] = defaultdict(list)
        for record in trecs:
            by_debater[record.debater_id].append(record)

        for debater_id, drecs in by_debater.items():
            agg = aggregates[debater_id]
            agg.tournament_ids.add(tournament_id)
            for record in drecs:
                if record.is_elimination:
                    agg.elims_debated += 1
                    if record.won is True:
                        agg.elim_wins += 1
                    elif record.won is False:
                        agg.elim_losses += 1
                else:
                    agg.prelims_debated += 1
                    if record.won is True:
                        agg.prelim_wins += 1
                    elif record.won is False:
                        agg.prelim_losses += 1

            elim_recs = [r for r in drecs if r.is_elimination]
            if elim_recs and tournament is not None:
                furthest = max(elim_recs, key=lambda r: r.round_seq)
                round_ = rounds_by_id.get(furthest.round_id)
                agg.furthest_rounds.append(
                    {
                        "tournament_id": tournament_id,
                        "tournament_name": tournament.name,
                        "date": tournament.date.isoformat() if tournament.date else None,
                        "round_id": furthest.round_id,
                        "round_name": round_.name if round_ else None,
                        "round_seq": furthest.round_seq,
                        "won": furthest.won,
                    }
                )

        prelim_detailed = core.round_scores_with_iron(session, tournament_id, elimination=False)
        elim_detailed = core.round_scores_with_iron(session, tournament_id, elimination=True)
        for detailed in (prelim_detailed, elim_detailed):
            for debater_id, rounds in detailed.items():
                agg = aggregates[debater_id]
                for score, is_iron in rounds.values():
                    agg.speech_scores.append(score)
                    if is_iron:
                        agg.iron_person_count += 1

        _minimum_speeches, tab_entries = tournament_stats.speaker_tab_rows(session, tournament_id)
        for debater_id, entry in tab_entries.items():
            agg = aggregates[debater_id]
            agg.speaker_performances.append(
                {
                    "tournament_id": tournament_id,
                    "tournament_name": tournament.name if tournament else None,
                    "date": tournament.date.isoformat() if tournament and tournament.date else None,
                    "speeches": entry["speeches"],
                    "average": round(entry["average"], 4),
                    "stdev": round(entry["stdev"], 4),
                    "rank": entry["rank"],
                }
            )

    category_rows = session.exec(
        select(SpeakerScore.debater_id, SpeakerScore.content, SpeakerScore.style, SpeakerScore.strategy)
        .join(Ballot, SpeakerScore.ballot_id == Ballot.id)
        .where(
            Ballot.discarded == False,  # noqa: E712
            Ballot.forfeit == False,  # noqa: E712
            SpeakerScore.position != SpeakerPosition.REPLY,
            SpeakerScore.content.is_not(None),
        )
    ).all()
    for debater_id, content, style, strategy in category_rows:
        agg = aggregates[debater_id]
        agg.content_scores.append(content)
        agg.style_scores.append(style)
        agg.strategy_scores.append(strategy)

    return dict(aggregates)


def head_to_head(
    session: Session, debater_id: int, opponent_id: int | None = None
) -> list[HeadToHeadRecord]:
    by_debate: dict[int, list[core.Participation]] = defaultdict(list)
    for record in core.participation(session):
        by_debate[record.debate_id].append(record)

    tally: dict[int, dict[str, int]] = defaultdict(lambda: {"wins": 0, "losses": 0})
    for recs in by_debate.values():
        mine = [r for r in recs if r.debater_id == debater_id]
        if not mine or mine[0].won is None:
            continue
        my_side = mine[0].side
        won = mine[0].won
        for opp in recs:
            if opp.side == my_side:
                continue
            if opponent_id is not None and opp.debater_id != opponent_id:
                continue
            key = "wins" if won else "losses"
            tally[opp.debater_id][key] += 1

    if not tally:
        return []

    debaters = {
        d.id: d for d in session.exec(select(Debater).where(Debater.id.in_(tally.keys()))).all()
    }
    results = []
    for opp_id, counts in tally.items():
        debater = debaters.get(opp_id)
        name = core.display_name(debater.full_name, debater.first_name, debater.last_name) if debater else ""
        results.append(
            HeadToHeadRecord(opponent_id=opp_id, name=name, wins=counts["wins"], losses=counts["losses"])
        )
    results.sort(key=lambda r: (-r.wins, r.name))
    return results
