import csv
import io
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.models import FinanceCategory, Subscription, Transaction, TransactionKind
from app.services.finance import (
    DEFAULT_CATEGORIES,
    add_months,
    csv_cell,
    ensure_default_categories,
    finance_summary,
)

API = "/api/v1"
ADMIN = f"{API}/admin"
TODAY = date.today()


@pytest.fixture
def categories(session: Session) -> dict[str, int]:
    ensure_default_categories(session)
    return {c.name: c.id for c in session.exec(select(FinanceCategory)).all()}


def _post(client: TestClient, path: str, body: dict, expected: int = 201) -> dict:
    resp = client.post(f"{ADMIN}/{path}", json=body)
    assert resp.status_code == expected, resp.text
    return resp.json()


def _account(client: TestClient, name: str, opening: str = "0", opened: str = "2020-01-01", **fields) -> dict:
    return _post(
        client, "accounts", {"name": name, "opening_balance_lkr": opening, "opening_date": opened, **fields}
    )


def _person(client: TestClient, name: str = "Ann Treasurer") -> dict:
    return _post(client, "people", {"name": name, "kind": "board"})


def _txn(client: TestClient, expected: int = 201, **body) -> dict:
    body.setdefault("occurred_on", TODAY.isoformat())
    body.setdefault("description", "Something")
    return _post(client, "transactions", body, expected)


def _balances(client: TestClient, **params) -> dict[str, Decimal]:
    resp = client.get(f"{ADMIN}/finance/balances", params=params)
    assert resp.status_code == 200, resp.text
    return {b["name"]: Decimal(b["balance_lkr"]) for b in resp.json()}


# --- categories --------------------------------------------------------------------------


def test_default_categories_are_seeded_once(session: Session) -> None:
    total = sum(len(names) for names in DEFAULT_CATEGORIES.values())
    assert ensure_default_categories(session) == total
    assert ensure_default_categories(session) == 0
    assert len(session.exec(select(FinanceCategory)).all()) == total


def test_category_kind_cannot_be_transfer(client: TestClient) -> None:
    _post(client, "finance-categories", {"name": "Moves", "kind": "transfer"}, 422)
    cat = _post(client, "finance-categories", {"name": "Grants", "kind": "income"})
    _post(client, "finance-categories", {"name": "Grants", "kind": "income"}, 409)
    assert client.patch(f"{ADMIN}/finance-categories/{cat['id']}", json={"kind": "transfer"}).status_code == 422


# --- transaction rules -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"kind": "expense"}, "needs a category"),
        ({"kind": "expense", "category": "Sponsorship"}, "is for income"),
        ({"kind": "income", "category": "Sponsorship", "to_account": True}, "Only a transfer"),
        ({"kind": "transfer"}, "both a from and a to account"),
        ({"kind": "transfer", "to_account": "same"}, "must differ"),
        ({"kind": "transfer", "to_account": True, "category": "Venue"}, "has no category"),
        ({"kind": "transfer", "to_account": True, "person": True}, "can't be paid by a person"),
        ({"kind": "expense", "category": "Venue", "no_account": True}, "account is required"),
        ({"kind": "income", "category": "Sponsorship", "person": True, "reimbursement_status": "pending"}, "Only an expense"),
        ({"kind": "expense", "category": "Venue", "reimbursement_status": "pending", "no_account": True}, "the person who paid"),
        ({"kind": "expense", "category": "Venue", "person": True, "reimbursement_status": "reimbursed"}, "date it was paid back"),
        ({"kind": "expense", "category": "Venue", "original_amount": "10"}, "go together"),
    ],
)
def test_transaction_rules(client: TestClient, categories, body: dict, message: str) -> None:
    main = _account(client, "Main")
    other = _account(client, "Cash", kind="cash")
    payload = {"kind": body["kind"], "amount_lkr": "100", "account_id": main["id"]}
    if body.get("no_account"):
        payload["account_id"] = None
    if "category" in body:
        payload["category_id"] = categories[body["category"]]
    if body.get("to_account"):
        payload["to_account_id"] = main["id"] if body["to_account"] == "same" else other["id"]
    if body.get("person"):
        payload["paid_by_person_id"] = _person(client)["id"]
    for key in ("reimbursement_status", "original_amount"):
        if key in body:
            payload[key] = body[key]
    resp = _txn(client, 422, **payload)
    assert message in resp["detail"]


