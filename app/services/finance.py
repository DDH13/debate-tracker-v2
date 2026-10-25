"""The DC's ledger: transaction rules, account balances, the dashboard summary, and the
actions that create transactions for something else (a subscription bill, a membership fee,
paying someone back).

Amounts are LKR `Decimal`s throughout. Income and expense totals are by `occurred_on` (when
the money was earned or spent, even if a person fronted it). Account balances are by when
money actually moved: a pending reimbursement hasn't touched any DC account yet, and a
reimbursed one moved on `reimbursed_on`.
"""

import calendar
import csv
import io
from collections import defaultdict
from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from pydantic import field_validator
from sqlalchemy import func, or_
from sqlmodel import Field, Session, SQLModel, col, select

from app.models import (
    Account,
    AccountKind,
    BillingCycle,
    FinanceCategory,
    Institution,
    Membership,
    PaymentMethod,
    Person,
    ReimbursementStatus,
    Subscription,
    Tournament,
    Transaction,
    TransactionKind,
    Trip,
    TripTraveller,
    User,
)
from app.models.membership import http_url

ZERO = Decimal("0")
RENEWAL_WINDOW_DAYS = 30
MEMBERSHIP_CATEGORY = "Membership fees"

DEFAULT_CATEGORIES: dict[TransactionKind, tuple[str, ...]] = {
    TransactionKind.INCOME: (
        MEMBERSHIP_CATEGORY,
        "Sponsorship",
        "Tournament fees",
        "Workshop fees",
        "Donations",
        "Other income",
    ),
    TransactionKind.EXPENSE: (
        "Subscriptions",
        "Travel",
        "Venue",
        "Food & refreshments",
        "Printing",
        "Equipment",
        "Workshops",
        "Tournament costs",
        "WSDC",
        "Bank charges",
        "Other expenses",
    ),
}

CYCLE_MONTHS = {BillingCycle.MONTHLY: 1, BillingCycle.QUARTERLY: 3, BillingCycle.YEARLY: 12}


class FinanceInvalid(ValueError):
    """The request breaks a ledger rule (→ 422)."""


class FinanceConflict(ValueError):
    """The action doesn't fit the record's current state (→ 409)."""


# --- Categories ---------------------------------------------------------------------------


def ensure_default_categories(session: Session) -> int:
    """Adds any default category that's missing (by name and kind) and returns how many it
    added. Never touches existing ones, so renamed or deactivated categories stay as the
    board left them; a renamed default does come back, though, under its default name."""
    existing = {(c.name, c.kind) for c in session.exec(select(FinanceCategory)).all()}
    added = 0
    for kind, names in DEFAULT_CATEGORIES.items():
        for name in names:
            if (name, kind) not in existing:
                session.add(FinanceCategory(name=name, kind=kind))
                added += 1
    if added:
        session.commit()
    return added


# --- Transaction rules --------------------------------------------------------------------


def validate_transaction(session: Session, txn: Transaction) -> None:
    """Raises `FinanceInvalid` unless `txn` (with any pending changes applied) is a valid
    ledger line. Called before every create and update."""
    pending = txn.reimbursement_status == ReimbursementStatus.PENDING

    if txn.kind == TransactionKind.TRANSFER:
        if txn.account_id is None or txn.to_account_id is None:
            raise FinanceInvalid("A transfer needs both a from and a to account")
        if txn.account_id == txn.to_account_id:
            raise FinanceInvalid("A transfer's from and to accounts must differ")
        if txn.category_id is not None:
            raise FinanceInvalid("A transfer has no category")
        if txn.reimbursement_status != ReimbursementStatus.NONE or txn.paid_by_person_id is not None:
            raise FinanceInvalid("A transfer can't be paid by a person or reimbursed")
    else:
        if txn.to_account_id is not None:
            raise FinanceInvalid("Only a transfer has a to account")
        if txn.category_id is None:
            raise FinanceInvalid(f"An {txn.kind.value} needs a category")
        category = session.get(FinanceCategory, txn.category_id)
        if category is not None and category.kind != txn.kind:
            raise FinanceInvalid(f"Category {category.name!r} is for {category.kind.value}, not {txn.kind.value}")

    if txn.account_id is None and not pending:
        raise FinanceInvalid("An account is required (except for a pending reimbursement)")

    if txn.reimbursement_status != ReimbursementStatus.NONE:
        if txn.kind != TransactionKind.EXPENSE:
            raise FinanceInvalid("Only an expense can be reimbursed")
        if txn.paid_by_person_id is None:
            raise FinanceInvalid("A reimbursement needs the person who paid")
    if txn.reimbursement_status == ReimbursementStatus.REIMBURSED and txn.reimbursed_on is None:
        raise FinanceInvalid("A reimbursed expense needs the date it was paid back")
    if txn.reimbursement_status != ReimbursementStatus.REIMBURSED:
        txn.reimbursed_on = None

    if txn.membership_id is not None and txn.kind != TransactionKind.INCOME:
        raise FinanceInvalid("Only income can be linked to a membership")
    if txn.subscription_id is not None and txn.kind != TransactionKind.EXPENSE:
        raise FinanceInvalid("Only an expense can be linked to a subscription")
    if (txn.original_amount is None) != (txn.original_currency is None):
        raise FinanceInvalid("An original amount and its currency go together")

    has_trip_fields = (
        txn.trip_id is not None or txn.trip_cost_category is not None or txn.trip_traveller_id is not None
    )
    if has_trip_fields and txn.kind != TransactionKind.EXPENSE:
        raise FinanceInvalid("Only an expense can be linked to a trip")
    if txn.trip_id is None:
        if txn.trip_cost_category is not None or txn.trip_traveller_id is not None:
            raise FinanceInvalid("A trip cost category or traveller needs a trip")
    else:
        if txn.trip_cost_category is None:
            raise FinanceInvalid("A trip expense needs a trip cost category")
        if txn.trip_traveller_id is not None:
            traveller = session.get(TripTraveller, txn.trip_traveller_id)
            if traveller is not None and traveller.trip_id != txn.trip_id:
                raise FinanceInvalid("That traveller is not on this trip")


