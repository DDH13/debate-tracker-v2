"""Workshops: who came (schools and their student headcount), who ran them, and the money.

Money is ordinary ledger rows tagged with `Transaction.workshop_id`: fees as income, venue
and other costs as expenses. A pending reimbursement counts as spent, as it does in the
finance reports. Amounts are LKR `Decimal`s.
"""

from collections import defaultdict
from datetime import date
from decimal import Decimal

from sqlmodel import Session, SQLModel, col, func, select

from app.models import (
    Feedback,
    FeedbackPublic,
    Institution,
    Person,
    Task,
    TaskPublic,
    Transaction,
    TransactionKind,
    TransactionPublic,
    Workshop,
    WorkshopFacilitator,
    WorkshopMode,
    WorkshopPublic,
    WorkshopSchool,
    WorkshopStatus,
)

ZERO = Decimal("0.00")


class WorkshopConflict(ValueError):
    """The action doesn't fit the workshop's current state (→ 409)."""


class WorkshopSchoolRow(SQLModel):
    id: int
    institution_id: int
    name: str
    students: int | None
    notes: str | None


class WorkshopFacilitatorRow(SQLModel):
    id: int
    person_id: int
    name: str
    role: str | None


class WorkshopMoney(SQLModel):
    income_lkr: Decimal = ZERO
    expense_lkr: Decimal = ZERO
    net_lkr: Decimal = ZERO


class WorkshopDetail(WorkshopMoney):
    workshop: WorkshopPublic
    schools: list[WorkshopSchoolRow]
    facilitators: list[WorkshopFacilitatorRow]
    headcount: int
    transactions: list[TransactionPublic]
    feedback: list[FeedbackPublic]
    tasks: list[TaskPublic]


class WorkshopSummary(WorkshopMoney):
    id: int
    title: str
    held_on: date
    mode: WorkshopMode
    status: WorkshopStatus
    schools: int
    headcount: int
    facilitators: int


def _money(income: Decimal, expense: Decimal) -> dict[str, Decimal]:
    return {"income_lkr": income, "expense_lkr": expense, "net_lkr": income - expense}


def _money_by_workshop(session: Session, workshop_ids: list[int]) -> dict[int, dict[str, Decimal]]:
    totals: dict[int, dict[TransactionKind, Decimal]] = defaultdict(lambda: defaultdict(lambda: ZERO))
    if workshop_ids:
        rows = session.exec(
            select(Transaction.workshop_id, Transaction.kind, func.sum(Transaction.amount_lkr))
            .where(col(Transaction.workshop_id).in_(workshop_ids))
            .group_by(Transaction.workshop_id, Transaction.kind)
        ).all()
        for workshop_id, kind, total in rows:
            totals[workshop_id][kind] += Decimal(total)
    return {
        wid: _money(totals[wid][TransactionKind.INCOME], totals[wid][TransactionKind.EXPENSE])
        for wid in workshop_ids
    }


