import csv
import io
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, func, select

from app.models import FinanceCategory, Transaction, TripBudgetLine, TripDirectCost, TripTraveller
from app.services.finance import ensure_default_categories

API = "/api/v1"
ADMIN = f"{API}/admin"
TODAY = date.today().isoformat()


@pytest.fixture
def categories(session: Session) -> dict[str, int]:
    ensure_default_categories(session)
    return {c.name: c.id for c in session.exec(select(FinanceCategory)).all()}


@pytest.fixture
def account(client: TestClient) -> dict:
    return _post(client, "accounts", {"name": "Test account", "opening_balance_lkr": "0", "opening_date": "2020-01-01"})


def _post(client: TestClient, path: str, body: dict, expected: int = 201) -> dict:
    resp = client.post(f"{ADMIN}/{path}", json=body)
    assert resp.status_code == expected, resp.text
    return resp.json()


def _trip(client: TestClient, name: str = "WSDC 2026 Testland", event: str = "WSDC", **fields) -> dict:
    return _post(client, "trips", {"name": name, "event": event, **fields})


def _traveller(client: TestClient, trip: dict, name: str, **fields) -> dict:
    return _post(client, "trip-travellers", {"trip_id": trip["id"], "name": name, **fields})


def _travellers(client: TestClient, trip: dict, n: int) -> list[dict]:
    return [_traveller(client, trip, f"Traveller {i}") for i in range(1, n + 1)]


def _budget(client: TestClient, trip: dict, category: str, amount: str, expected: int = 201, **fields) -> dict:
    return _post(client, "trip-budget-lines", {"trip_id": trip["id"], "category": category, "amount_lkr": amount, **fields}, expected)


def _direct(client: TestClient, trip: dict, category: str, amount: str, expected: int = 201, **fields) -> dict:
    return _post(client, "trip-direct-costs", {"trip_id": trip["id"], "category": category, "amount_lkr": amount, **fields}, expected)


def _expense(client: TestClient, trip: dict | None, category: str | None, amount: str, categories, account, expected: int = 201, **fields) -> dict:
    body = {
        "kind": "expense",
        "occurred_on": TODAY,
        "description": "Trip cost",
        "amount_lkr": amount,
        "category_id": categories["WSDC"],
        "account_id": account["id"],
        **fields,
    }
    if trip is not None:
        body["trip_id"] = trip["id"]
    if category is not None:
        body["trip_cost_category"] = category
    return _post(client, "transactions", body, expected)


def _costs(client: TestClient, trip: dict) -> dict:
    resp = client.get(f"{ADMIN}/trips/{trip['id']}/costs")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _by(rows: list[dict], key: str) -> dict:
    return {r[key]: r for r in rows}


def _completed_trip(client: TestClient, categories, account, name: str, n: int, flights: str, visa: str, event: str = "WSDC") -> dict:
    """A completed trip with `n` travellers: flights paid by the DC, visas paid by families."""
    trip = _trip(client, name, event, status="completed")
    _travellers(client, trip, n)
    _expense(client, trip, "flights", flights, categories, account)
    _direct(client, trip, "visa", visa)
    return trip


# --- trips and children ------------------------------------------------------------------


def test_trip_crud_filters_and_date_order(client: TestClient) -> None:
    squad = _post(client, "squads", {"kind": "national", "season": "2025/26"})
    trip = _trip(client, squad_id=squad["id"], country="Testland", start_on="2026-08-01", end_on="2026-08-10")
    _trip(client, "Other cup", "Regional", status="confirmed")
    assert len(client.get(f"{ADMIN}/trips", params={"event": "WSDC"}).json()) == 1
    assert len(client.get(f"{ADMIN}/trips", params={"status": "confirmed"}).json()) == 1
    assert len(client.get(f"{ADMIN}/trips", params={"squad_id": squad["id"]}).json()) == 1
    assert client.patch(f"{ADMIN}/trips/{trip['id']}", json={"status": "completed"}).json()["status"] == "completed"
    _post(client, "trips", {"name": "Bad", "event": "WSDC", "squad_id": 9999}, 404)
    _post(client, "trips", {"name": "Bad", "event": "WSDC", "start_on": "2026-02-02", "end_on": "2026-02-01"}, 422)
    _post(client, "trips", {"name": "", "event": "WSDC"}, 422)


