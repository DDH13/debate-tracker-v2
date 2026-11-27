"""Paged, sortable list pages for debaters, judges, institutions and tournaments.

Envelope: `{total, offset, limit, items}`. `sort` is a whitelist (an unknown value is a 422 at
the endpoint), ordering is `nulls_last` then id so paging is stable, and every row carries its
institution / champion reference so the UI never looks names up per row.
"""

from datetime import date, datetime
from typing import Literal

from sqlalchemy import ColumnElement
from sqlalchemy import func as sa_func
from sqlmodel import Session, SQLModel, col, func, select

from app.models import (
    Debater,
    DebaterProfile,
    Institution,
    Judge,
    JudgeProfile,
    Round,
    Team,
    Tournament,
)
from app.services.public import field
from app.services.public.brackets import derive_brackets
from app.services.public.loaders import institution_ref, institution_refs, team_refs, tournament_ref
from app.services.public.schemas import InstitutionRef, Page, PersonRef, TeamRef, TournamentRef
from app.services.public.schools import latest_team_school, school_id
from app.services.public.snapshots import elim_rounds_by_tournament

Order = Literal["asc", "desc"]


def _ordered(expression: ColumnElement, order: Order, id_column: ColumnElement) -> tuple[ColumnElement, ColumnElement]:
    direction = expression.asc() if order == "asc" else expression.desc()
    return (direction.nulls_last(), id_column.asc())


def _name_expression(model: type[Debater] | type[Judge]) -> ColumnElement:
    return func.coalesce(
        model.full_name, func.trim(func.coalesce(model.first_name, "") + " " + func.coalesce(model.last_name, ""))
    )


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


# --- Debaters ---

DebaterSort = Literal["name", "average", "rounds", "tournaments", "win_rate", "rank", "stdev"]


class DebaterListRow(SQLModel):
    debater: PersonRef
    institution: InstitutionRef | None
    tournaments: int
    rounds: int
    average: float | None
    stdev: float | None
    prelim_win_rate: float | None
    speaker_rank: int | None
    percentile: float | None
    trend: list[float]  # per-tournament prelim averages, oldest first (for a sparkline)


class DebaterList(Page):
    profile_computed_at: datetime | None
    items: list[DebaterListRow]


def list_debaters(
    session: Session,
    *,
    q: str | None,
    institution_id: int | None,
    min_rounds: int,
    sort: DebaterSort,
    order: Order,
    offset: int,
    limit: int,
) -> DebaterList:
    name = _name_expression(Debater)
    sort_columns = {
        "name": name,
        "average": DebaterProfile.average_speaker_score,
        "rounds": DebaterProfile.total_rounds,
        "tournaments": DebaterProfile.tournaments_debated,
        "win_rate": DebaterProfile.win_rate_prelims,
        "rank": DebaterProfile.speaker_rank,
        "stdev": DebaterProfile.speaker_score_stdev,
    }
    latest = latest_team_school()
    school = school_id(latest)
    base = (
        select(Debater, Institution, DebaterProfile)
        .join(latest, latest.c.debater_id == Debater.id, isouter=True)
        .join(Institution, school == Institution.id, isouter=True)
        .join(DebaterProfile, DebaterProfile.debater_id == Debater.id, isouter=True)
    )
    if q and q.strip():
        base = base.where(name.icontains(q.strip(), autoescape=True))
    if institution_id is not None:
        base = base.where(school == institution_id)
    if min_rounds > 0:
        base = base.where(DebaterProfile.total_rounds >= min_rounds)
    total = session.exec(select(func.count()).select_from(base.subquery())).one()
    if sort == "rank":
        # Lower rank is better, so best-first ("desc", the default) runs ascending.
        order = "asc" if order == "desc" else "desc"
    rows = session.exec(
        base.order_by(*_ordered(sort_columns[sort], order, Debater.id)).offset(offset).limit(limit)
    ).all()
    items = []
    for debater, institution, profile in rows:
        performances = sorted(
            (profile.speaker_performances if profile else []),
            key=lambda p: (p.get("date") or "", p.get("tournament_id") or 0),
        )
        items.append(
            DebaterListRow(
                debater=PersonRef(
                    id=debater.id,
                    name=debater.full_name
                    or " ".join(p for p in (debater.first_name, debater.last_name) if p)
                    or f"Debater {debater.id}",
                ),
                institution=institution_ref(institution) if institution else None,
                tournaments=profile.tournaments_debated if profile else 0,
                rounds=profile.total_rounds if profile else 0,
                average=_round(profile.average_speaker_score if profile else None),
                stdev=_round(profile.speaker_score_stdev if profile else None),
                prelim_win_rate=_round(profile.win_rate_prelims if profile else None),
                speaker_rank=profile.speaker_rank if profile else None,
                percentile=_round(profile.speaker_score_percentile if profile else None, 1),
                trend=[p["average"] for p in performances if p.get("average") is not None],
            )
        )
    return DebaterList(
        total=total,
        offset=offset,
        limit=limit,
        profile_computed_at=field.field_means(session).profile_computed_at,
        items=items,
    )


