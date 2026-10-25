from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.models import FinanceCategory, WorkshopSchool
from app.services.finance import ensure_default_categories

API = "/api/v1"
ADMIN = f"{API}/admin"


@pytest.fixture
def categories(session: Session) -> dict[str, int]:
    ensure_default_categories(session)
    return {c.name: c.id for c in session.exec(select(FinanceCategory)).all()}


def _post(client: TestClient, path: str, body: dict, expected: int = 201) -> dict:
    resp = client.post(path, json=body)
    assert resp.status_code == expected, resp.text
    return resp.json()


def _workshop(client: TestClient, title: str = "Intro to debating", held_on: str = "2026-03-01", **fields) -> dict:
    return _post(client, f"{ADMIN}/workshops", {"title": title, "held_on": held_on, **fields})


def _school(client: TestClient, name: str) -> dict:
    return _post(client, f"{API}/institutions", {"name": name})


def _account(client: TestClient) -> dict:
    return _post(client, f"{ADMIN}/accounts", {"name": "Bank", "opening_date": "2020-01-01"})


def test_workshop_detail_headcount_money_and_links(client: TestClient, categories) -> None:
    workshop = _workshop(client, other_attendees=4)
    royal, trinity = _school(client, "Royal College"), _school(client, "Trinity College")
    _post(client, f"{ADMIN}/workshop-schools", {"workshop_id": workshop["id"], "institution_id": royal["id"], "students": 20})
    _post(client, f"{ADMIN}/workshop-schools", {"workshop_id": workshop["id"], "institution_id": trinity["id"]})
    _post(client, f"{ADMIN}/workshop-schools", {"workshop_id": workshop["id"], "institution_id": royal["id"]}, 409)
    ann = _post(client, f"{ADMIN}/people", {"name": "Ann Facilitator"})
    _post(client, f"{ADMIN}/workshop-facilitators", {"workshop_id": workshop["id"], "person_id": ann["id"], "role": "lead"})
    _post(client, f"{ADMIN}/workshop-facilitators", {"workshop_id": workshop["id"], "person_id": ann["id"]}, 409)

    bank = _account(client)
    base = {"occurred_on": "2026-03-01", "workshop_id": workshop["id"]}
    _post(client, f"{ADMIN}/transactions", {**base, "kind": "income", "description": "Fees", "amount_lkr": "6000",
                                           "category_id": categories["Workshop fees"], "account_id": bank["id"]})
    _post(client, f"{ADMIN}/transactions", {**base, "kind": "expense", "description": "Hall", "amount_lkr": "2500",
                                           "category_id": categories["Venue"], "account_id": bank["id"]})
    # Pending reimbursement: spent, so it counts.
    _post(client, f"{ADMIN}/transactions", {**base, "kind": "expense", "description": "Snacks", "amount_lkr": "500",
                                           "category_id": categories["Food & refreshments"],
                                           "paid_by_person_id": ann["id"], "reimbursement_status": "pending"})
    _post(client, f"{ADMIN}/feedback", {"content": "Great session", "workshop_id": workshop["id"]})
    _post(client, f"{ADMIN}/tasks", {"title": "Send certificates", "workshop_id": workshop["id"]})

    detail = client.get(f"{ADMIN}/workshops/{workshop['id']}/detail").json()
    assert [(s["name"], s["students"]) for s in detail["schools"]] == [("Royal College", 20), ("Trinity College", None)]
    assert [(f["name"], f["role"]) for f in detail["facilitators"]] == [("Ann Facilitator", "lead")]
    assert detail["headcount"] == 24
    money = [Decimal(detail[k]) for k in ("income_lkr", "expense_lkr", "net_lkr")]
    assert money == [Decimal(6000), Decimal(3000), Decimal(3000)]
    assert len(detail["transactions"]) == 3
    assert [f["content"] for f in detail["feedback"]] == ["Great session"]
    assert [t["title"] for t in detail["tasks"]] == ["Send certificates"]

    feedback = client.get(f"{ADMIN}/feedback", params={"workshop_id": workshop["id"]}).json()
    assert len(feedback) == 1
    ledger = client.get(f"{ADMIN}/transactions", params={"workshop_id": workshop["id"]}).json()
    assert len(ledger) == 3
    assert "workshop" in client.get(f"{ADMIN}/transactions/export.csv").text.splitlines()[0]