def test_budget_line_and_direct_cost_traveller_must_be_on_the_trip(client: TestClient) -> None:
    one, two = _trip(client, "Trip one"), _trip(client, "Trip two")
    mine = _traveller(client, one, "Traveller A")
    theirs = _traveller(client, two, "Traveller B")
    _budget(client, one, "flights", "100.00", traveller_id=mine["id"])
    resp = client.post(f"{ADMIN}/trip-budget-lines", json={"trip_id": one["id"], "category": "flights", "amount_lkr": "5", "traveller_id": theirs["id"]})
    assert resp.status_code == 422 and "not on this trip" in resp.json()["detail"]
    _direct(client, one, "visa", "50", traveller_id=mine["id"])
    _direct(client, one, "visa", "50", 422, traveller_id=theirs["id"])
    line = _budget(client, one, "food", "10")
    # Re-pointing an existing line at another trip's traveller is refused as well.
    assert client.patch(f"{ADMIN}/trip-budget-lines/{line['id']}", json={"traveller_id": theirs["id"]}).status_code == 422
    assert client.patch(f"{ADMIN}/trip-budget-lines/{line['id']}", json={"traveller_id": mine["id"]}).status_code == 200


def test_amounts_must_be_positive_and_links_http(client: TestClient) -> None:
    trip = _trip(client)
    _budget(client, trip, "flights", "0", 422)
    _direct(client, trip, "visa", "-5", 422)
    _direct(client, trip, "visa", "5", 422, receipt_url="javascript:alert(1)")
    _direct(client, trip, "visa", "5", 422, original_currency="USDX")
    ok = _direct(client, trip, "visa", "5", original_amount="1.50", original_currency="usd", receipt_url="https://example.com/r")
    assert ok["original_currency"] == "USD"


def test_deleting_a_traveller_unlinks_their_lines(client: TestClient, categories, account) -> None:
    trip = _trip(client)
    traveller = _traveller(client, trip, "Traveller A")
    budget = _budget(client, trip, "flights", "100", traveller_id=traveller["id"])
    direct = _direct(client, trip, "visa", "20", traveller_id=traveller["id"])
    expense = _expense(client, trip, "food", "5", categories, account, trip_traveller_id=traveller["id"])
    assert client.delete(f"{ADMIN}/trip-travellers/{traveller['id']}").status_code == 204
    assert client.get(f"{ADMIN}/trip-budget-lines/{budget['id']}").json()["traveller_id"] is None
    assert client.get(f"{ADMIN}/trip-direct-costs/{direct['id']}").json()["traveller_id"] is None
    assert client.get(f"{ADMIN}/transactions/{expense['id']}").json()["trip_traveller_id"] is None


# --- transaction trip rules --------------------------------------------------------------


def test_transaction_trip_rules(client: TestClient, categories, account) -> None:
    trip, other = _trip(client), _trip(client, "Other trip")
    mine = _traveller(client, trip, "Traveller A")
    theirs = _traveller(client, other, "Traveller B")

    ok = _expense(client, trip, "flights", "100", categories, account, trip_traveller_id=mine["id"])
    assert (ok["trip_id"], ok["trip_cost_category"], ok["trip_traveller_id"]) == (trip["id"], "flights", mine["id"])

    # A category is required with a trip, and the traveller must be on it.
    assert "category" in _expense(client, trip, None, "5", categories, account, 422)["detail"]
    assert "not on this trip" in _expense(client, trip, "food", "5", categories, account, 422, trip_traveller_id=theirs["id"])["detail"]
    # Category / traveller without a trip.
    assert "needs a trip" in _expense(client, None, "food", "5", categories, account, 422)["detail"]
    assert "needs a trip" in _expense(client, None, None, "5", categories, account, 422, trip_traveller_id=mine["id"])["detail"]
    # Unknown trip / traveller.
    _expense(client, {"id": 9999}, "food", "5", categories, account, 404)
    _expense(client, trip, "food", "5", categories, account, 404, trip_traveller_id=9999)


def test_only_expenses_can_be_trip_costs(client: TestClient, categories, account) -> None:
    trip = _trip(client)
    income = {
        "kind": "income",
        "occurred_on": TODAY,
        "description": "Sponsor",
        "amount_lkr": "10",
        "category_id": categories["Sponsorship"],
        "account_id": account["id"],
        "trip_id": trip["id"],
        "trip_cost_category": "other",
    }
    resp = client.post(f"{ADMIN}/transactions", json=income)
    assert resp.status_code == 422 and "expense" in resp.json()["detail"]
    other = _post(client, "accounts", {"name": "Second", "opening_balance_lkr": "0", "opening_date": "2020-01-01"})
    transfer = {"kind": "transfer", "occurred_on": TODAY, "description": "Move", "amount_lkr": "10", "account_id": account["id"], "to_account_id": other["id"], "trip_id": trip["id"], "trip_cost_category": "other"}
    assert client.post(f"{ADMIN}/transactions", json=transfer).status_code == 422
    # Turning a trip expense into income is refused too.
    expense = _expense(client, trip, "food", "5", categories, account)
    resp = client.patch(f"{ADMIN}/transactions/{expense['id']}", json={"kind": "income", "category_id": categories["Sponsorship"]})
    assert resp.status_code == 422


