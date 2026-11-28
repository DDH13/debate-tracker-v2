"""`GET /public/institutions/{id}`: a school's whole page in one response.

Win/loss excludes intra-school debates (as `profiles.institution_stats` does) and speeches are
credited to the school of the team represented that day. Tournament history merges the
imported tournaments with the board's manual `SchoolParticipation` rows, whose `notes` are
internal and never served; `SchoolAchievement` stays private."""

from collections import defaultdict
from datetime import date

from sqlmodel import Session, SQLModel, func, select

from app.models import (
    Debate,
    DebateJudge,
    Debater,
    DebaterProfile,
    Institution,
    Judge,
    Round,
    SchoolParticipation,
    Side,
    Team,
    TeamMember,
)
from app.services.public_site import field
from app.services.public_site.loaders import debater_refs, institution_ref, judge_refs, person_name
from app.services.public_site.schemas import (
    InstitutionRef,
    Metric,
    PersonRef,
    TeamRef,
    TournamentRef,
    WinLoss,
    metric,
    win_loss,
)
from app.services.public_site.schools import latest_team_school, school_id
from app.services.public_site.snapshots import tournament_snapshot
from app.services.stats.history import tournament_averages

FINISH_ORDER = ["champion", "finalist", "semi", "quarter", "octo", "broke"]


class InstitutionSummary(SQLModel):
    tournaments: int
    teams: int
    debaters: int
    judges: int
    record: WinLoss
    win_rate: Metric
    mean_speaks: Metric
    breaks: Metric
    finals: int
    titles: int


class HistoryRow(SQLModel):
    source: str  # "imported" | "manual"
    tournament: TournamentRef | None
    event_name: str
    year: int | None
    teams: int | None
    best_finish: str | None
    best_rank: int | None


class InstitutionTeamRow(SQLModel):
    tournament: TournamentRef
    team: TeamRef
    speakers: list[PersonRef]
    rank: int | None
    prelim: WinLoss
    elim: WinLoss
    finish: str | None
    bracket: str | None


class RosterRow(SQLModel):
    debater: PersonRef
    tournaments: int
    rounds: int
    average: float | None
    speaker_rank: int | None
    prelim_win_rate: float | None


class InstitutionJudgeRow(SQLModel):
    judge: PersonRef
    debates: int
    tournaments: int


class SeriesPoint(SQLModel):
    tournament: TournamentRef
    teams: int
    wins: int
    losses: int
    win_rate: float | None
    mean_speaks: float | None
    field_average: float | None


class PublicInstitution(SQLModel):
    institution: InstitutionRef
    district: str | None
    summary: InstitutionSummary
    history: list[HistoryRow]
    teams: list[InstitutionTeamRow]
    roster: list[RosterRow]
    judges: list[InstitutionJudgeRow]
    series: list[SeriesPoint]


def _better(a: str | None, b: str | None) -> str | None:
    if a is None:
        return b
    if b is None:
        return a
    return a if FINISH_ORDER.index(a) <= FINISH_ORDER.index(b) else b