def workshop_detail(session: Session, workshop: Workshop) -> WorkshopDetail:
    schools = session.exec(
        select(WorkshopSchool, Institution.name)
        .join(Institution, Institution.id == WorkshopSchool.institution_id)
        .where(WorkshopSchool.workshop_id == workshop.id)
        .order_by(Institution.name)
    ).all()
    facilitators = session.exec(
        select(WorkshopFacilitator, Person.name)
        .join(Person, Person.id == WorkshopFacilitator.person_id)
        .where(WorkshopFacilitator.workshop_id == workshop.id)
        .order_by(Person.name)
    ).all()
    transactions = session.exec(
        select(Transaction)
        .where(Transaction.workshop_id == workshop.id)
        .order_by(col(Transaction.occurred_on).desc(), col(Transaction.id).desc())
    ).all()
    feedback = session.exec(
        select(Feedback).where(Feedback.workshop_id == workshop.id).order_by(col(Feedback.created_at).desc())
    ).all()
    tasks = session.exec(select(Task).where(Task.workshop_id == workshop.id).order_by(Task.id)).all()

    income = sum((Decimal(t.amount_lkr) for t in transactions if t.kind == TransactionKind.INCOME), ZERO)
    expense = sum((Decimal(t.amount_lkr) for t in transactions if t.kind == TransactionKind.EXPENSE), ZERO)
    return WorkshopDetail(
        workshop=WorkshopPublic.model_validate(workshop),
        schools=[
            WorkshopSchoolRow(id=ws.id, institution_id=ws.institution_id, name=name, students=ws.students, notes=ws.notes)
            for ws, name in schools
        ],
        facilitators=[
            WorkshopFacilitatorRow(id=wf.id, person_id=wf.person_id, name=name, role=wf.role)
            for wf, name in facilitators
        ],
        headcount=sum(ws.students or 0 for ws, _ in schools) + workshop.other_attendees,
        transactions=[TransactionPublic.model_validate(t) for t in transactions],
        feedback=[FeedbackPublic.model_validate(f) for f in feedback],
        tasks=[TaskPublic.model_validate(t) for t in tasks],
        **_money(income, expense),
    )


def workshop_summaries(session: Session, year: int | None = None) -> list[WorkshopSummary]:
    """Every workshop (in `year` when given), newest first, with grouped counts and money."""
    statement = select(Workshop)
    if year is not None:
        statement = statement.where(Workshop.held_on >= date(year, 1, 1), Workshop.held_on <= date(year, 12, 31))
    workshops = session.exec(statement.order_by(col(Workshop.held_on).desc(), col(Workshop.id).desc())).all()
    ids = [w.id for w in workshops]
    if not ids:
        return []

    schools = {
        wid: (n, students or 0)
        for wid, n, students in session.exec(
            select(WorkshopSchool.workshop_id, func.count(), func.sum(WorkshopSchool.students))
            .where(col(WorkshopSchool.workshop_id).in_(ids))
            .group_by(WorkshopSchool.workshop_id)
        ).all()
    }
    facilitators = dict(
        session.exec(
            select(WorkshopFacilitator.workshop_id, func.count())
            .where(col(WorkshopFacilitator.workshop_id).in_(ids))
            .group_by(WorkshopFacilitator.workshop_id)
        ).all()
    )
    money = _money_by_workshop(session, ids)
    return [
        WorkshopSummary(
            id=w.id,
            title=w.title,
            held_on=w.held_on,
            mode=w.mode,
            status=w.status,
            schools=schools.get(w.id, (0, 0))[0],
            headcount=schools.get(w.id, (0, 0))[1] + w.other_attendees,
            facilitators=facilitators.get(w.id, 0),
            **money[w.id],
        )
        for w in workshops
    ]


class SchoolWorkshop(SQLModel):
    workshop_id: int
    title: str
    held_on: date
    status: WorkshopStatus
    students: int | None


def school_workshops(session: Session, institution_id: int) -> list[SchoolWorkshop]:
    rows = session.exec(
        select(WorkshopSchool, Workshop)
        .join(Workshop, Workshop.id == WorkshopSchool.workshop_id)
        .where(WorkshopSchool.institution_id == institution_id)
        .order_by(col(Workshop.held_on).desc())
    ).all()
    return [
        SchoolWorkshop(workshop_id=w.id, title=w.title, held_on=w.held_on, status=w.status, students=ws.students)
        for ws, w in rows
    ]


def ensure_deletable(session: Session, workshop: Workshop) -> None:
    """Schools and facilitators go with the workshop; ledger entries are money history and
    block the delete. Feedback and task links are nulled."""
    ledger = session.exec(
        select(func.count()).select_from(Transaction).where(Transaction.workshop_id == workshop.id)
    ).one()
    if ledger:
        raise WorkshopConflict(
            f"Workshop {workshop.id} has {ledger} ledger entr{'ies' if ledger != 1 else 'y'}: "
            "unlink or delete them first"
        )