def test_transfer_cannot_be_linked_to_a_workshop(client: TestClient, categories) -> None:
    workshop = _workshop(client)
    bank = _account(client)
    cash = _post(client, f"{ADMIN}/accounts", {"name": "Cash", "opening_date": "2020-01-01", "kind": "cash"})
    resp = client.post(f"{ADMIN}/transactions", json={
        "kind": "transfer", "occurred_on": "2026-03-01", "description": "Float", "amount_lkr": "10",
        "account_id": bank["id"], "to_account_id": cash["id"], "workshop_id": workshop["id"],
    })
    assert resp.status_code == 422
    assert "workshop" in resp.json()["detail"]


def test_workshop_rules_and_delete(client: TestClient, categories) -> None:
    _post(client, f"{ADMIN}/workshops", {"title": "Bad", "held_on": "2026-03-02", "end_on": "2026-03-01"}, 422)
    _post(client, f"{ADMIN}/workshops", {"title": "Bad", "held_on": "2026-03-02", "other_attendees": -1}, 422)

    workshop = _workshop(client)
    school = _school(client, "Royal College")
    _post(client, f"{ADMIN}/workshop-schools", {"workshop_id": workshop["id"], "institution_id": school["id"], "students": 5})
    feedback = _post(client, f"{ADMIN}/feedback", {"content": "Ok", "workshop_id": workshop["id"]})
    bank = _account(client)
    txn = _post(client, f"{ADMIN}/transactions", {
        "kind": "income", "occurred_on": "2026-03-01", "description": "Fees", "amount_lkr": "100",
        "category_id": categories["Workshop fees"], "account_id": bank["id"], "workshop_id": workshop["id"],
    })

    resp = client.delete(f"{ADMIN}/workshops/{workshop['id']}")
    assert resp.status_code == 409
    assert "1 ledger entry" in resp.json()["detail"]

    assert client.delete(f"{ADMIN}/transactions/{txn['id']}").status_code == 204
    assert client.delete(f"{ADMIN}/workshops/{workshop['id']}").status_code == 204
    assert client.get(f"{ADMIN}/workshop-schools", params={"workshop_id": workshop["id"]}).json() == []
    assert client.get(f"{ADMIN}/feedback/{feedback['id']}").json()["workshop_id"] is None


def test_summary_by_year(client: TestClient, categories) -> None:
    old = _workshop(client, "Old one", "2025-06-01")
    new = _workshop(client, "New one", "2026-02-01", other_attendees=3)
    school = _school(client, "Royal College")
    _post(client, f"{ADMIN}/workshop-schools", {"workshop_id": new["id"], "institution_id": school["id"], "students": 10})
    person = _post(client, f"{ADMIN}/people", {"name": "Ann"})
    _post(client, f"{ADMIN}/workshop-facilitators", {"workshop_id": new["id"], "person_id": person["id"]})
    bank = _account(client)
    _post(client, f"{ADMIN}/transactions", {
        "kind": "expense", "occurred_on": "2026-02-01", "description": "Hall", "amount_lkr": "750",
        "category_id": categories["Venue"], "account_id": bank["id"], "workshop_id": new["id"],
    })

    rows = client.get(f"{ADMIN}/workshops/summary").json()
    assert [r["title"] for r in rows] == ["New one", "Old one"]
    first = rows[0]
    assert (first["schools"], first["headcount"], first["facilitators"]) == (1, 13, 1)
    assert Decimal(first["net_lkr"]) == Decimal(-750)
    assert rows[1]["headcount"] == 0 and Decimal(rows[1]["net_lkr"]) == 0

    assert [r["id"] for r in client.get(f"{ADMIN}/workshops/summary", params={"year": 2025}).json()] == [old["id"]]
    assert [w["id"] for w in client.get(f"{ADMIN}/workshops", params={"year": 2026}).json()] == [new["id"]]