def build_public_institution(session: Session, institution: Institution) -> PublicInstitution:
    iid = institution.id
    aggregates = field.institution_aggregates(session)
    agg = aggregates.by_id.get(iid) or field.InstitutionAggregate(institution_id=iid)

    own_teams = session.exec(select(Team).where(Team.institution_id == iid)).all()
    tournament_ids = sorted({t.tournament_id for t in own_teams})
    snapshots = {tid: tournament_snapshot(session, tid) for tid in tournament_ids}
    snapshots = {tid: s for tid, s in snapshots.items() if s is not None}
    field_average = {t.tournament_id: t.average for t in tournament_averages(session)}

    # Live rows, not the cached snapshot (which may predate a team): the school's own teams,
    # their members, and a decided-debate record that leaves out intra-school debates.
    own_ids = {t.id for t in own_teams}
    members_by_team: dict[int, list[int]] = defaultdict(list)
    for team_id, member_id in session.exec(
        select(TeamMember.team_id, TeamMember.debater_id)
        .where(TeamMember.team_id.in_(own_ids or {-1}))
        .order_by(TeamMember.id)
    ).all():
        members_by_team[team_id].append(member_id)
    people = debater_refs(session, (d for ds in members_by_team.values() for d in ds))
    record: dict[int, list[int]] = defaultdict(lambda: [0, 0])  # tournament -> [wins, losses]
    if own_ids:
        for tournament_id, prop, opp, winner in session.exec(
            select(Round.tournament_id, Debate.prop_team_id, Debate.opp_team_id, Debate.winner)
            .join(Round, Debate.round_id == Round.id)
            .where(
                Debate.winner.is_not(None),
                Debate.prop_team_id.in_(own_ids) | Debate.opp_team_id.in_(own_ids),
            )
        ).all():
            if prop in own_ids and opp in own_ids:
                continue  # intra-school: no net result for the school
            won = (winner == Side.PROP) == (prop in own_ids)
            record[tournament_id][0 if won else 1] += 1
    inst_ref = institution_ref(institution)

    team_rows: list[InstitutionTeamRow] = []
    history: dict[int, HistoryRow] = {}
    series: list[SeriesPoint] = []
    for tid, snap in snapshots.items():
        mine = [t for t in own_teams if t.tournament_id == tid]
        wins, losses = record[tid] if tid in record else (0, 0)
        speaks_total = 0.0
        speaks_count = 0.0
        best_finish: str | None = None
        best_rank: int | None = None
        for team in mine:
            standing = snap.standing_by_team.get(team.id)
            finish = snap.finishes.get(team.id)
            if standing:
                if standing.average_speaks:
                    speaks_total += standing.total_speaks
                    speaks_count += standing.total_speaks / standing.average_speaks
                best_rank = standing.rank if best_rank is None else min(best_rank, standing.rank)
            if finish and finish.primary:
                best_finish = _better(best_finish, finish.finish)
            team_rows.append(
                InstitutionTeamRow(
                    tournament=snap.tournament,
                    team=TeamRef(id=team.id, name=team.name, institution=inst_ref),
                    speakers=[people[d] for d in members_by_team[team.id] if d in people],
                    rank=standing.rank if standing else None,
                    prelim=win_loss(standing.prelim_wins, standing.prelim_losses) if standing else WinLoss(),
                    elim=win_loss(standing.elim_wins, standing.elim_losses) if standing else WinLoss(),
                    finish=finish.finish if finish else None,
                    bracket=finish.bracket_label if finish else None,
                )
            )
        history[tid] = HistoryRow(
            source="imported",
            tournament=snap.tournament,
            event_name=snap.tournament.name,
            year=snap.tournament.date.year if snap.tournament.date else None,
            teams=len(mine),
            best_finish=best_finish,
            best_rank=best_rank,
        )
        series.append(
            SeriesPoint(
                tournament=snap.tournament,
                teams=len(mine),
                wins=wins,
                losses=losses,
                win_rate=round(wins / (wins + losses) * 100, 2) if wins + losses else None,
                mean_speaks=round(speaks_total / speaks_count, 4) if speaks_count else None,
                field_average=field_average.get(tid),
            )
        )
    team_rows.sort(
        key=lambda r: (r.tournament.date or date.min, r.tournament.id, r.rank or 10**6), reverse=False
    )
    team_rows.sort(key=lambda r: (r.tournament.date or date.min, r.tournament.id), reverse=True)
    series.sort(key=lambda p: (p.tournament.date or date.min, p.tournament.id))

    rows = list(history.values())
    for manual in session.exec(
        select(SchoolParticipation).where(SchoolParticipation.institution_id == iid)
    ).all():
        # `notes` is deliberately left out: it is internal.
        rows.append(
            HistoryRow(
                source="manual",
                tournament=None,
                event_name=manual.event_name,
                year=manual.year,
                teams=manual.teams,
                best_finish=None,
                best_rank=None,
            )
        )
    rows.sort(
        key=lambda r: (
            r.year is not None,
            r.year or 0,
            r.tournament.date if r.tournament and r.tournament.date else date.min,
            r.event_name,
        ),
        reverse=True,
    )

    latest = latest_team_school()
    roster = [
        RosterRow(
            debater=PersonRef(id=d.id, name=person_name(d, "Debater")),
            tournaments=p.tournaments_debated if p else 0,
            rounds=p.total_rounds if p else 0,
            average=p.average_speaker_score if p else None,
            speaker_rank=p.speaker_rank if p else None,
            prelim_win_rate=p.win_rate_prelims if p else None,
        )
        for d, p in session.exec(
            select(Debater, DebaterProfile)
            .join(DebaterProfile, DebaterProfile.debater_id == Debater.id, isouter=True)
            .join(latest, latest.c.debater_id == Debater.id, isouter=True)
            .where(school_id(latest) == iid)
        ).all()
    ]
    roster.sort(key=lambda r: (-r.rounds, r.debater.name))

    judge_objs = session.exec(select(Judge).where(Judge.institution_id == iid)).all()
    involvement = {
        jid: (debates, tournaments)
        for jid, debates, tournaments in session.exec(
            select(
                DebateJudge.judge_id,
                func.count(func.distinct(DebateJudge.debate_id)),
                func.count(func.distinct(Round.tournament_id)),
            )
            .join(Debate, DebateJudge.debate_id == Debate.id)
            .join(Round, Debate.round_id == Round.id)
            .join(Judge, DebateJudge.judge_id == Judge.id)
            .where(Judge.institution_id == iid)
            .group_by(DebateJudge.judge_id)
        ).all()
    }
    judge_people = judge_refs(session, (j.id for j in judge_objs))
    judges = sorted(
        (
            InstitutionJudgeRow(
                judge=judge_people[j.id],
                debates=involvement.get(j.id, (0, 0))[0],
                tournaments=involvement.get(j.id, (0, 0))[1],
            )
            for j in judge_objs
        ),
        key=lambda r: (-r.debates, r.judge.name),
    )

    summary = InstitutionSummary(
        tournaments=agg.tournaments,
        teams=agg.teams,
        debaters=agg.debaters,
        judges=agg.judges,
        record=win_loss(agg.wins, agg.losses),
        win_rate=metric(agg.win_rate, aggregates.field_win_rate, digits=1),
        mean_speaks=metric(agg.mean_speaks, aggregates.field_mean_speaks),
        breaks=metric(float(agg.breaks), aggregates.field_breaks, digits=1),
        finals=agg.finals,
        titles=agg.titles,
    )
    return PublicInstitution(
        institution=inst_ref,
        district=institution.district,
        summary=summary,
        history=rows,
        teams=team_rows,
        roster=roster,
        judges=judges,
        series=series,
    )