def test_transaction_trip_fields_can_be_cleared(client: TestClient, categories, account) -> None:
    trip = _trip(client)
    expense = _expense(client, trip, "food", "5", categories, account)
    resp = client.patch(f"{ADMIN}/transactions/{expense['id']}", json={"trip_id": None, "trip_cost_category": None})
    assert resp.status_code == 200, resp.text
    assert resp.json()["trip_id"] is None
    assert client.delete(f"{ADMIN}/trips/{trip['id']}").status_code == 204


def test_transactions_filter_by_trip(client: TestClient, categories, account) -> None:
    one, two = _trip(client, "Trip one"), _trip(client, "Trip two")
    _expense(client, one, "food", "5", categories, account)
    _expense(client, two, "food", "7", categories, account)
    _expense(client, None, None, "9", categories, account)
    rows = client.get(f"{ADMIN}/transactions", params={"trip_id": one["id"]}).json()
    assert [r["amount_lkr"] for r in rows] == ["5.00"]


def test_csv_has_trip_columns(client: TestClient, categories, account) -> None:
    trip = _trip(client, "WSDC 2026 Testland")
    _expense(client, trip, "flights", "100", categories, account)
    _expense(client, None, None, "9", categories, account)
    text = client.get(f"{ADMIN}/transactions/export.csv").text
    rows = list(csv.DictReader(io.StringIO(text)))
    by_amount = {r["amount_lkr"]: r for r in rows}
    assert (by_amount["100.00"]["trip"], by_amount["100.00"]["trip_category"]) == ("WSDC 2026 Testland", "flights")
    assert (by_amount["9.00"]["trip"], by_amount["9.00"]["trip_category"]) == ("", "")
    filtered = client.get(f"{ADMIN}/transactions/export.csv", params={"trip_id": trip["id"]}).text
    assert len(list(csv.DictReader(io.StringIO(filtered)))) == 1


# --- costs -------------------------------------------------------------------------------


def test_trip_costs_by_category_traveller_and_person(client: TestClient, categories, account) -> None:
    trip, other = _trip(client), _trip(client, "Other trip")
    a, b = _traveller(client, trip, "Traveller A"), _traveller(client, trip, "Traveller B")
    _budget(client, trip, "flights", "1000.00")
    _budget(client, trip, "visa", "200.00", traveller_id=a["id"])
    _budget(client, trip, "food", "300.00")
    _expense(client, trip, "flights", "900.00", categories, account, trip_traveller_id=a["id"])
    _expense(client, trip, "flights", "950.50", categories, account, trip_traveller_id=b["id"])
    _expense(client, trip, "insurance", "40.00", categories, account)
    # A pending reimbursement is spent even though no account has paid yet.
    person = _post(client, "people", {"name": "Test Treasurer", "kind": "board"})
    _expense(client, trip, "food", "60.00", categories, account, paid_by_person_id=person["id"], reimbursement_status="pending", account_id=None)
    _direct(client, trip, "visa", "250.00", traveller_id=a["id"], paid_by="Parent of A")
    _direct(client, trip, "flights", "20.00")
    # Another trip's costs never leak in.
    _expense(client, other, "flights", "5000.00", categories, account)
    _direct(client, other, "visa", "5000.00")

    costs = _costs(client, trip)
    assert costs["travellers"] == 2
    assert Decimal(costs["budget_lkr"]) == Decimal("1500.00")
    assert Decimal(costs["dc_lkr"]) == Decimal("1950.50")
    assert Decimal(costs["direct_lkr"]) == Decimal("270.00")
    assert Decimal(costs["actual_lkr"]) == Decimal("2220.50")
    assert Decimal(costs["variance_lkr"]) == Decimal("720.50")
    assert Decimal(costs["per_person_actual_lkr"]) == Decimal("1110.25")
    assert Decimal(costs["per_person_budget_lkr"]) == Decimal("750.00")

    cats = _by(costs["by_category"], "category")
    assert [c["category"] for c in costs["by_category"]] == ["flights", "visa", "food", "insurance"]
    flights = cats["flights"]
    assert [Decimal(flights[k]) for k in ("budget_lkr", "dc_lkr", "direct_lkr", "actual_lkr", "variance_lkr")] == [
        Decimal("1000.00"), Decimal("1850.50"), Decimal("20.00"), Decimal("1870.50"), Decimal("870.50"),
    ]
    assert (Decimal(cats["visa"]["direct_lkr"]), Decimal(cats["visa"]["variance_lkr"])) == (Decimal("250.00"), Decimal("50.00"))
    assert Decimal(cats["food"]["dc_lkr"]) == Decimal("60.00")
    assert Decimal(cats["insurance"]["budget_lkr"]) == Decimal("0")

    people = _by(costs["by_traveller"], "name")
    assert list(people) == ["Traveller A", "Traveller B", "Shared"]
    assert people["Shared"]["traveller_id"] is None
    assert Decimal(people["Traveller A"]["actual_lkr"]) == Decimal("1150.00")  # 900 DC flight + 250 direct visa
    assert Decimal(people["Traveller A"]["budget_lkr"]) == Decimal("200.00")
    assert Decimal(people["Traveller B"]["dc_lkr"]) == Decimal("950.50")
    assert Decimal(people["Shared"]["budget_lkr"]) == Decimal("1300.00")
    assert Decimal(people["Shared"]["dc_lkr"]) == Decimal("100.00")  # insurance + food
    assert Decimal(people["Shared"]["direct_lkr"]) == Decimal("20.00")


