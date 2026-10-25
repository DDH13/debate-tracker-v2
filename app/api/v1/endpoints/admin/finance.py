from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response, status
from sqlmodel import Session, col, select
from sqlmodel.sql.expression import SelectOfScalar

from app.api.crud import ListParams, add_crud_routes
from app.api.deps import BoardUserDep, SessionDep, get_or_404
from app.models import (
    Account,
    AccountCreate,
    AccountKind,
    AccountPublic,
    AccountUpdate,
    FinanceCategory,
    FinanceCategoryCreate,
    FinanceCategoryPublic,
    FinanceCategoryUpdate,
    Institution,
    Membership,
    Person,
    ReimbursementStatus,
    Subscription,
    SubscriptionCreate,
    SubscriptionPublic,
    SubscriptionUpdate,
    Tournament,
    Transaction,
    TransactionCreate,
    TransactionKind,
    TransactionPublic,
    TransactionUpdate,
    Trip,
    TripTraveller,
    User,
    Workshop,
)
from app.services import finance as finance_service
from app.services.finance import (
    FinanceConflict,
    FinanceInvalid,
    MembershipPayment,
    ReimbursementPayment,
    SubscriptionPayment,
)

router = APIRouter()


@contextmanager
def finance_errors() -> Iterator[None]:
    try:
        yield
    except FinanceInvalid as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except FinanceConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


IN_USE = "still referenced by transactions or subscriptions; deactivate it instead"

# --- Accounts and categories ----------------------------------------------------------


class AccountFilters(ListParams):
    kind: AccountKind | None = None
    active: bool | None = None


accounts = APIRouter(prefix="/accounts", tags=["admin: finance"])
add_crud_routes(
    accounts,
    noun="account",
    model=Account,
    create_schema=AccountCreate,
    public_schema=AccountPublic,
    update_schema=AccountUpdate,
    filters=AccountFilters,
    order_by=(col(Account.active).desc(), Account.name),
    conflict_detail=f"An account with that name exists, or this account is {IN_USE}",
)
router.include_router(accounts)


class CategoryFilters(ListParams):
    kind: TransactionKind | None = None
    active: bool | None = None


categories = APIRouter(prefix="/finance-categories", tags=["admin: finance"])
add_crud_routes(
    categories,
    noun="finance_category",
    model=FinanceCategory,
    create_schema=FinanceCategoryCreate,
    public_schema=FinanceCategoryPublic,
    update_schema=FinanceCategoryUpdate,
    filters=CategoryFilters,
    order_by=(FinanceCategory.kind, FinanceCategory.name),
    conflict_detail=f"A category with that name and kind exists, or this category is {IN_USE}",
)
router.include_router(categories)

# --- Subscriptions --------------------------------------------------------------------


class SubscriptionFilters(ListParams):
    active: bool | None = None
    category_id: int | None = None
    paid_by_person_id: int | None = None


def _check_subscription_category(session: Session, sub: Subscription, _user: User) -> None:
    category = session.get(FinanceCategory, sub.category_id)
    if category is not None and category.kind != TransactionKind.EXPENSE:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="A subscription needs an expense category",
        )


subscriptions = APIRouter(prefix="/subscriptions", tags=["admin: finance"])


@subscriptions.post("/{subscription_id}/record-payment", response_model=TransactionPublic, status_code=status.HTTP_201_CREATED)
def record_subscription_payment(
    subscription_id: int, session: SessionDep, user: BoardUserDep, body: SubscriptionPayment | None = None
) -> Transaction:
    """Adds this cycle's expense (prefilled from the subscription) and advances the next
    renewal date by one cycle."""
    sub = get_or_404(session, Subscription, subscription_id)
    body = body or SubscriptionPayment()
    for field, model in (("account_id", Account), ("paid_by_person_id", Person)):
        if getattr(body, field) is not None:
            get_or_404(session, model, getattr(body, field))
    with finance_errors():
        return finance_service.record_subscription_payment(session, sub, body, user)


add_crud_routes(
    subscriptions,
    noun="subscription",
    model=Subscription,
    create_schema=SubscriptionCreate,
    public_schema=SubscriptionPublic,
    update_schema=SubscriptionUpdate,
    filters=SubscriptionFilters,
    order_by=(col(Subscription.active).desc(), Subscription.name),
    parents={"category_id": FinanceCategory, "account_id": Account, "paid_by_person_id": Person},
    conflict_detail="This subscription is still referenced by other records",
    on_save=_check_subscription_category,
)
router.include_router(subscriptions)

# --- Transactions ---------------------------------------------------------------------


