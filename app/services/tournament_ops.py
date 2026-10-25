"""Tournament operations: which schools dropped teams and which didn't bring the judges
they owed.

The board enters registrations (teams registered, judges promised). Teams fielded and
judges provided come from the imported tab, and judges required from the tournament's quota
rule ("1 judge per N teams", rounded up); each can be overridden per school. When the tab
hasn't been imported (no teams at all), fielded and provided are unknown rather than zero,
so no dropouts or shortfalls are invented.
"""

import math
from collections import defaultdict
from datetime import date

from sqlmodel import Session, SQLModel, col, func, select

from app.models import (
    Debate,
    DebateJudge,
    Feedback,
    FeedbackPublic,
    Institution,
    Judge,
    Round,
    Team,
    Tournament,
    TournamentOps,
    TournamentOpsUpdate,
    TournamentRegistration,
)

DEFAULT_SETTINGS = TournamentOpsUpdate()


class OpsSettings(SQLModel):
    teams_per_judge: int
    hosted_by_dc: bool
    notes: str | None


class OpsRow(SQLModel):
    institution_id: int
    name: str
    registration_id: int | None
    teams_registered: int | None
    # Override when set, else from the tab; None when neither is known.
    teams_fielded: int | None
    teams_fielded_from_tab: int | None
    fielded_overridden: bool
    dropouts: int | None
    judges_required: int
    required_overridden: bool
    judges_promised: int | None
    judges_provided: int | None
    judges_provided_from_tab: int | None
    provided_overridden: bool
    shortfall: int | None
    dropout_notes: str | None
    notes: str | None


class OpsTotals(SQLModel):
    teams_registered: int = 0
    teams_fielded: int = 0
    dropouts: int = 0
    judges_required: int = 0
    judges_provided: int = 0
    shortfall: int = 0


class TournamentOpsView(SQLModel):
    tournament_id: int
    name: str
    date: date | None
    settings: OpsSettings
    # False until teams have been imported: fielded / provided are then unknown.
    tab_imported: bool
    rows: list[OpsRow]
    totals: OpsTotals
    feedback: list[FeedbackPublic]


class OpsSummaryRow(SQLModel):
    tournament_id: int
    name: str
    date: date | None
    hosted_by_dc: bool
    tab_imported: bool
    schools: int
    registrations: int
    dropouts: int
    shortfall: int


class SchoolTournamentRow(OpsRow):
    tournament_id: int
    tournament_name: str
    tournament_date: date | None


# --- Derived counts -----------------------------------------------------------------------


def _teams_by_school(session: Session, tournament_ids: list[int]) -> dict[tuple[int, int], int]:
    rows = session.exec(
        select(Team.tournament_id, Team.institution_id, func.count())
        .where(col(Team.tournament_id).in_(tournament_ids), col(Team.institution_id).is_not(None))
        .group_by(Team.tournament_id, Team.institution_id)
    ).all()
    return {(tid, iid): n for tid, iid, n in rows}


def _judges_by_school(session: Session, tournament_ids: list[int]) -> dict[tuple[int, int], int]:
    """Distinct judges from each school who sat on at least one panel of the tournament as a
    full judge. Trainees don't count towards a quota."""
    rows = session.exec(
        select(Round.tournament_id, Judge.institution_id, func.count(func.distinct(Judge.id)))
        .select_from(DebateJudge)
        .join(Debate, Debate.id == DebateJudge.debate_id)
        .join(Round, Round.id == Debate.round_id)
        .join(Judge, Judge.id == DebateJudge.judge_id)
        .where(
            col(Round.tournament_id).in_(tournament_ids),
            col(Judge.institution_id).is_not(None),
            DebateJudge.is_trainee == False,  # noqa: E712
        )
        .group_by(Round.tournament_id, Judge.institution_id)
    ).all()
    return {(tid, iid): n for tid, iid, n in rows}


def _tournaments_with_teams(session: Session, tournament_ids: list[int]) -> set[int]:
    return set(
        session.exec(select(Team.tournament_id).where(col(Team.tournament_id).in_(tournament_ids)).distinct()).all()
    )


def _settings(session: Session, tournament_ids: list[int]) -> dict[int, OpsSettings]:
    stored = {
        s.tournament_id: s
        for s in session.exec(select(TournamentOps).where(col(TournamentOps.tournament_id).in_(tournament_ids))).all()
    }
    defaults = OpsSettings(**DEFAULT_SETTINGS.model_dump())
    return {
        tid: OpsSettings(teams_per_judge=s.teams_per_judge, hosted_by_dc=s.hosted_by_dc, notes=s.notes)
        if (s := stored.get(tid))
        else defaults
        for tid in tournament_ids
    }


def _row(
    institution_id: int,
    name: str,
    reg: TournamentRegistration | None,
    teams_from_tab: int | None,
    judges_from_tab: int | None,
    teams_per_judge: int,
) -> OpsRow:
    registered = reg.teams_registered if reg else None
    fielded_override = reg.teams_fielded_override if reg else None
    provided_override = reg.judges_provided_override if reg else None
    required_override = reg.judges_required_override if reg else None

    fielded = fielded_override if fielded_override is not None else teams_from_tab
    provided = provided_override if provided_override is not None else judges_from_tab
    quota_teams = fielded if fielded is not None else (registered or 0)
    required = required_override if required_override is not None else math.ceil(quota_teams / teams_per_judge)
    return OpsRow(
        institution_id=institution_id,
        name=name,
        registration_id=reg.id if reg else None,
        teams_registered=registered,
        teams_fielded=fielded,
        teams_fielded_from_tab=teams_from_tab,
        fielded_overridden=fielded_override is not None,
        dropouts=max(registered - fielded, 0) if registered is not None and fielded is not None else None,
        judges_required=required,
        required_overridden=required_override is not None,
        judges_promised=reg.judges_promised if reg else None,
        judges_provided=provided,
        judges_provided_from_tab=judges_from_tab,
        provided_overridden=provided_override is not None,
        shortfall=max(required - provided, 0) if provided is not None else None,
        dropout_notes=reg.dropout_notes if reg else None,
        notes=reg.notes if reg else None,
    )