def test_trip_costs_with_no_travellers_has_no_per_person(client: TestClient) -> None:
    trip = _trip(client)
    _budget(client, trip, "flights", "100")
    costs = _costs(client, trip)
    assert costs["travellers"] == 0
    assert costs["per_person_actual_lkr"] is None and costs["per_person_budget_lkr"] is None
    assert [r["name"] for r in costs["by_traveller"]] == ["Shared"]
    empty = _costs(client, _trip(client, "Empty"))
    assert empty["by_category"] == [] and empty["by_traveller"] == []
    assert client.get(f"{ADMIN}/trips/9999/costs").status_code == 404


def test_direct_costs_stay_out_of_the_ledger(client: TestClient, categories, account) -> None:
    trip = _trip(client)
    _expense(client, trip, "flights", "100.00", categories, account)
    _direct(client, trip, "visa", "900.00")
    summary = client.get(f"{ADMIN}/finance/summary", params={"start": f"{date.today().year}-01-01", "end": f"{date.today().year}-12-31"}).json()
    assert Decimal(summary["expense_lkr"]) == Decimal("100.00")
    assert Decimal(_costs(client, trip)["actual_lkr"]) == Decimal("1000.00")


# --- forecast ----------------------------------------------------------------------------


def test_forecast_averages_completed_trips_of_the_same_event(client: TestClient, categories, account) -> None:
    _completed_trip(client, categories, account, "WSDC 2024", 4, flights="4000.00", visa="400.00")  # 1000 + 100 pp
    _completed_trip(client, categories, account, "WSDC 2025", 2, flights="3000.00", visa="1000.00")  # 1500 + 500 pp
    _completed_trip(client, categories, account, "wsdc 2023", 1, flights="500.00", visa="0.01")  # event match ignores case
    # Excluded: another event, not completed, no travellers, no costs.
    _completed_trip(client, categories, account, "Regional 2025", 3, flights="9000.00", visa="9000.00", event="Regional")
    planning = _trip(client, "WSDC planned")
    _travellers(client, planning, 2)
    _direct(client, planning, "visa", "9000")
    nobody = _trip(client, "WSDC nobody", status="completed")
    _direct(client, nobody, "visa", "9000")
    free = _trip(client, "WSDC free", status="completed")
    _travellers(client, free, 3)

    resp = client.get(f"{ADMIN}/trips/forecast", params={"event": "WSDC", "travellers": 5})
    assert resp.status_code == 200, resp.text
    forecast = resp.json()
    assert sorted(t["name"] for t in forecast["trips"]) == ["WSDC 2024", "WSDC 2025", "wsdc 2023"]
    by_name = _by(forecast["trips"], "name")
    assert (by_name["WSDC 2024"]["travellers"], Decimal(by_name["WSDC 2024"]["per_person_lkr"])) == (4, Decimal("1100.00"))
    cats = _by(forecast["by_category"], "category")
    # flights: (1000 + 1500 + 500) / 3 = 1000 pp; visa: (100 + 500 + 0.01) / 3 = 200.00 pp.
    assert Decimal(cats["flights"]["per_person_lkr"]) == Decimal("1000.00")
    assert Decimal(cats["flights"]["suggested_lkr"]) == Decimal("5000.00")
    assert Decimal(cats["visa"]["per_person_lkr"]) == Decimal("200.00")
    assert Decimal(cats["visa"]["suggested_lkr"]) == Decimal("1000.02")
    assert Decimal(forecast["suggested_total_lkr"]) == Decimal("6000.02")
    assert forecast["travellers"] == 5


