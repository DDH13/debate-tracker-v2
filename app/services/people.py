"""The DC's own people: the board over time, and everything recorded against one person."""

from datetime import date
from decimal import Decimal

from sqlmodel import Session, SQLModel, col, func, select

from app.models import (
    BoardRole,
    BoardRolePublic,
    Person,
    PersonPublic,
    ReimbursementStatus,
    Subscription,
    Task,
    TaskPublic,
    Transaction,
    TransactionPublic,
    User,
    UserPublic,
    Workshop,
    WorkshopFacilitator,
)
from app.services.tasks import OPEN_STATUSES

ZERO = Decimal("0.00")


class BoardRoleRow(BoardRolePublic):
    person_name: str
    # "2024–2026", "2025–" (current) or "2025".
    term_label: str


class Board(SQLModel):
    on: date
    current: list[BoardRoleRow]
    upcoming: list[BoardRoleRow]
    # Most recently ended first.
    past: list[BoardRoleRow]


def term_label(role: BoardRole) -> str:
    start = role.term_start.year
    if role.term_end is None:
        return f"{start}–"
    end = role.term_end.year
    return str(start) if end == start else f"{start}–{end}"


def is_current(role: BoardRole, on: date) -> bool:
    return role.term_start <= on and (role.term_end is None or role.term_end >= on)


def board(session: Session, on: date | None = None) -> Board:
    """Board roles split into those held on `on` (default today), those starting later, and
    past terms."""
    on = on or date.today()
    rows = session.exec(
        select(BoardRole, Person.name).join(Person, Person.id == BoardRole.person_id)
    ).all()
    as_rows = [
        BoardRoleRow(**BoardRolePublic.model_validate(r).model_dump(), person_name=name, term_label=term_label(r))
        for r, name in rows
    ]
    by_id = {r.id: r for r, _ in rows}
    current = [r for r in as_rows if is_current(by_id[r.id], on)]
    upcoming = [r for r in as_rows if r.term_start > on]
    past = [r for r in as_rows if r.term_end is not None and r.term_end < on]
    return Board(
        on=on,
        current=sorted(current, key=lambda r: (r.term_start, r.title)),
        upcoming=sorted(upcoming, key=lambda r: (r.term_start, r.title)),
        past=sorted(past, key=lambda r: (r.term_end, r.term_start), reverse=True),
    )


class FacilitatedWorkshop(SQLModel):
    workshop_id: int
    title: str
    held_on: date
    role: str | None


class PersonDetail(SQLModel):
    person: PersonPublic
    user: UserPublic | None
    board_roles: list[BoardRolePublic]
    tasks: list[TaskPublic]
    workshops: list[FacilitatedWorkshop]
    transactions: list[TransactionPublic]
    owed_lkr: Decimal


def person_detail(session: Session, person: Person) -> PersonDetail:
    user = session.exec(select(User).where(User.person_id == person.id)).first()
    roles = session.exec(
        select(BoardRole).where(BoardRole.person_id == person.id).order_by(col(BoardRole.term_start).desc())
    ).all()
    tasks = session.exec(select(Task).where(Task.assignee_person_id == person.id)).all()
    tasks = sorted(tasks, key=lambda t: (t.status not in OPEN_STATUSES, t.due_on is None, t.due_on or date.max, t.id))
    workshops = session.exec(
        select(WorkshopFacilitator, Workshop)
        .join(Workshop, Workshop.id == WorkshopFacilitator.workshop_id)
        .where(WorkshopFacilitator.person_id == person.id)
        .order_by(col(Workshop.held_on).desc())
    ).all()
    transactions = session.exec(
        select(Transaction)
        .where(Transaction.paid_by_person_id == person.id)
        .order_by(col(Transaction.occurred_on).desc(), col(Transaction.id).desc())
    ).all()
    owed = sum(
        (Decimal(t.amount_lkr) for t in transactions if t.reimbursement_status == ReimbursementStatus.PENDING),
        ZERO,
    )
    return PersonDetail(
        person=PersonPublic.model_validate(person),
        user=UserPublic.model_validate(user) if user else None,
        board_roles=[BoardRolePublic.model_validate(r) for r in roles],
        tasks=[TaskPublic.model_validate(t) for t in tasks],
        workshops=[
            FacilitatedWorkshop(workshop_id=w.id, title=w.title, held_on=w.held_on, role=wf.role)
            for wf, w in workshops
        ],
        transactions=[TransactionPublic.model_validate(t) for t in transactions],
        owed_lkr=owed,
    )


def person_references(session: Session, person_id: int) -> dict[str, int]:
    """What blocks deleting a person: records that are history rather than links."""
    counts = {
        "board role(s)": select(func.count()).select_from(BoardRole).where(BoardRole.person_id == person_id),
        "transaction(s)": select(func.count())
        .select_from(Transaction)
        .where(Transaction.paid_by_person_id == person_id),
        "subscription(s)": select(func.count())
        .select_from(Subscription)
        .where(Subscription.paid_by_person_id == person_id),
    }
    return {label: n for label, statement in counts.items() if (n := session.exec(statement).one())}