def test_school_detail_lists_workshops_and_guards_delete(client: TestClient) -> None:
    workshop = _workshop(client)
    school = _school(client, "Royal College")
    _post(client, f"{ADMIN}/workshop-schools", {"workshop_id": workshop["id"], "institution_id": school["id"], "students": 7})

    detail = client.get(f"{ADMIN}/schools/{school['id']}").json()
    assert [(w["title"], w["students"]) for w in detail["workshops"]] == [("Intro to debating", 7)]
    resp = client.delete(f"{API}/institutions/{school['id']}")
    assert resp.status_code == 409
    assert "workshop attendance" in resp.json()["detail"]


def test_institution_merge_combines_workshop_attendance(client: TestClient, session: Session) -> None:
    w1, w2 = _workshop(client, "One"), _workshop(client, "Two")
    keep, dupe = _school(client, "Royal College"), _school(client, "Royal College Colombo")
    _post(client, f"{ADMIN}/workshop-schools", {"workshop_id": w1["id"], "institution_id": keep["id"], "students": 10, "notes": "Seniors"})
    _post(client, f"{ADMIN}/workshop-schools", {"workshop_id": w1["id"], "institution_id": dupe["id"], "students": 5, "notes": "Juniors"})
    _post(client, f"{ADMIN}/workshop-schools", {"workshop_id": w2["id"], "institution_id": dupe["id"]})

    resp = client.post(f"{API}/institutions/merge", json={"target_id": keep["id"], "source_ids": [dupe["id"]]})
    assert resp.status_code == 200, resp.text
    session.expire_all()
    rows = {r.workshop_id: r for r in session.exec(select(WorkshopSchool)).all()}
    assert len(rows) == 2
    assert {r.institution_id for r in rows.values()} == {keep["id"]}
    assert (rows[w1["id"]].students, rows[w1["id"]].notes) == (15, "Seniors\nJuniors")
    assert rows[w2["id"]].students is None


def test_person_detail_aggregates(client: TestClient, categories) -> None:
    person = _post(client, f"{ADMIN}/people", {"name": "Ann"})
    user = _post(client, f"{ADMIN}/users", {"email": "ann@example.com", "name": "Ann", "role": "volunteer",
                                            "password": "long-enough", "person_id": person["id"]})
    _post(client, f"{ADMIN}/board-roles", {"person_id": person["id"], "title": "Secretary", "term_start": "2025-01-01"})
    workshop = _workshop(client)
    _post(client, f"{ADMIN}/workshop-facilitators", {"workshop_id": workshop["id"], "person_id": person["id"], "role": "lead"})
    _post(client, f"{ADMIN}/tasks", {"title": "Done", "assignee_person_id": person["id"], "status": "done"})
    _post(client, f"{ADMIN}/tasks", {"title": "Open", "assignee_person_id": person["id"]})
    _post(client, f"{ADMIN}/transactions", {
        "kind": "expense", "occurred_on": "2026-03-01", "description": "Printing", "amount_lkr": "1200",
        "category_id": categories["Printing"], "paid_by_person_id": person["id"], "reimbursement_status": "pending",
    })

    detail = client.get(f"{ADMIN}/people/{person['id']}/detail").json()
    assert detail["user"]["id"] == user["id"]
    assert [r["title"] for r in detail["board_roles"]] == ["Secretary"]
    assert [t["title"] for t in detail["tasks"]] == ["Open", "Done"]
    assert [(w["title"], w["role"]) for w in detail["workshops"]] == [("Intro to debating", "lead")]
    assert Decimal(detail["owed_lkr"]) == Decimal(1200)
    assert client.get(f"{ADMIN}/people/999/detail").status_code == 404

    # Facilitator links go with the person; the pending expense blocks the delete.
    resp = client.delete(f"{ADMIN}/people/{person['id']}")
    assert resp.status_code == 409
    assert "transaction(s)" in resp.json()["detail"]