def test_forecast_excludes_the_current_trip_and_handles_no_history(client: TestClient, categories, account) -> None:
    only = _completed_trip(client, categories, account, "WSDC 2025", 2, flights="2000.00", visa="0.01")
    assert len(client.get(f"{ADMIN}/trips/forecast", params={"event": "WSDC"}).json()["trips"]) == 1
    empty = client.get(f"{ADMIN}/trips/forecast", params={"event": "WSDC", "exclude_trip_id": only["id"]}).json()
    assert empty["trips"] == [] and empty["by_category"] == [] and Decimal(empty["suggested_total_lkr"]) == 0
    assert client.get(f"{ADMIN}/trips/forecast").status_code == 422  # event is required


# --- copy budget -------------------------------------------------------------------------


def test_copy_budget_plain_scaled_and_replace(client: TestClient) -> None:
    source, target = _trip(client, "Source"), _trip(client, "Target")
    sa = _traveller(client, source, "S1")
    _travellers(client, source, 3)  # 4 travellers on the source
    _travellers(client, target, 2)
    _budget(client, source, "flights", "4000.00", description="Return fares", traveller_id=sa["id"])
    _budget(client, source, "visa", "100.00")
    _budget(client, target, "other", "1.00")

    resp = client.post(f"{ADMIN}/trips/{target['id']}/copy-budget", json={"from_trip_id": source["id"]})
    assert resp.status_code == 201, resp.text
    copied = resp.json()
    assert sorted(Decimal(c["amount_lkr"]) for c in copied) == [Decimal("100.00"), Decimal("4000.00")]
    assert all(c["traveller_id"] is None and c["trip_id"] == target["id"] for c in copied)
    assert next(c for c in copied if c["category"] == "flights")["description"] == "Return fares"
    # Not replaced: the old line is still there.
    assert len(client.get(f"{ADMIN}/trip-budget-lines", params={"trip_id": target["id"]}).json()) == 3

    scaled = client.post(f"{ADMIN}/trips/{target['id']}/copy-budget", json={"from_trip_id": source["id"], "scale": True, "replace": True})
    assert scaled.status_code == 201, scaled.text
    assert sorted(Decimal(c["amount_lkr"]) for c in scaled.json()) == [Decimal("50.00"), Decimal("2000.00")]
    lines = client.get(f"{ADMIN}/trip-budget-lines", params={"trip_id": target["id"]}).json()
    assert sorted(Decimal(c["amount_lkr"]) for c in lines) == [Decimal("50.00"), Decimal("2000.00")]
    # The source is untouched.
    assert len(client.get(f"{ADMIN}/trip-budget-lines", params={"trip_id": source["id"]}).json()) == 2


def test_copy_budget_errors(client: TestClient) -> None:
    source, target, empty = _trip(client, "Source"), _trip(client, "Target"), _trip(client, "Empty")
    _budget(client, source, "visa", "100.00")
    _travellers(client, source, 2)
    path = f"{ADMIN}/trips/{target['id']}/copy-budget"
    # Scaling needs travellers on both trips.
    assert client.post(path, json={"from_trip_id": source["id"], "scale": True}).status_code == 422
    _travellers(client, empty, 2)
    assert client.post(path, json={"from_trip_id": empty["id"]}).status_code == 422  # nothing to copy
    assert client.post(path, json={"from_trip_id": target["id"]}).status_code == 422  # itself
    assert client.post(path, json={"from_trip_id": 9999}).status_code == 404
    assert client.post(f"{ADMIN}/trips/9999/copy-budget", json={"from_trip_id": source["id"]}).status_code == 404
    # A failed scaled copy with replace leaves the existing lines alone.
    _budget(client, target, "other", "7.00")
    assert client.post(path, json={"from_trip_id": source["id"], "scale": True, "replace": True}).status_code == 422
    assert len(client.get(f"{ADMIN}/trip-budget-lines", params={"trip_id": target["id"]}).json()) == 1