def test_transaction_field_validation(client: TestClient, categories) -> None:
    main = _account(client, "Main")
    base = {"kind": "expense", "category_id": categories["Venue"], "account_id": main["id"]}
    _txn(client, 422, amount_lkr="0", **base)
    _txn(client, 422, amount_lkr="-5", **base)
    _txn(client, 422, amount_lkr="5", receipt_url="javascript:alert(1)", **base)
    _txn(client, 422, amount_lkr="5", original_amount="2", original_currency="dollars", **base)
    _txn(client, 404, amount_lkr="5", **{**base, "category_id": 999})
    ok = _txn(client, amount_lkr="5", original_amount="2", original_currency=" usd ", **base)
    assert ok["original_currency"] == "USD"
    assert Decimal(ok["amount_lkr"]) == 5


def test_transaction_records_who_created_it(client: TestClient, categories) -> None:
    main = _account(client, "Main")
    me = client.get(f"{API}/auth/me").json()
    txn = _txn(client, kind="income", category_id=categories["Donations"], account_id=main["id"], amount_lkr="50")
    assert txn["created_by_id"] == me["id"]
    resp = client.patch(f"{ADMIN}/transactions/{txn['id']}", json={"description": "Gift"})
    assert resp.status_code == 200
    assert resp.json()["updated_at"] >= txn["updated_at"]
    # A PATCH that breaks a rule is rejected as a whole.
    resp = client.patch(f"{ADMIN}/transactions/{txn['id']}", json={"kind": "expense"})
    assert resp.status_code == 422


def test_reimbursed_on_is_cleared_unless_reimbursed(client: TestClient, categories) -> None:
    main = _account(client, "Main")
    person = _person(client)
    txn = _txn(
        client, kind="expense", category_id=categories["Venue"], amount_lkr="10", account_id=main["id"],
        paid_by_person_id=person["id"], reimbursement_status="reimbursed", reimbursed_on="2026-01-02",
    )
    resp = client.patch(f"{ADMIN}/transactions/{txn['id']}", json={"reimbursement_status": "pending"})
    assert resp.json()["reimbursed_on"] is None


# --- balances ----------------------------------------------------------------------------


def test_balances_follow_income_expenses_and_transfers(client: TestClient, categories) -> None:
    bank = _account(client, "Bank", opening="1000.50", opened="2026-01-01")
    cash = _account(client, "Cash", opening="-20", opened="2026-01-01", kind="cash")
    _txn(client, kind="income", category_id=categories["Sponsorship"], account_id=bank["id"], amount_lkr="500", occurred_on="2026-02-01")
    _txn(client, kind="expense", category_id=categories["Venue"], account_id=bank["id"], amount_lkr="200", occurred_on="2026-02-02")
    _txn(client, kind="transfer", account_id=bank["id"], to_account_id=cash["id"], amount_lkr="100", occurred_on="2026-02-03")
    # Before the account's opening date: already part of the opening balance.
    _txn(client, kind="income", category_id=categories["Donations"], account_id=bank["id"], amount_lkr="999", occurred_on="2025-12-31")

    assert _balances(client) == {"Bank": Decimal("1200.50"), "Cash": Decimal("80")}
    assert _balances(client, as_of="2026-02-02") == {"Bank": Decimal("1300.50"), "Cash": Decimal("-20")}