def stamp_transaction(txn: Transaction, user: User) -> None:
    now = datetime.now(timezone.utc)
    if txn.id is None:
        txn.created_by_id = user.id
        txn.created_at = now
    txn.updated_at = now


# --- Balances -----------------------------------------------------------------------------


class AccountBalance(SQLModel):
    account_id: int
    name: str
    kind: AccountKind
    active: bool
    opening_balance_lkr: Decimal
    opening_date: date
    balance_lkr: Decimal


def _moved_on():
    # When the money actually left or reached the account: a reimbursed expense moved when
    # the person was paid back, not when they spent it.
    return func.coalesce(Transaction.reimbursed_on, Transaction.occurred_on)


def account_balances(session: Session, as_of: date | None = None) -> list[AccountBalance]:
    """Opening balance + income − expenses − transfers out + transfers in, counting only
    money that moved on or after the account's `opening_date` (and on or before `as_of`).
    Pending reimbursements are excluded until they're paid back."""
    moved_on = _moved_on()
    deltas: dict[int, Decimal] = defaultdict(lambda: ZERO)

    def add(account_column, sign_by_kind: dict[TransactionKind, int]) -> None:
        statement = (
            select(account_column, Transaction.kind, func.sum(Transaction.amount_lkr))
            .join(Account, Account.id == account_column)
            .where(
                col(Transaction.kind).in_(list(sign_by_kind)),
                Transaction.reimbursement_status != ReimbursementStatus.PENDING,
                moved_on >= Account.opening_date,
            )
            .group_by(account_column, Transaction.kind)
        )
        if as_of is not None:
            statement = statement.where(moved_on <= as_of)
        for account_id, kind, total in session.exec(statement).all():
            deltas[account_id] += sign_by_kind[kind] * Decimal(total)

    add(
        Transaction.account_id,
        {TransactionKind.INCOME: 1, TransactionKind.EXPENSE: -1, TransactionKind.TRANSFER: -1},
    )
    add(Transaction.to_account_id, {TransactionKind.TRANSFER: 1})

    accounts = session.exec(select(Account).order_by(col(Account.active).desc(), Account.name)).all()
    return [
        AccountBalance(
            account_id=a.id,
            name=a.name,
            kind=a.kind,
            active=a.active,
            opening_balance_lkr=a.opening_balance_lkr,
            opening_date=a.opening_date,
            balance_lkr=Decimal(a.opening_balance_lkr) + deltas[a.id],
        )
        for a in accounts
    ]


# --- Summary ------------------------------------------------------------------------------


class MonthTotals(SQLModel):
    month: str  # YYYY-MM
    income_lkr: Decimal = ZERO
    expense_lkr: Decimal = ZERO


class CategoryTotal(SQLModel):
    category_id: int | None
    name: str
    kind: TransactionKind
    total_lkr: Decimal


class PersonOwed(SQLModel):
    person_id: int
    name: str
    total_lkr: Decimal
    items: int