def _rows_by_tournament(
    session: Session, tournament_ids: list[int]
) -> tuple[dict[int, list[OpsRow]], dict[int, OpsSettings], set[int]]:
    """Every school row for each tournament: one per registration or school with imported
    teams, sorted by school name."""
    if not tournament_ids:
        return {}, {}, set()
    settings = _settings(session, tournament_ids)
    imported = _tournaments_with_teams(session, tournament_ids)
    teams = _teams_by_school(session, tournament_ids)
    judges = _judges_by_school(session, tournament_ids)
    registrations = {
        (r.tournament_id, r.institution_id): r
        for r in session.exec(
            select(TournamentRegistration).where(col(TournamentRegistration.tournament_id).in_(tournament_ids))
        ).all()
    }
    keys = set(registrations) | set(teams)
    names = dict(
        session.exec(
            select(Institution.id, Institution.name).where(col(Institution.id).in_({iid for _, iid in keys}))
        ).all()
    ) if keys else {}

    rows: dict[int, list[OpsRow]] = defaultdict(list)
    for tid, iid in keys:
        tab = tid in imported
        rows[tid].append(
            _row(
                iid,
                names.get(iid, f"Institution {iid}"),
                registrations.get((tid, iid)),
                teams.get((tid, iid), 0) if tab else None,
                judges.get((tid, iid), 0) if tab else None,
                settings[tid].teams_per_judge,
            )
        )
    for tid in rows:
        rows[tid].sort(key=lambda r: r.name.lower())
    return rows, settings, imported


def _totals(rows: list[OpsRow]) -> OpsTotals:
    return OpsTotals(
        teams_registered=sum(r.teams_registered or 0 for r in rows),
        teams_fielded=sum(r.teams_fielded or 0 for r in rows),
        dropouts=sum(r.dropouts or 0 for r in rows),
        judges_required=sum(r.judges_required for r in rows),
        judges_provided=sum(r.judges_provided or 0 for r in rows),
        shortfall=sum(r.shortfall or 0 for r in rows),
    )


# --- Views --------------------------------------------------------------------------------


def tournament_ops(session: Session, tournament: Tournament) -> TournamentOpsView:
    rows, settings, imported = _rows_by_tournament(session, [tournament.id])
    mine = rows.get(tournament.id, [])
    feedback = session.exec(
        select(Feedback).where(Feedback.tournament_id == tournament.id).order_by(col(Feedback.created_at).desc())
    ).all()
    return TournamentOpsView(
        tournament_id=tournament.id,
        name=tournament.name,
        date=tournament.date,
        settings=settings[tournament.id],
        tab_imported=tournament.id in imported,
        rows=mine,
        totals=_totals(mine),
        feedback=[FeedbackPublic.model_validate(f) for f in feedback],
    )


def ops_summary(session: Session) -> list[OpsSummaryRow]:
    """Every tournament, newest first (undated last), with its dropout and shortfall totals."""
    tournaments = session.exec(select(Tournament)).all()
    tournaments = sorted(tournaments, key=lambda t: (t.date is None, -(t.date.toordinal() if t.date else 0), -t.id))
    rows, settings, imported = _rows_by_tournament(session, [t.id for t in tournaments])
    summary = []
    for t in tournaments:
        mine = rows.get(t.id, [])
        totals = _totals(mine)
        summary.append(
            OpsSummaryRow(
                tournament_id=t.id,
                name=t.name,
                date=t.date,
                hosted_by_dc=settings[t.id].hosted_by_dc,
                tab_imported=t.id in imported,
                schools=len(mine),
                registrations=sum(1 for r in mine if r.registration_id is not None),
                dropouts=totals.dropouts,
                shortfall=totals.shortfall,
            )
        )
    return summary


def school_tournament_record(session: Session, institution_id: int) -> list[SchoolTournamentRow]:
    """The school's row at every tournament it registered for or fielded teams at."""
    tournament_ids = set(
        session.exec(
            select(TournamentRegistration.tournament_id).where(TournamentRegistration.institution_id == institution_id)
        ).all()
    ) | set(session.exec(select(Team.tournament_id).where(Team.institution_id == institution_id)).all())
    if not tournament_ids:
        return []
    tournaments = {t.id: t for t in session.exec(select(Tournament).where(col(Tournament.id).in_(tournament_ids))).all()}
    rows, _, _ = _rows_by_tournament(session, list(tournament_ids))
    record = [
        SchoolTournamentRow(
            **row.model_dump(),
            tournament_id=tid,
            tournament_name=tournaments[tid].name,
            tournament_date=tournaments[tid].date,
        )
        for tid in tournament_ids
        for row in rows.get(tid, [])
        if row.institution_id == institution_id
    ]
    return sorted(record, key=lambda r: (r.tournament_date is None, r.tournament_date or date.min), reverse=True)


def upsert_settings(session: Session, tournament: Tournament, body: TournamentOpsUpdate) -> OpsSettings:
    ops = session.exec(select(TournamentOps).where(TournamentOps.tournament_id == tournament.id)).first()
    if ops is None:
        ops = TournamentOps(tournament_id=tournament.id)
    ops.sqlmodel_update(body.model_dump())
    session.add(ops)
    session.commit()
    return OpsSettings(teams_per_judge=ops.teams_per_judge, hosted_by_dc=ops.hosted_by_dc, notes=ops.notes)
