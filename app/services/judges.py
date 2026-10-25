"""The judge registry: the board's level and notes for each judge, the schools they're
affiliated with, and how involved they've been.

Involvement is computed live from the imported panels (`DebateJudge`), so it's current right
after an import. The materialized `JudgeProfile` (scores, dissents) only updates on
`POST /stats/refresh`; the detail view includes it when present.
"""

from datetime import date

from sqlalchemy import case
from sqlmodel import Session, SQLModel, col, func, select

from app.models import (
    AffiliationRole,
    Debate,
    DebateJudge,
    Feedback,
    FeedbackPublic,
    Institution,
    Judge,
    JudgeAffiliation,
    JudgeLevel,
    JudgeProfile,
    JudgeProfilePublic,
    JudgePublic,
    JudgeRecord,
    JudgeRecordPublic,
    JudgeRecordUpdate,
    Round,
    Tournament,
)


def display_name(judge: Judge) -> str:
    if judge.full_name:
        return judge.full_name
    name = " ".join(p for p in (judge.first_name, judge.last_name) if p)
    return name or f"Judge {judge.id}"


class Involvement(SQLModel):
    tournaments: int = 0
    debates: int = 0
    prelims: int = 0
    elims: int = 0
    chaired: int = 0
    trainee: int = 0
    last_tournament_id: int | None = None
    last_tournament_name: str | None = None
    last_tournament_date: date | None = None


def _panels(*columns):
    """`columns` over every panel seat, joined through to its round."""
    return (
        select(*columns)
        .select_from(DebateJudge)
        .join(Debate, Debate.id == DebateJudge.debate_id)
        .join(Round, Round.id == Debate.round_id)
    )


def involvement(session: Session, judge_ids: list[int] | None = None) -> dict[int, Involvement]:
    """Per judge: tournaments and debates judged (prelims / elims), chairs, trainee panels and
    the most recent tournament (by date) they judged at."""
    counts = _panels(
        DebateJudge.judge_id,
        func.count(),
        func.sum(case((Round.isElimination == True, 1), else_=0)),  # noqa: E712
        func.sum(case((DebateJudge.is_chair == True, 1), else_=0)),  # noqa: E712
        func.sum(case((DebateJudge.is_trainee == True, 1), else_=0)),  # noqa: E712
        func.count(func.distinct(Round.tournament_id)),
    ).group_by(DebateJudge.judge_id)
    tournaments = (
        _panels(DebateJudge.judge_id, Tournament.id, Tournament.name, Tournament.date)
        .join(Tournament, Tournament.id == Round.tournament_id)
        .distinct()
    )
    if judge_ids is not None:
        counts = counts.where(col(DebateJudge.judge_id).in_(judge_ids))
        tournaments = tournaments.where(col(DebateJudge.judge_id).in_(judge_ids))

    result: dict[int, Involvement] = {}
    for judge_id, debates, elims, chaired, trainee, n_tournaments in session.exec(counts).all():
        result[judge_id] = Involvement(
            tournaments=n_tournaments,
            debates=debates,
            prelims=debates - (elims or 0),
            elims=elims or 0,
            chaired=chaired or 0,
            trainee=trainee or 0,
        )
    for judge_id, tid, name, when in session.exec(tournaments).all():
        inv = result.get(judge_id)
        if inv is None:
            continue
        newer = inv.last_tournament_id is None or (
            (when or date.min, tid) > (inv.last_tournament_date or date.min, inv.last_tournament_id)
        )
        if newer:
            inv.last_tournament_id, inv.last_tournament_name, inv.last_tournament_date = tid, name, when
    return result


# --- Registry -----------------------------------------------------------------------------


class JudgeRegistryRow(SQLModel):
    judge_id: int
    name: str
    institution_id: int | None
    institution_name: str | None
    level: JudgeLevel | None
    affiliations: int
    involvement: Involvement


def judge_registry(
    session: Session,
    q: str | None = None,
    level: JudgeLevel | None = None,
    institution_id: int | None = None,
) -> list[JudgeRegistryRow]:
    """Every judge (filtered), most recently active first, then by name. Unpaginated: the
    console pages through it client-side."""
    statement = select(Judge, Institution.name).join(
        Institution, Institution.id == Judge.institution_id, isouter=True
    )
    if institution_id is not None:
        statement = statement.where(Judge.institution_id == institution_id)
    judges = session.exec(statement).all()
    records = {r.judge_id: r for r in session.exec(select(JudgeRecord)).all()}
    affiliations = dict(
        session.exec(
            select(JudgeAffiliation.judge_id, func.count()).group_by(JudgeAffiliation.judge_id)
        ).all()
    )
    activity = involvement(session)
    needle = q.strip().lower() if q and q.strip() else None

    rows = []
    for judge, school in judges:
        name = display_name(judge)
        record = records.get(judge.id)
        if needle and needle not in name.lower():
            continue
        if level is not None and (record is None or record.level != level):
            continue
        rows.append(
            JudgeRegistryRow(
                judge_id=judge.id,
                name=name,
                institution_id=judge.institution_id,
                institution_name=school,
                level=record.level if record else None,
                affiliations=affiliations.get(judge.id, 0),
                involvement=activity.get(judge.id, Involvement()),
            )
        )
    rows.sort(
        key=lambda r: (
            r.involvement.last_tournament_date is None,
            -(r.involvement.last_tournament_date.toordinal() if r.involvement.last_tournament_date else 0),
            r.name.lower(),
        )
    )
    return rows