class RenewalDue(SQLModel):
    subscription_id: int
    name: str
    next_renewal_on: date
    amount_lkr: Decimal
    overdue: bool


class FinanceSummary(SQLModel):
    start: date
    end: date
    income_lkr: Decimal
    expense_lkr: Decimal
    net_lkr: Decimal
    by_month: list[MonthTotals]
    by_category: list[CategoryTotal]
    pending_reimbursements: list[PersonOwed]
    pending_reimbursements_lkr: Decimal
    monthly_recurring_lkr: Decimal
    renewals: list[RenewalDue]
    balances: list[AccountBalance]


def monthly_cost(sub: Subscription) -> Decimal:
    return (Decimal(sub.amount_lkr) / CYCLE_MONTHS[sub.billing_cycle]).quantize(Decimal("0.01"))


def _months(start: date, end: date) -> list[str]:
    months = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def pending_reimbursements(session: Session) -> list[PersonOwed]:
    rows = session.exec(
        select(Person.id, Person.name, func.sum(Transaction.amount_lkr), func.count())
        .join(Person, Person.id == Transaction.paid_by_person_id)
        .where(Transaction.reimbursement_status == ReimbursementStatus.PENDING)
        .group_by(Person.id, Person.name)
    ).all()
    owed = [PersonOwed(person_id=pid, name=name, total_lkr=Decimal(total), items=n) for pid, name, total, n in rows]
    return sorted(owed, key=lambda p: (-p.total_lkr, p.name))


def finance_summary(
    session: Session, start: date | None = None, end: date | None = None, today: date | None = None
) -> FinanceSummary:
    """Income and spending between `start` and `end` (inclusive; default: this calendar
    year), plus the current state: what's owed to people, recurring costs, renewals coming
    up and account balances."""
    today = today or date.today()
    start = start or date(today.year, 1, 1)
    end = end or date(today.year, 12, 31)
    if end < start:
        raise FinanceInvalid("end must be on or after start")

    categories = {c.id: c for c in session.exec(select(FinanceCategory)).all()}
    by_month = {m: MonthTotals(month=m) for m in _months(start, end)}
    by_category: dict[tuple[int | None, TransactionKind], Decimal] = defaultdict(lambda: ZERO)
    income = expense = ZERO
    rows = session.exec(
        select(Transaction.occurred_on, Transaction.kind, Transaction.category_id, Transaction.amount_lkr).where(
            Transaction.kind != TransactionKind.TRANSFER,
            Transaction.occurred_on >= start,
            Transaction.occurred_on <= end,
        )
    ).all()
    for occurred_on, kind, category_id, amount in rows:
        amount = Decimal(amount)
        month = by_month[occurred_on.strftime("%Y-%m")]
        if kind == TransactionKind.INCOME:
            income += amount
            month.income_lkr += amount
        else:
            expense += amount
            month.expense_lkr += amount
        by_category[(category_id, kind)] += amount

    category_totals = [
        CategoryTotal(
            category_id=cid,
            name=categories[cid].name if cid in categories else "Uncategorised",
            kind=kind,
            total_lkr=total,
        )
        for (cid, kind), total in by_category.items()
    ]
    category_totals.sort(key=lambda c: (c.kind.value, -c.total_lkr, c.name))

    subscriptions = session.exec(select(Subscription).where(Subscription.active == True)).all()  # noqa: E712
    horizon = today + timedelta(days=RENEWAL_WINDOW_DAYS)
    renewals = sorted(
        (
            RenewalDue(
                subscription_id=s.id,
                name=s.name,
                next_renewal_on=s.next_renewal_on,
                amount_lkr=s.amount_lkr,
                overdue=s.next_renewal_on < today,
            )
            for s in subscriptions
            if s.next_renewal_on is not None and s.next_renewal_on <= horizon
        ),
        key=lambda r: (r.next_renewal_on, r.name),
    )

    owed = pending_reimbursements(session)
    return FinanceSummary(
        start=start,
        end=end,
        income_lkr=income,
        expense_lkr=expense,
        net_lkr=income - expense,
        by_month=list(by_month.values()),
        by_category=category_totals,
        pending_reimbursements=owed,
        pending_reimbursements_lkr=sum((p.total_lkr for p in owed), ZERO),
        monthly_recurring_lkr=sum((monthly_cost(s) for s in subscriptions), ZERO),
        renewals=renewals,
        balances=account_balances(session),
    )


# --- Actions that create transactions -----------------------------------------------------