# --- Judges ---

JudgeSort = Literal["name", "debates", "tournaments", "average", "stdev", "dissent", "sentiment", "leniency", "harshness"]


class JudgeListRow(SQLModel):
    judge: PersonRef
    institution: InstitutionRef | None
    debates: int
    tournaments: int
    average: float | None
    stdev: float | None
    dissent_rate: float | None
    sentiment: float | None
    leniency: float | None
    harshness: float | None


class JudgeList(Page):
    profile_computed_at: datetime | None
    items: list[JudgeListRow]


def list_judges(
    session: Session,
    *,
    q: str | None,
    institution_id: int | None,
    min_debates: int,
    sort: JudgeSort,
    order: Order,
    offset: int,
    limit: int,
) -> JudgeList:
    name = _name_expression(Judge)
    sort_columns = {
        "name": name,
        "debates": JudgeProfile.total_judged,
        "tournaments": JudgeProfile.tournaments_judged,
        "average": JudgeProfile.average_substantive,
        "stdev": JudgeProfile.score_stdev,
        "dissent": JudgeProfile.dissent_rate,
        "sentiment": JudgeProfile.overall_sentiment,
        "leniency": JudgeProfile.leniency,
        "harshness": JudgeProfile.harshness,
    }
    base = (
        select(Judge, Institution, JudgeProfile)
        .join(Institution, Judge.institution_id == Institution.id, isouter=True)
        .join(JudgeProfile, JudgeProfile.judge_id == Judge.id, isouter=True)
    )
    if q and q.strip():
        base = base.where(name.icontains(q.strip(), autoescape=True))
    if institution_id is not None:
        base = base.where(Judge.institution_id == institution_id)
    if min_debates > 0:
        base = base.where(JudgeProfile.total_judged >= min_debates)
    total = session.exec(select(func.count()).select_from(base.subquery())).one()
    rows = session.exec(
        base.order_by(*_ordered(sort_columns[sort], order, Judge.id)).offset(offset).limit(limit)
    ).all()
    items = [
        JudgeListRow(
            judge=PersonRef(
                id=judge.id,
                name=judge.full_name
                or " ".join(p for p in (judge.first_name, judge.last_name) if p)
                or f"Judge {judge.id}",
            ),
            institution=institution_ref(institution) if institution else None,
            debates=profile.total_judged if profile else 0,
            tournaments=profile.tournaments_judged if profile else 0,
            average=_round(profile.average_substantive if profile else None),
            stdev=_round(profile.score_stdev if profile else None),
            dissent_rate=_round(profile.dissent_rate if profile else None, 1),
            sentiment=_round(profile.overall_sentiment if profile else None),
            leniency=_round(profile.leniency if profile else None),
            harshness=_round(profile.harshness if profile else None),
        )
        for judge, institution, profile in rows
    ]
    return JudgeList(
        total=total,
        offset=offset,
        limit=limit,
        profile_computed_at=field.field_means(session).profile_computed_at,
        items=items,
    )


# --- Institutions ---

InstitutionSort = Literal[
    "name", "teams", "tournaments", "debaters", "win_rate", "mean_speaks", "breaks", "finals", "titles"
]


class InstitutionListRow(SQLModel):
    institution: InstitutionRef
    district: str | None
    teams: int
    tournaments: int
    debaters: int
    win_rate: float | None
    mean_speaks: float | None
    breaks: int
    finals: int
    titles: int