# --- Detail -------------------------------------------------------------------------------


class AffiliationRow(SQLModel):
    id: int
    institution_id: int
    institution_name: str
    role: AffiliationRole
    from_year: int | None
    to_year: int | None
    notes: str | None


class TournamentJudged(SQLModel):
    tournament_id: int
    name: str
    date: date | None
    debates: int
    chaired: int
    elims: int
    trainee: int


class JudgeDetail(SQLModel):
    judge: JudgePublic
    name: str
    institution_name: str | None
    record: JudgeRecordPublic | None
    affiliations: list[AffiliationRow]
    involvement: Involvement
    history: list[TournamentJudged]
    profile: JudgeProfilePublic | None
    feedback: list[FeedbackPublic]


def judge_detail(session: Session, judge: Judge) -> JudgeDetail:
    school = session.get(Institution, judge.institution_id) if judge.institution_id else None
    record = session.exec(select(JudgeRecord).where(JudgeRecord.judge_id == judge.id)).first()
    affiliations = session.exec(
        select(JudgeAffiliation, Institution.name)
        .join(Institution, Institution.id == JudgeAffiliation.institution_id)
        .where(JudgeAffiliation.judge_id == judge.id)
        .order_by(col(JudgeAffiliation.from_year).desc(), JudgeAffiliation.id)
    ).all()
    history_rows = session.exec(
        _panels(
            Tournament.id,
            Tournament.name,
            Tournament.date,
            func.count(),
            func.sum(case((DebateJudge.is_chair == True, 1), else_=0)),  # noqa: E712
            func.sum(case((Round.isElimination == True, 1), else_=0)),  # noqa: E712
            func.sum(case((DebateJudge.is_trainee == True, 1), else_=0)),  # noqa: E712
        )
        .join(Tournament, Tournament.id == Round.tournament_id)
        .where(DebateJudge.judge_id == judge.id)
        .group_by(Tournament.id, Tournament.name, Tournament.date)
    ).all()
    history = sorted(
        (
            TournamentJudged(
                tournament_id=tid, name=name, date=when, debates=n, chaired=ch or 0, elims=el or 0, trainee=tr or 0
            )
            for tid, name, when, n, ch, el, tr in history_rows
        ),
        key=lambda h: (h.date is None, h.date or date.min, h.tournament_id),
        reverse=True,
    )
    profile = session.exec(select(JudgeProfile).where(JudgeProfile.judge_id == judge.id)).first()
    feedback = session.exec(
        select(Feedback).where(Feedback.judge_id == judge.id).order_by(col(Feedback.created_at).desc())
    ).all()
    return JudgeDetail(
        judge=JudgePublic.model_validate(judge),
        name=display_name(judge),
        institution_name=school.name if school else None,
        record=JudgeRecordPublic.model_validate(record) if record else None,
        affiliations=[
            AffiliationRow(
                id=a.id,
                institution_id=a.institution_id,
                institution_name=name,
                role=a.role,
                from_year=a.from_year,
                to_year=a.to_year,
                notes=a.notes,
            )
            for a, name in affiliations
        ],
        involvement=involvement(session, [judge.id]).get(judge.id, Involvement()),
        history=history,
        profile=JudgeProfilePublic.model_validate(profile) if profile else None,
        feedback=[FeedbackPublic.model_validate(f) for f in feedback],
    )


def upsert_record(session: Session, judge: Judge, body: JudgeRecordUpdate) -> JudgeRecord:
    record = session.exec(select(JudgeRecord).where(JudgeRecord.judge_id == judge.id)).first()
    if record is None:
        record = JudgeRecord(judge_id=judge.id)
    record.sqlmodel_update(body.model_dump())
    session.add(record)
    session.commit()
    session.refresh(record)
    return record


class AffiliatedJudge(SQLModel):
    judge_id: int
    name: str
    role: AffiliationRole
    from_year: int | None
    to_year: int | None


def school_affiliated_judges(session: Session, institution_id: int) -> list[AffiliatedJudge]:
    rows = session.exec(
        select(JudgeAffiliation, Judge)
        .join(Judge, Judge.id == JudgeAffiliation.judge_id)
        .where(JudgeAffiliation.institution_id == institution_id)
    ).all()
    judges = [
        AffiliatedJudge(judge_id=j.id, name=display_name(j), role=a.role, from_year=a.from_year, to_year=a.to_year)
        for a, j in rows
    ]
    return sorted(judges, key=lambda r: r.name.lower())
