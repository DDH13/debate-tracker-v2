"""One rule for "which school is this debater from", shared by the debater page, the school
roster and counts, the debater list (column and filter) and search.

A debater's school is `Debater.institution_id`, else the institution of their most recent team
that has one (by tournament date, undated last, then newest tournament, then newest team).
Imported debaters usually have no school of their own, so the fallback is the common case.

`latest_team_school()` is a grouped subquery (a window function, no per-row queries) to
outer-join on `debater_id`; `school_id()` is the effective-school expression over it."""

from sqlalchemy import ColumnElement
from sqlalchemy.sql.selectable import Subquery
from sqlmodel import Session, func, select

from app.models import Debater, Team, TeamMember, Tournament


def latest_team_school() -> Subquery:
    ranked = (
        select(
            TeamMember.debater_id.label("debater_id"),
            Team.institution_id.label("institution_id"),
            func.row_number()
            .over(
                partition_by=TeamMember.debater_id,
                order_by=(Tournament.date.desc().nulls_last(), Tournament.id.desc(), Team.id.desc()),
            )
            .label("rn"),
        )
        .join(Team, TeamMember.team_id == Team.id)
        .join(Tournament, Team.tournament_id == Tournament.id)
        .where(Team.institution_id.is_not(None))
        .subquery()
    )
    return select(ranked.c.debater_id, ranked.c.institution_id).where(ranked.c.rn == 1).subquery()


def school_id(latest: Subquery) -> ColumnElement:
    return func.coalesce(Debater.institution_id, latest.c.institution_id)


def debater_school_id(session: Session, debater: Debater) -> int | None:
    if debater.institution_id is not None:
        return debater.institution_id
    latest = latest_team_school()
    return session.exec(select(latest.c.institution_id).where(latest.c.debater_id == debater.id)).first()