# --- apply forecast ----------------------------------------------------------------------


def test_apply_forecast_creates_one_line_per_category(client: TestClient, categories, account) -> None:
    _completed_trip(client, categories, account, "WSDC 2024", 4, flights="4000.00", visa="400.00")
    _completed_trip(client, categories, account, "WSDC 2025", 2, flights="3000.00", visa="1000.00")
    trip = _trip(client, "WSDC 2026")
    _travellers(client, trip, 3)
    _budget(client, trip, "other", "1.00")

    resp = client.post(f"{ADMIN}/trips/{trip['id']}/apply-forecast", json={})
    assert resp.status_code == 201, resp.text
    lines = _by(resp.json(), "category")
    # flights (1000 + 1500) / 2 = 1250 pp; visa (100 + 500) / 2 = 300 pp; times 3 travellers.
    assert Decimal(lines["flights"]["amount_lkr"]) == Decimal("3750.00")
    assert Decimal(lines["visa"]["amount_lkr"]) == Decimal("900.00")
    assert lines["flights"]["description"] == "Forecast from 2 past trips"
    assert len(client.get(f"{ADMIN}/trip-budget-lines", params={"trip_id": trip["id"]}).json()) == 3

    replaced = client.post(f"{ADMIN}/trips/{trip['id']}/apply-forecast", json={"replace": True})
    assert replaced.status_code == 201
    assert len(client.get(f"{ADMIN}/trip-budget-lines", params={"trip_id": trip["id"]}).json()) == 2
    # The body is optional.
    assert client.post(f"{ADMIN}/trips/{trip['id']}/apply-forecast").status_code == 201


def test_apply_forecast_needs_history_and_travellers(client: TestClient, categories, account) -> None:
    trip = _trip(client, "WSDC 2026")
    _travellers(client, trip, 2)
    assert client.post(f"{ADMIN}/trips/{trip['id']}/apply-forecast", json={}).status_code == 422  # no past trips
    _completed_trip(client, categories, account, "WSDC 2025", 2, flights="2000.00", visa="1.00")
    nobody = _trip(client, "WSDC no travellers")
    assert client.post(f"{ADMIN}/trips/{nobody['id']}/apply-forecast", json={}).status_code == 422
    assert client.post(f"{ADMIN}/trips/{trip['id']}/apply-forecast", json={}).status_code == 201
    assert client.post(f"{ADMIN}/trips/9999/apply-forecast", json={}).status_code == 404


# --- deleting ----------------------------------------------------------------------------


def test_trip_delete_is_409_with_ledger_expenses(client: TestClient, categories, account) -> None:
    trip = _trip(client)
    expense = _expense(client, trip, "flights", "100", categories, account)
    resp = client.delete(f"{ADMIN}/trips/{trip['id']}")
    assert resp.status_code == 409
    assert "1 ledger expense" in resp.json()["detail"] and "unlink or delete" in resp.json()["detail"]
    assert client.get(f"{ADMIN}/trips/{trip['id']}").status_code == 200
    assert client.delete(f"{ADMIN}/transactions/{expense['id']}").status_code == 204
    assert client.delete(f"{ADMIN}/trips/{trip['id']}").status_code == 204


def test_trip_delete_cascades_its_children(session: Session, client: TestClient) -> None:
    trip = _trip(client)
    traveller = _traveller(client, trip, "Traveller A")
    _budget(client, trip, "flights", "100", traveller_id=traveller["id"])
    _direct(client, trip, "visa", "10")
    assert client.delete(f"{ADMIN}/trips/{trip['id']}").status_code == 204
    for model in (TripTraveller, TripBudgetLine, TripDirectCost):
        assert session.exec(select(func.count()).select_from(model)).one() == 0


def test_tournament_and_squad_delete_null_the_trip_links(client: TestClient) -> None:
    tournament = client.post(f"{API}/tournaments", json={"name": "Cup", "slug": "cup"}).json()
    squad = _post(client, "squads", {"kind": "national", "season": "2025/26"})
    trip = _trip(client, tournament_id=tournament["id"], squad_id=squad["id"])
    assert client.delete(f"{API}/tournaments/{tournament['id']}").status_code == 204
    assert client.delete(f"{ADMIN}/squads/{squad['id']}").status_code == 204
    after = client.get(f"{ADMIN}/trips/{trip['id']}").json()
    assert (after["tournament_id"], after["squad_id"]) == (None, None)
