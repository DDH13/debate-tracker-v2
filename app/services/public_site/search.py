"""Global search: one query per kind, prefix matches first, sublines from the joined rows
(or the cached institution aggregates), never a lookup per hit."""

from typing import Literal

from sqlalchemy import ColumnElement, case
from sqlmodel import Session, SQLModel, col, select

from app.models import Debater, DebaterProfile, Institution, Judge, JudgeProfile, Team, Tournament
from app.services.public_site import field
from app.services.public_site.lists import _name_expression
from app.services.public_site.schools import latest_team_school, school_id

MIN_QUERY_LENGTH = 2


class SearchHit(SQLModel):
    kind: Literal["debater", "judge", "institution", "tournament", "team"]
    id: int
    name: str
    subline: str | None = None
    tournament_id: int | None = None  # teams only: the page a team links to


class SearchResults(SQLModel):
    q: str
    debaters: list[SearchHit]
    judges: list[SearchHit]
    institutions: list[SearchHit]
    tournaments: list[SearchHit]
    teams: list[SearchHit]


def _join(*parts: str | None) -> str | None:
    text = " · ".join(p for p in parts if p)
    return text or None


def _prefix_first(name: ColumnElement, q: str) -> tuple[ColumnElement, ColumnElement]:
    return (case((name.istartswith(q, autoescape=True), 0), else_=1), name)


def search(session: Session, q: str, limit: int) -> SearchResults:
    q = q.strip()

    debater_name = _name_expression(Debater)
    latest = latest_team_school()
    debaters = [
        SearchHit(
            kind="debater",
            id=did,
            name=name or f"Debater {did}",
            subline=_join(
                school,
                f"{rounds} rounds" if rounds else None,
                f"{avg:.1f} avg" if avg is not None else None,
            ),
        )
        for did, name, school, rounds, avg in session.exec(
            select(
                Debater.id,
                debater_name,
                Institution.name,
                DebaterProfile.total_rounds,
                DebaterProfile.average_speaker_score,
            )
            .join(latest, latest.c.debater_id == Debater.id, isouter=True)
            .join(Institution, school_id(latest) == Institution.id, isouter=True)
            .join(DebaterProfile, DebaterProfile.debater_id == Debater.id, isouter=True)
            .where(debater_name.icontains(q, autoescape=True))
            .order_by(*_prefix_first(debater_name, q), Debater.id)
            .limit(limit)
        ).all()
    ]

    judge_name = _name_expression(Judge)
    judges = [
        SearchHit(
            kind="judge",
            id=jid,
            name=name or f"Judge {jid}",
            subline=_join(school, f"{debates} debates" if debates else None),
        )
        for jid, name, school, debates in session.exec(
            select(Judge.id, judge_name, Institution.name, JudgeProfile.total_judged)
            .join(Institution, Judge.institution_id == Institution.id, isouter=True)
            .join(JudgeProfile, JudgeProfile.judge_id == Judge.id, isouter=True)
            .where(judge_name.icontains(q, autoescape=True))
            .order_by(*_prefix_first(judge_name, q), Judge.id)
            .limit(limit)
        ).all()
    ]

    aggregates = field.institution_aggregates(session).by_id
    institutions = []
    for iid, name, code in session.exec(
        select(Institution.id, Institution.name, Institution.code)
        .where(
            col(Institution.name).icontains(q, autoescape=True)
            | col(Institution.code).icontains(q, autoescape=True)
        )
        .order_by(*_prefix_first(col(Institution.name), q), Institution.id)
        .limit(limit)
    ).all():
        agg = aggregates.get(iid)
        institutions.append(
            SearchHit(
                kind="institution",
                id=iid,
                name=name,
                subline=_join(
                    f"{agg.teams} teams" if agg and agg.teams else None,
                    f"{agg.titles} titles" if agg and agg.titles else None,
                ),
            )
        )

    tournaments = [
        SearchHit(
            kind="tournament",
            id=tid,
            name=name,
            subline=_join(abbr, str(day.year) if day else None),
        )
        for tid, name, abbr, day in session.exec(
            select(Tournament.id, Tournament.name, Tournament.abbr, Tournament.date)
            .where(
                col(Tournament.name).icontains(q, autoescape=True)
                | col(Tournament.abbr).icontains(q, autoescape=True)
            )
            .order_by(*_prefix_first(col(Tournament.name), q), col(Tournament.id).desc())
            .limit(limit)
        ).all()
    ]

    teams = [
        SearchHit(kind="team", id=team_id, name=name, subline=_join(tournament_name, school), tournament_id=tid)
        for team_id, name, tid, tournament_name, school in session.exec(
            select(Team.id, Team.name, Tournament.id, Tournament.name, Institution.name)
            .join(Tournament, Team.tournament_id == Tournament.id)
            .join(Institution, Team.institution_id == Institution.id, isouter=True)
            .where(col(Team.name).icontains(q, autoescape=True))
            .order_by(*_prefix_first(col(Team.name), q), Team.id)
            .limit(limit)
        ).all()
    ]

    return SearchResults(
        q=q, debaters=debaters, judges=judges, institutions=institutions, tournaments=tournaments, teams=teams
    )