def test_pending_reimbursement_hits_the_balance_when_paid_back(client: TestClient, categories) -> None:
    bank = _account(client, "Bank", opening="1000", opened="2026-01-01")
    person = _person(client)
    txn = _txn(
        client, kind="expense", category_id=categories["Food & refreshments"], amount_lkr="300",
        paid_by_person_id=person["id"], reimbursement_status="pending", occurred_on="2026-03-01",
    )
    assert txn["account_id"] is None
    assert _balances(client) == {"Bank": Decimal("1000")}

    summary = client.get(f"{ADMIN}/finance/summary", params={"start": "2026-01-01", "end": "2026-12-31"}).json()
    assert Decimal(summary["pending_reimbursements_lkr"]) == 300
    assert summary["pending_reimbursements"][0]["name"] == person["name"]
    assert Decimal(summary["expense_lkr"]) == 300  # counted as spent when it was spent

    # Paying back needs an account, since the expense never had one.
    resp = client.post(f"{ADMIN}/transactions/{txn['id']}/mark-reimbursed", json={})
    assert resp.status_code == 422
    resp = client.post(
        f"{ADMIN}/transactions/{txn['id']}/mark-reimbursed",
        json={"account_id": bank["id"], "reimbursed_on": "2026-04-10"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["reimbursement_status"] == "reimbursed"
    assert _balances(client) == {"Bank": Decimal("700")}
    # The money left the bank when the person was paid back, not when they spent it.
    assert _balances(client, as_of="2026-04-09") == {"Bank": Decimal("1000")}

    again = client.post(f"{ADMIN}/transactions/{txn['id']}/mark-reimbursed", json={"account_id": bank["id"]})
    assert again.status_code == 409


# --- summary -----------------------------------------------------------------------------


def test_summary_totals_months_categories_and_recurring(session: Session, client: TestClient, categories) -> None:
    bank = _account(client, "Bank")
    cash = _account(client, "Cash", kind="cash")
    _txn(client, kind="income", category_id=categories["Sponsorship"], account_id=bank["id"], amount_lkr="1000", occurred_on="2026-01-15")
    _txn(client, kind="expense", category_id=categories["Venue"], account_id=bank["id"], amount_lkr="300", occurred_on="2026-01-20")
    _txn(client, kind="expense", category_id=categories["Venue"], account_id=bank["id"], amount_lkr="100", occurred_on="2026-03-01")
    _txn(client, kind="expense", category_id=categories["Printing"], account_id=bank["id"], amount_lkr="50", occurred_on="2026-03-02")
    _txn(client, kind="transfer", account_id=bank["id"], to_account_id=cash["id"], amount_lkr="400", occurred_on="2026-03-03")
    _txn(client, kind="income", category_id=categories["Donations"], account_id=bank["id"], amount_lkr="7", occurred_on="2025-12-31")

    sub = dict(category_id=categories["Subscriptions"], currency="LKR")
    _post(client, "subscriptions", {"name": "Zoom", "amount": "3000", "amount_lkr": "3000", "billing_cycle": "monthly", **sub})
    _post(client, "subscriptions", {"name": "Domain", "amount": "1200", "amount_lkr": "1200", "billing_cycle": "yearly", **sub})
    _post(client, "subscriptions", {"name": "Drive", "amount": "900", "amount_lkr": "900", "billing_cycle": "quarterly", **sub})
    _post(client, "subscriptions", {"name": "Old", "amount": "5", "amount_lkr": "5", "active": False, **sub})

    s = finance_summary(session, date(2026, 1, 1), date(2026, 12, 31))
    assert (s.income_lkr, s.expense_lkr, s.net_lkr) == (Decimal(1000), Decimal(450), Decimal(550))
    assert len(s.by_month) == 12
    jan, _, mar = s.by_month[:3]
    assert (jan.month, jan.income_lkr, jan.expense_lkr) == ("2026-01", 1000, 300)
    assert mar.expense_lkr == 150
    venue = next(c for c in s.by_category if c.name == "Venue")
    assert venue.total_lkr == 400
    assert s.monthly_recurring_lkr == Decimal("3000") + Decimal("100") + Decimal("300")


def test_summary_renewals_window(session: Session, client: TestClient, categories) -> None:
    def sub(name: str, renews: date | None, active: bool = True) -> None:
        _post(client, "subscriptions", {
            "name": name, "amount": "10", "amount_lkr": "10", "category_id": categories["Subscriptions"],
            "next_renewal_on": renews.isoformat() if renews else None, "active": active,
        })

    sub("Overdue", TODAY - timedelta(days=3))
    sub("Soon", TODAY + timedelta(days=30))
    sub("Later", TODAY + timedelta(days=31))
    sub("Unknown", None)
    sub("Cancelled", TODAY, active=False)
    renewals = finance_summary(session).renewals
    assert [(r.name, r.overdue) for r in renewals] == [("Overdue", True), ("Soon", False)]


def test_summary_rejects_backwards_period(client: TestClient) -> None:
    resp = client.get(f"{ADMIN}/finance/summary", params={"start": "2026-02-01", "end": "2026-01-01"})
    assert resp.status_code == 422


# --- subscriptions -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "months", "expected"),
    [
        (date(2026, 1, 31), 1, date(2026, 2, 28)),
        (date(2028, 1, 31), 1, date(2028, 2, 29)),
        (date(2026, 11, 30), 3, date(2027, 2, 28)),
        (date(2026, 5, 15), 12, date(2027, 5, 15)),
        (date(2026, 12, 1), 1, date(2027, 1, 1)),
    ],
)
def test_add_months(day: date, months: int, expected: date) -> None:
    assert add_months(day, months) == expected


def test_subscription_needs_an_expense_category(client: TestClient, categories) -> None:
    body = {"name": "Zoom", "amount": "10", "amount_lkr": "3000", "category_id": categories["Sponsorship"]}
    _post(client, "subscriptions", body, 422)
    sub = _post(client, "subscriptions", {**body, "category_id": categories["Subscriptions"], "currency": ""})
    assert sub["currency"] == "LKR"


def test_record_subscription_payment(session: Session, client: TestClient, categories) -> None:
    bank = _account(client, "Bank")
    sub = _post(client, "subscriptions", {
        "name": "Zoom", "vendor": "Zoom Inc", "amount": "15.99", "currency": "usd", "amount_lkr": "4800",
        "billing_cycle": "monthly", "next_renewal_on": "2026-01-31", "category_id": categories["Subscriptions"],
        "account_id": bank["id"],
    })
    resp = client.post(f"{ADMIN}/subscriptions/{sub['id']}/record-payment", json={"occurred_on": "2026-01-31"})
    assert resp.status_code == 201, resp.text
    txn = resp.json()
    assert (txn["kind"], txn["subscription_id"], txn["account_id"]) == ("expense", sub["id"], bank["id"])
    assert Decimal(txn["amount_lkr"]) == 4800
    assert (Decimal(txn["original_amount"]), txn["original_currency"]) == (Decimal("15.99"), "USD")
    assert txn["counterparty"] == "Zoom Inc"
    assert txn["reimbursement_status"] == "none"
    assert session.get(Subscription, sub["id"]).next_renewal_on == date(2026, 2, 28)

    # Override the amount, keep the renewal date where it is.
    resp = client.post(
        f"{ADMIN}/subscriptions/{sub['id']}/record-payment",
        json={"amount_lkr": "5000", "original_amount": "16.50", "advance_renewal": False},
    )
    assert Decimal(resp.json()["amount_lkr"]) == 5000
    session.expire_all()
    assert session.get(Subscription, sub["id"]).next_renewal_on == date(2026, 2, 28)


def test_subscription_paid_by_a_person_makes_pending_reimbursements(session: Session, client: TestClient, categories) -> None:
    person = _person(client)
    sub = _post(client, "subscriptions", {
        "name": "CapCut", "amount": "2400", "amount_lkr": "2400", "billing_cycle": "yearly",
        "category_id": categories["Subscriptions"], "paid_by_person_id": person["id"],
    })
    txn = client.post(f"{ADMIN}/subscriptions/{sub['id']}/record-payment", json={"occurred_on": "2026-03-10"}).json()
    assert txn["reimbursement_status"] == "pending"
    assert txn["paid_by_person_id"] == person["id"]
    assert txn["original_amount"] is None  # LKR bill
    # No renewal date set: the next one is a cycle after this payment.
    assert session.get(Subscription, sub["id"]).next_renewal_on == date(2027, 3, 10)

    # Clearing the person explicitly without an account breaks the account rule.
    resp = client.post(f"{ADMIN}/subscriptions/{sub['id']}/record-payment", json={"paid_by_person_id": None})
    assert resp.status_code == 422


# --- membership payments -----------------------------------------------------------------


def test_record_membership_payment(client: TestClient, categories) -> None:
    bank = _account(client, "Bank")
    school = client.post(f"{API}/institutions", json={"name": "Royal College"}).json()
    membership = _post(client, "memberships", {"institution_id": school["id"], "year": 2026})

    resp = client.post(f"{ADMIN}/memberships/{membership['id']}/record-payment", json={"account_id": bank["id"]})
    assert resp.status_code == 422  # no fee on record and no amount given

    resp = client.post(
        f"{ADMIN}/memberships/{membership['id']}/record-payment",
        json={"account_id": bank["id"], "amount_lkr": "5000", "occurred_on": "2026-02-01"},
    )
    assert resp.status_code == 201, resp.text
    txn = resp.json()
    assert (txn["kind"], txn["category_id"]) == ("income", categories["Membership fees"])
    assert (txn["membership_id"], txn["institution_id"]) == (membership["id"], school["id"])
    assert (txn["description"], txn["counterparty"]) == ("2026 membership fee", "Royal College")

    updated = client.get(f"{ADMIN}/memberships/{membership['id']}").json()
    assert updated["paid_on"] == "2026-02-01"
    assert Decimal(updated["fee_lkr"]) == 5000

    again = client.post(f"{ADMIN}/memberships/{membership['id']}/record-payment", json={"account_id": bank["id"]})
    assert again.status_code == 409


def test_membership_payment_keeps_existing_fee_and_paid_on(client: TestClient, categories) -> None:
    bank = _account(client, "Bank")
    school = client.post(f"{API}/institutions", json={"name": "Trinity College"}).json()
    membership = _post(client, "memberships", {
        "institution_id": school["id"], "year": 2026, "fee_lkr": "4000", "paid_on": "2026-01-05",
    })
    txn = _post(client, f"memberships/{membership['id']}/record-payment", {"account_id": bank["id"]})
    assert Decimal(txn["amount_lkr"]) == 4000
    updated = client.get(f"{ADMIN}/memberships/{membership['id']}").json()
    assert updated["paid_on"] == "2026-01-05"

    # Deleting the membership keeps the income, unlinked.
    assert client.delete(f"{ADMIN}/memberships/{membership['id']}").status_code == 204
    assert client.get(f"{ADMIN}/transactions/{txn['id']}").json()["membership_id"] is None


# --- listing and export ------------------------------------------------------------------


def test_transaction_filters(client: TestClient, categories) -> None:
    bank = _account(client, "Bank")
    cash = _account(client, "Cash", kind="cash")
    _txn(client, kind="income", category_id=categories["Sponsorship"], account_id=bank["id"], amount_lkr="1", occurred_on="2026-01-01", description="ACME sponsorship")
    _txn(client, kind="expense", category_id=categories["Venue"], account_id=cash["id"], amount_lkr="2", occurred_on="2026-02-01", counterparty="Acme Hall")
    _txn(client, kind="transfer", account_id=bank["id"], to_account_id=cash["id"], amount_lkr="3", occurred_on="2026-03-01")

    def amounts(**params) -> list[str]:
        resp = client.get(f"{ADMIN}/transactions", params=params)
        assert resp.status_code == 200, resp.text
        return [str(Decimal(t["amount_lkr"]).normalize()) for t in resp.json()]

    assert amounts() == ["3", "2", "1"]  # newest first
    assert amounts(account_id=cash["id"]) == ["3", "2"]  # either side of the transfer
    assert amounts(kind="expense") == ["2"]
    assert amounts(date_from="2026-01-15", date_to="2026-02-28") == ["2"]
    assert amounts(q="acme") == ["2", "1"]
    assert amounts(category_id=categories["Sponsorship"]) == ["1"]


def test_csv_export_filters_and_escapes(client: TestClient, categories) -> None:
    bank = _account(client, "Bank")
    person = _person(client, "=HYPERLINK(\"http://x\")")
    _txn(client, kind="income", category_id=categories["Sponsorship"], account_id=bank["id"], amount_lkr="1000", description="Sponsor", occurred_on="2026-01-01")
    _txn(
        client, kind="expense", category_id=categories["Venue"], amount_lkr="250.5", description="@SUM(A1)",
        paid_by_person_id=person["id"], reimbursement_status="pending", occurred_on="2026-02-01",
    )

    resp = client.get(f"{ADMIN}/transactions/export.csv")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(resp.text)))
    assert [r["description"] for r in rows] == ["'@SUM(A1)", "Sponsor"]
    expense = rows[0]
    assert expense["paid_by"].startswith("'=")
    assert Decimal(expense["signed_amount_lkr"]) == Decimal("-250.5")  # numbers aren't escaped
    assert expense["reimbursement_status"] == "pending"
    assert expense["category"] == "Venue"

    only_income = client.get(f"{ADMIN}/transactions/export.csv", params={"kind": "income"}).text
    assert len(list(csv.DictReader(io.StringIO(only_income)))) == 1