def add_months(day: date, months: int) -> date:
    """`day` moved by whole months, clamped to the month's end (31 Jan + 1 → 28/29 Feb)."""
    index = day.year * 12 + day.month - 1 + months
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


class SubscriptionPayment(SQLModel):
    """Overrides for the expense a subscription payment creates. Leaving `account_id` or
    `paid_by_person_id` out uses the subscription's; sending null clears it."""

    occurred_on: date | None = None
    amount_lkr: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    original_amount: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    account_id: int | None = None
    paid_by_person_id: int | None = None
    payment_method: PaymentMethod | None = None
    reference: str | None = None
    receipt_url: str | None = None
    notes: str | None = None
    advance_renewal: bool = True

    _check_receipt = field_validator("receipt_url")(http_url)


def _override(body: SQLModel, field: str, default):
    return getattr(body, field) if field in body.model_fields_set else default


def record_subscription_payment(
    session: Session, sub: Subscription, body: SubscriptionPayment, user: User
) -> Transaction:
    """Adds the expense for one billing cycle of `sub` and moves its next renewal on by one
    cycle (from the current renewal date, or from the payment date if none was set). Paid
    by a person, it's a pending reimbursement to them."""
    occurred_on = body.occurred_on or date.today()
    paid_by = _override(body, "paid_by_person_id", sub.paid_by_person_id)
    foreign = sub.currency != "LKR"
    txn = Transaction(
        kind=TransactionKind.EXPENSE,
        occurred_on=occurred_on,
        description=f"{sub.name} ({sub.billing_cycle.value})",
        amount_lkr=body.amount_lkr or sub.amount_lkr,
        original_amount=(body.original_amount or sub.amount) if foreign else None,
        original_currency=sub.currency if foreign else None,
        counterparty=sub.vendor,
        payment_method=body.payment_method or sub.payment_method,
        reference=body.reference,
        receipt_url=body.receipt_url,
        notes=body.notes,
        account_id=_override(body, "account_id", sub.account_id),
        category_id=sub.category_id,
        subscription_id=sub.id,
        paid_by_person_id=paid_by,
        reimbursement_status=ReimbursementStatus.PENDING if paid_by else ReimbursementStatus.NONE,
    )
    validate_transaction(session, txn)
    stamp_transaction(txn, user)
    if body.advance_renewal:
        sub.next_renewal_on = add_months(sub.next_renewal_on or occurred_on, CYCLE_MONTHS[sub.billing_cycle])
        session.add(sub)
    session.add(txn)
    session.commit()
    session.refresh(txn)
    return txn


class MembershipPayment(SQLModel):
    occurred_on: date | None = None
    # Defaults to the membership's fee; required when no fee was recorded.
    amount_lkr: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    account_id: int
    payment_method: PaymentMethod | None = None
    reference: str | None = None
    receipt_url: str | None = None
    notes: str | None = None

    _check_receipt = field_validator("receipt_url")(http_url)


def membership_category(session: Session) -> FinanceCategory:
    category = session.exec(
        select(FinanceCategory).where(
            FinanceCategory.name == MEMBERSHIP_CATEGORY, FinanceCategory.kind == TransactionKind.INCOME
        )
    ).first()
    if category is None:
        category = FinanceCategory(name=MEMBERSHIP_CATEGORY, kind=TransactionKind.INCOME)
        session.add(category)
        session.flush()
    return category


def record_membership_payment(
    session: Session, membership: Membership, body: MembershipPayment, user: User
) -> Transaction:
    """Adds the membership fee as income (linked to the membership and its school) and fills
    the membership's `paid_on` / `fee_lkr` when they're blank. One payment per membership."""
    already = session.exec(
        select(Transaction.id).where(
            Transaction.membership_id == membership.id, Transaction.kind == TransactionKind.INCOME
        )
    ).first()
    if already is not None:
        raise FinanceConflict(f"This membership's payment is already recorded (transaction {already})")
    amount = body.amount_lkr or membership.fee_lkr
    if not amount:
        raise FinanceInvalid("No fee is recorded for this membership, so an amount is required")

    institution = session.get(Institution, membership.institution_id)
    occurred_on = body.occurred_on or date.today()
    txn = Transaction(
        kind=TransactionKind.INCOME,
        occurred_on=occurred_on,
        description=f"{membership.year} membership fee",
        amount_lkr=amount,
        counterparty=institution.name,
        payment_method=body.payment_method,
        reference=body.reference,
        receipt_url=body.receipt_url,
        notes=body.notes,
        account_id=body.account_id,
        category_id=membership_category(session).id,
        membership_id=membership.id,
        institution_id=institution.id,
    )
    validate_transaction(session, txn)
    stamp_transaction(txn, user)
    if membership.paid_on is None:
        membership.paid_on = occurred_on
    if membership.fee_lkr is None:
        membership.fee_lkr = amount
    session.add_all([membership, txn])
    session.commit()
    session.refresh(txn)
    return txn