class TransactionFilters(ListParams):
    custom = frozenset({"account_id", "date_from", "date_to", "q"})

    kind: TransactionKind | None = None
    category_id: int | None = None
    reimbursement_status: ReimbursementStatus | None = None
    paid_by_person_id: int | None = None
    membership_id: int | None = None
    subscription_id: int | None = None
    institution_id: int | None = None
    tournament_id: int | None = None
    trip_id: int | None = None
    workshop_id: int | None = None
    # Either side of a transfer.
    account_id: int | None = None
    date_from: date | None = None
    date_to: date | None = None
    # Matches description or counterparty.
    q: str | None = None


def _transaction_filters(statement: SelectOfScalar, params: TransactionFilters) -> SelectOfScalar:
    if params.account_id is not None:
        statement = finance_service.either_account(statement, params.account_id)
    if params.date_from is not None:
        statement = statement.where(Transaction.occurred_on >= params.date_from)
    if params.date_to is not None:
        statement = statement.where(Transaction.occurred_on <= params.date_to)
    if params.q and params.q.strip():
        statement = finance_service.text_search(statement, params.q)
    return statement


def _save_transaction(session: Session, txn: Transaction, user: User) -> None:
    with finance_errors():
        finance_service.validate_transaction(session, txn)
    finance_service.stamp_transaction(txn, user)


transactions = APIRouter(prefix="/transactions", tags=["admin: finance"])

TRANSACTION_ORDER = (col(Transaction.occurred_on).desc(), col(Transaction.id).desc())


@transactions.get("/export.csv", response_class=Response)
def export_transactions(session: SessionDep, params: Annotated[TransactionFilters, Query()]) -> Response:
    """Every transaction matching the list filters (paging ignored) as a CSV download."""
    statement = select(Transaction)
    equality = set(TransactionFilters.model_fields) - {"offset", "limit"} - TransactionFilters.custom
    for field, value in params.model_dump(include=equality, exclude_none=True).items():
        statement = statement.where(getattr(Transaction, field) == value)
    rows = list(session.exec(_transaction_filters(statement, params).order_by(*TRANSACTION_ORDER)).all())
    filename = f"dc-transactions-{date.today().isoformat()}.csv"
    return Response(
        content=finance_service.transactions_csv(session, rows),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@transactions.post("/{transaction_id}/mark-reimbursed", response_model=TransactionPublic)
def mark_reimbursed(
    transaction_id: int, session: SessionDep, user: BoardUserDep, body: ReimbursementPayment | None = None
) -> Transaction:
    txn = get_or_404(session, Transaction, transaction_id)
    body = body or ReimbursementPayment()
    if body.account_id is not None:
        get_or_404(session, Account, body.account_id)
    with finance_errors():
        return finance_service.mark_reimbursed(session, txn, body, user)


add_crud_routes(
    transactions,
    noun="transaction",
    model=Transaction,
    create_schema=TransactionCreate,
    public_schema=TransactionPublic,
    update_schema=TransactionUpdate,
    filters=TransactionFilters,
    order_by=TRANSACTION_ORDER,
    parents={
        "account_id": Account,
        "to_account_id": Account,
        "category_id": FinanceCategory,
        "membership_id": Membership,
        "subscription_id": Subscription,
        "tournament_id": Tournament,
        "institution_id": Institution,
        "paid_by_person_id": Person,
        "trip_id": Trip,
        "workshop_id": Workshop,
        "trip_traveller_id": TripTraveller,
    },
    on_save=_save_transaction,
    filter_hook=_transaction_filters,
)
router.include_router(transactions)

# --- Membership payments --------------------------------------------------------------

membership_payments = APIRouter(prefix="/memberships", tags=["admin: finance"])


@membership_payments.post(
    "/{membership_id}/record-payment", response_model=TransactionPublic, status_code=status.HTTP_201_CREATED
)
def record_membership_payment(
    membership_id: int, body: MembershipPayment, session: SessionDep, user: BoardUserDep
) -> Transaction:
    """Records the membership fee as income and fills the membership's paid date and fee
    if they're blank."""
    membership = get_or_404(session, Membership, membership_id)
    get_or_404(session, Account, body.account_id)
    with finance_errors():
        return finance_service.record_membership_payment(session, membership, body, user)


router.include_router(membership_payments)

# --- Reports --------------------------------------------------------------------------

reports = APIRouter(prefix="/finance", tags=["admin: finance"])


@reports.get("/summary", response_model=finance_service.FinanceSummary)
def finance_summary(
    session: SessionDep, start: date | None = None, end: date | None = None
) -> finance_service.FinanceSummary:
    """Totals for `start`–`end` (default: this calendar year) plus current balances, what's
    owed to people, recurring costs and upcoming renewals."""
    with finance_errors():
        return finance_service.finance_summary(session, start, end)


@reports.get("/balances", response_model=list[finance_service.AccountBalance])
def finance_balances(session: SessionDep, as_of: date | None = None) -> list[finance_service.AccountBalance]:
    return finance_service.account_balances(session, as_of)


router.include_router(reports)