@pytest.mark.parametrize(
    ("value", "expected"),
    [("=1+1", "'=1+1"), ("+94 77", "'+94 77"), ("-x", "'-x"), ("\tx", "'\tx"), ("plain", "plain"), (None, ""), (Decimal("-5"), "-5")],
)
def test_csv_cell(value, expected) -> None:
    assert csv_cell(value) == expected


# --- references and merges ---------------------------------------------------------------


def test_referenced_accounts_categories_and_people_cannot_be_deleted(client: TestClient, categories) -> None:
    bank = _account(client, "Bank")
    person = _person(client)
    unused = _account(client, "Unused")
    _txn(
        client, kind="expense", category_id=categories["Venue"], account_id=bank["id"], amount_lkr="5",
        paid_by_person_id=person["id"], reimbursement_status="pending",
    )
    assert client.delete(f"{ADMIN}/accounts/{bank['id']}").status_code == 409
    assert client.delete(f"{ADMIN}/finance-categories/{categories['Venue']}").status_code == 409
    assert client.delete(f"{ADMIN}/people/{person['id']}").status_code == 409
    assert client.delete(f"{ADMIN}/accounts/{unused['id']}").status_code == 204
    # Deactivating is the way out.
    assert client.patch(f"{ADMIN}/accounts/{bank['id']}", json={"active": False}).json()["active"] is False


def test_institution_merge_and_tournament_delete_keep_transactions(session: Session, client: TestClient, categories) -> None:
    bank = _account(client, "Bank")
    keep = client.post(f"{API}/institutions", json={"name": "Royal College"}).json()
    dupe = client.post(f"{API}/institutions", json={"name": "Royal College Colombo"}).json()
    tournament = client.post(f"{API}/tournaments", json={"name": "Cup", "slug": "cup"}).json()
    txn = _txn(
        client, kind="income", category_id=categories["Tournament fees"], account_id=bank["id"], amount_lkr="10",
        institution_id=dupe["id"], tournament_id=tournament["id"],
    )
    resp = client.post(f"{API}/institutions/merge", json={"target_id": keep["id"], "source_ids": [dupe["id"]]})
    assert resp.status_code == 200, resp.text
    assert client.delete(f"{API}/tournaments/{tournament['id']}").status_code == 204

    session.expire_all()
    stored = session.get(Transaction, txn["id"])
    assert (stored.institution_id, stored.tournament_id) == (keep["id"], None)
    assert stored.kind == TransactionKind.INCOME