class ReimbursementPayment(SQLModel):
    reimbursed_on: date | None = None
    # The account the person was paid back from; defaults to the expense's own account.
    account_id: int | None = None
    payment_method: PaymentMethod | None = None
    reference: str | None = None


def mark_reimbursed(session: Session, txn: Transaction, body: ReimbursementPayment, user: User) -> Transaction:
    if txn.reimbursement_status != ReimbursementStatus.PENDING:
        raise FinanceConflict("Only a pending reimbursement can be marked reimbursed")
    account_id = body.account_id or txn.account_id
    if account_id is None:
        raise FinanceInvalid("Pick the account the person was paid back from")
    txn.account_id = account_id
    txn.reimbursement_status = ReimbursementStatus.REIMBURSED
    txn.reimbursed_on = body.reimbursed_on or date.today()
    if body.payment_method is not None:
        txn.payment_method = body.payment_method
    if body.reference:
        txn.reference = body.reference
    validate_transaction(session, txn)
    stamp_transaction(txn, user)
    session.add(txn)
    session.commit()
    session.refresh(txn)
    return txn


# --- Queries and export -------------------------------------------------------------------


def text_search(statement, q: str):
    pattern = f"%{q.strip()}%"
    return statement.where(
        or_(col(Transaction.description).ilike(pattern), col(Transaction.counterparty).ilike(pattern))
    )


def either_account(statement, account_id: int):
    return statement.where(or_(Transaction.account_id == account_id, Transaction.to_account_id == account_id))


CSV_COLUMNS = (
    "id",
    "date",
    "kind",
    "description",
    "amount_lkr",
    "signed_amount_lkr",
    "original_amount",
    "original_currency",
    "category",
    "account",
    "to_account",
    "counterparty",
    "payment_method",
    "reference",
    "paid_by",
    "reimbursement_status",
    "reimbursed_on",
    "school",
    "tournament",
    "trip",
    "trip_category",
    "receipt_url",
    "notes",
)

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_cell(value) -> str:
    """A cell that spreadsheet apps won't run as a formula: text starting with = + - @ (or a
    tab / CR) gets a leading apostrophe. Numbers are written by us, so they're left alone."""
    if value is None:
        return ""
    if isinstance(value, (Decimal, int)):
        return str(value)
    text = value.value if hasattr(value, "value") else str(value)
    return f"'{text}" if text.startswith(_FORMULA_PREFIXES) else text


def _names(session: Session, model, ids: Iterable[int | None], attr: str = "name") -> dict[int, str]:
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    rows = session.exec(select(model).where(col(model.id).in_(wanted))).all()
    return {r.id: getattr(r, attr) for r in rows}


def transactions_csv(session: Session, rows: list[Transaction]) -> str:
    accounts = _names(session, Account, [r.account_id for r in rows] + [r.to_account_id for r in rows])
    categories = _names(session, FinanceCategory, [r.category_id for r in rows])
    people = _names(session, Person, [r.paid_by_person_id for r in rows])
    schools = _names(session, Institution, [r.institution_id for r in rows])
    tournaments = _names(session, Tournament, [r.tournament_id for r in rows])
    trips = _names(session, Trip, [r.trip_id for r in rows])

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(CSV_COLUMNS)
    for r in rows:
        sign = {TransactionKind.INCOME: 1, TransactionKind.EXPENSE: -1}.get(r.kind)
        writer.writerow(
            csv_cell(v)
            for v in (
                r.id,
                r.occurred_on.isoformat(),
                r.kind,
                r.description,
                Decimal(r.amount_lkr),
                sign * Decimal(r.amount_lkr) if sign else None,
                Decimal(r.original_amount) if r.original_amount is not None else None,
                r.original_currency,
                categories.get(r.category_id),
                accounts.get(r.account_id),
                accounts.get(r.to_account_id),
                r.counterparty,
                r.payment_method,
                r.reference,
                people.get(r.paid_by_person_id),
                r.reimbursement_status,
                r.reimbursed_on.isoformat() if r.reimbursed_on else None,
                schools.get(r.institution_id),
                tournaments.get(r.tournament_id),
                trips.get(r.trip_id),
                r.trip_cost_category,
                r.receipt_url,
                r.notes,
            )
        )
    return out.getvalue()
