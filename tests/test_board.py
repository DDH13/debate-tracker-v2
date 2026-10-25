from datetime import date, timedelta

from fastapi.testclient import TestClient

ADMIN = "/api/v1/admin"
TODAY = date.today()


def _person(client: TestClient, name: str) -> dict:
    resp = client.post(f"{ADMIN}/people", json={"name": name, "kind": "board"})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _role(client: TestClient, person_id: int, title: str, start: date, end: date | None = None, expected: int = 201) -> dict:
    body = {"person_id": person_id, "title": title, "term_start": start.isoformat()}
    if end is not None:
        body["term_end"] = end.isoformat()
    resp = client.post(f"{ADMIN}/board-roles", json=body)
    assert resp.status_code == expected, resp.text
    return resp.json()


def test_board_splits_current_upcoming_and_past(client: TestClient) -> None:
    ann = _person(client, "Ann Chair")
    bo = _person(client, "Bo Treasurer")
    cy = _person(client, "Cy Former")
    _role(client, ann["id"], "President", TODAY - timedelta(days=200))
    _role(client, bo["id"], "Treasurer", TODAY - timedelta(days=10), TODAY + timedelta(days=100))
    _role(client, cy["id"], "President", date(2022, 1, 1), date(2023, 12, 31))
    _role(client, cy["id"], "Secretary", date(2020, 1, 1), date(2020, 12, 31))
    _role(client, bo["id"], "President", TODAY + timedelta(days=300))

    board = client.get(f"{ADMIN}/board").json()
    assert [(r["title"], r["person_name"]) for r in board["current"]] == [
        ("President", "Ann Chair"),
        ("Treasurer", "Bo Treasurer"),
    ]
    assert [r["title"] for r in board["upcoming"]] == ["President"]
    # Most recently ended first, with readable term labels.
    assert [(r["title"], r["term_label"]) for r in board["past"]] == [("President", "2022–2023"), ("Secretary", "2020")]
    assert board["current"][0]["term_label"].endswith("–")

    current = client.get(f"{ADMIN}/board-roles", params={"current": True}).json()
    assert len(current) == 2
    assert len(client.get(f"{ADMIN}/board-roles", params={"current": False}).json()) == 3
    assert len(client.get(f"{ADMIN}/board-roles", params={"person_id": cy["id"]}).json()) == 2


def test_term_end_cannot_precede_start(client: TestClient) -> None:
    ann = _person(client, "Ann Chair")
    _role(client, ann["id"], "President", date(2026, 1, 1), date(2025, 1, 1), expected=422)
    role = _role(client, ann["id"], "President", date(2026, 1, 1))
    resp = client.patch(f"{ADMIN}/board-roles/{role['id']}", json={"term_end": "2025-12-31"})
    assert resp.status_code == 422
    # Ending a term.
    resp = client.patch(f"{ADMIN}/board-roles/{role['id']}", json={"term_end": "2026-06-30"})
    assert resp.json()["term_end"] == "2026-06-30"
    assert _role(client, 999, "Ghost", date(2026, 1, 1), expected=404)


def test_person_with_board_history_cannot_be_deleted(client: TestClient) -> None:
    ann = _person(client, "Ann Chair")
    _role(client, ann["id"], "President", date(2024, 1, 1), date(2024, 12, 31))
    resp = client.delete(f"{ADMIN}/people/{ann['id']}")
    assert resp.status_code == 409
    assert "1 board role(s)" in resp.json()["detail"]
    assert "Deactivate" in resp.json()["detail"]