class InstitutionList(Page):
    items: list[InstitutionListRow]


def list_institutions(
    session: Session,
    *,
    q: str | None,
    min_teams: int,
    sort: InstitutionSort,
    order: Order,
    offset: int,
    limit: int,
) -> InstitutionList:
    aggregates = field.institution_aggregates(session).by_id
    statement = select(Institution)
    if q and q.strip():
        statement = statement.where(
            col(Institution.name).icontains(q.strip(), autoescape=True)
            | col(Institution.code).icontains(q.strip(), autoescape=True)
        )
    rows = []
    for institution in session.exec(statement).all():
        agg = aggregates.get(institution.id) or field.InstitutionAggregate(institution_id=institution.id)
        if agg.teams < min_teams:
            continue
        rows.append(
            InstitutionListRow(
                institution=institution_ref(institution),
                district=institution.district,
                teams=agg.teams,
                tournaments=agg.tournaments,
                debaters=agg.debaters,
                win_rate=_round(agg.win_rate),
                mean_speaks=_round(agg.mean_speaks),
                breaks=agg.breaks,
                finals=agg.finals,
                titles=agg.titles,
            )
        )

    def key(row: InstitutionListRow):
        return row.institution.name.casefold() if sort == "name" else getattr(row, sort)

    # Missing values go last either way; ties keep ascending id (sorts are stable, and
    # `reverse=True` preserves the order of equal keys).
    rows.sort(key=lambda r: r.institution.id)
    present = sorted((r for r in rows if key(r) is not None), key=key, reverse=order == "desc")
    ordered = present + [r for r in rows if key(r) is None]
    return InstitutionList(
        total=len(ordered), offset=offset, limit=limit, items=ordered[offset : offset + limit]
    )


# --- Tournaments ---

TournamentSort = Literal["date", "name", "teams"]


class TournamentListRow(SQLModel):
    tournament: TournamentRef
    teams: int
    rounds: int
    champion: TeamRef | None
    finalist: TeamRef | None


class TournamentList(Page):
    items: list[TournamentListRow]


def list_tournaments(
    session: Session,
    *,
    q: str | None,
    sort: TournamentSort,
    order: Order,
    offset: int,
    limit: int,
) -> TournamentList:
    team_count = (
        select(Team.tournament_id.label("tid"), func.count(Team.id).label("n"))
        .group_by(Team.tournament_id)
        .subquery()
    )
    sort_columns = {"date": Tournament.date, "name": Tournament.name, "teams": team_count.c.n}
    base = select(Tournament, team_count.c.n).join(team_count, team_count.c.tid == Tournament.id, isouter=True)
    if q and q.strip():
        base = base.where(
            col(Tournament.name).icontains(q.strip(), autoescape=True)
            | col(Tournament.abbr).icontains(q.strip(), autoescape=True)
        )
    total = session.exec(select(func.count()).select_from(base.subquery())).one()
    rows = session.exec(
        base.order_by(*_ordered(sort_columns[sort], order, Tournament.id)).offset(offset).limit(limit)
    ).all()
    ids = [t.id for t, _n in rows]
    round_counts = dict(
        session.exec(
            select(Round.tournament_id, func.count()).where(Round.tournament_id.in_(ids or [-1])).group_by(Round.tournament_id)
        ).all()
    )
    brackets = {
        tid: derive_brackets(rounds)
        for tid, rounds in elim_rounds_by_tournament(session, tournament_ids=ids).items()
    }
    primaries = {tid: next((b for b in bs if b.primary), None) for tid, bs in brackets.items()}
    refs = team_refs(
        session,
        [x for b in primaries.values() if b for x in (b.champion_team_id, b.finalist_team_id)],
    )
    items = []
    for tournament, teams in rows:
        primary = primaries.get(tournament.id)
        items.append(
            TournamentListRow(
                tournament=tournament_ref(tournament),
                teams=teams or 0,
                rounds=round_counts.get(tournament.id, 0),
                champion=refs.get(primary.champion_team_id) if primary else None,
                finalist=refs.get(primary.finalist_team_id) if primary else None,
            )
        )
    return TournamentList(total=total, offset=offset, limit=limit, items=items)
