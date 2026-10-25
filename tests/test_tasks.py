from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.models import User, UserRole

API = "/api/v1"
ADMIN = f"{API}/admin"
TODAY = date.today()


def _person(client: TestClient, name: str = "Vee Volunteer") -> dict:
    return client.post(f"{ADMIN}/people", json={"name": name}).json()


def _task(client: TestClient, expected: int = 201, **body) -> dict:
    body.setdefault("title", "Book the hall")
    resp = client.post(f"{ADMIN}/tasks", json=body)
    assert resp.status_code == expected, resp.text
    return resp.json()


def test_task_crud_stamps_creator_and_completion(client: TestClient) -> None:
    me = client.get(f"{API}/auth/me").json()
    person = _person(client)
    task = _task(client, assignee_person_id=person["id"], due_on=TODAY.isoformat(), priority="high")
    assert (task["status"], task["created_by_id"], task["completed_on"]) == ("todo", me["id"], None)

    done = client.patch(f"{ADMIN}/tasks/{task['id']}", json={"status": "done", "completion_note": "Booked"}).json()
    assert done["completed_on"] == TODAY.isoformat()
    assert done["updated_at"] >= task["updated_at"]

    # A given completion date is kept; reopening clears it.
    dated = _task(client, status="done", completed_on="2026-01-05")
    assert dated["completed_on"] == "2026-01-05"
    reopened = client.patch(f"{ADMIN}/tasks/{dated['id']}", json={"status": "in_progress"}).json()
    assert reopened["completed_on"] is None

    _task(client, expected=422, title="")
    _task(client, expected=404, assignee_person_id=999)


def test_task_filters_and_order(client: TestClient) -> None:
    ann = _person(client, "Ann")
    _task(client, title="late", assignee_person_id=ann["id"], due_on=(TODAY - timedelta(days=2)).isoformat())
    _task(client, title="soon", due_on=(TODAY + timedelta(days=3)).isoformat())
    _task(client, title="undated")
    _task(client, title="finished", status="done", due_on=(TODAY - timedelta(days=9)).isoformat())

    def titles(**params) -> list[str]:
        resp = client.get(f"{ADMIN}/tasks", params=params)
        assert resp.status_code == 200, resp.text
        return [t["title"] for t in resp.json()]

    assert titles() == ["finished", "late", "soon", "undated"]  # by due date, undated last
    assert titles(open=True) == ["late", "soon", "undated"]
    assert titles(open=False) == ["finished"]
    assert titles(overdue=True) == ["late"]
    assert titles(assignee_person_id=ann["id"]) == ["late"]
    assert titles(status="done") == ["finished"]


def test_deleting_linked_records_unlinks_tasks(client: TestClient) -> None:
    person = _person(client)
    tournament = client.post(f"{API}/tournaments", json={"name": "Cup", "slug": "cup"}).json()
    trip = client.post(f"{ADMIN}/trips", json={"name": "WSDC", "event": "WSDC"}).json()
    workshop = client.post(f"{ADMIN}/workshops", json={"title": "Intro", "held_on": "2026-03-01"}).json()
    task = _task(
        client, assignee_person_id=person["id"], tournament_id=tournament["id"], trip_id=trip["id"],
        workshop_id=workshop["id"],
    )
    for path in (f"{API}/tournaments/{tournament['id']}", f"{ADMIN}/trips/{trip['id']}",
                 f"{ADMIN}/workshops/{workshop['id']}", f"{ADMIN}/people/{person['id']}"):
        assert client.delete(path).status_code == 204, path
    stored = client.get(f"{ADMIN}/tasks/{task['id']}").json()
    assert [stored[k] for k in ("assignee_person_id", "tournament_id", "trip_id", "workshop_id")] == [None] * 4


# --- /me/tasks: the volunteer's read-only view ---------------------------------------------


def _link(session: Session, role: UserRole, person_id: int | None) -> None:
    user = session.exec(select(User).where(User.role == role)).one()
    user.person_id = person_id
    session.add(user)
    session.commit()


def test_volunteer_sees_only_their_own_tasks(
    client: TestClient, volunteer_client: TestClient, session: Session
) -> None:
    mine = _person(client, "Vee Volunteer")
    other = _person(client, "Oz Other")
    workshop = client.post(f"{ADMIN}/workshops", json={"title": "Judging 101", "held_on": "2026-03-01"}).json()
    _task(client, title="Done one", assignee_person_id=mine["id"], status="done")
    _task(client, title="Undated", assignee_person_id=mine["id"])
    _task(client, title="Overdue", assignee_person_id=mine["id"], due_on=(TODAY - timedelta(days=1)).isoformat(),
          workshop_id=workshop["id"])
    _task(client, title="Not mine", assignee_person_id=other["id"])

    assert volunteer_client.get(f"{API}/me/tasks").json() == {"linked": False, "tasks": []}

    _link(session, UserRole.VOLUNTEER, mine["id"])
    body = volunteer_client.get(f"{API}/me/tasks").json()
    assert body["linked"] is True
    assert [t["title"] for t in body["tasks"]] == ["Overdue", "Undated", "Done one"]
    overdue = body["tasks"][0]
    assert overdue["overdue"] is True and overdue["workshop_title"] == "Judging 101"
    assert body["tasks"][1]["overdue"] is False

    # Board users get their own tasks there too.
    _link(session, UserRole.BOARD, other["id"])
    assert [t["title"] for t in client.get(f"{API}/me/tasks").json()["tasks"]] == ["Not mine"]


def test_me_tasks_needs_a_session_and_is_read_only(
    anon_client: TestClient, volunteer_client: TestClient
) -> None:
    assert anon_client.get(f"{API}/me/tasks").status_code == 401
    for method in ("POST", "PATCH", "PUT", "DELETE"):
        assert volunteer_client.request(method, f"{API}/me/tasks").status_code == 405
    # And the board-only task routes stay closed to volunteers.
    assert volunteer_client.get(f"{ADMIN}/tasks").status_code == 403
    assert volunteer_client.patch(f"{ADMIN}/tasks/1", json={"status": "done"}).status_code == 403


@pytest.mark.parametrize("status", ["todo", "in_progress", "cancelled"])
def test_completed_on_only_kept_when_done(client: TestClient, status: str) -> None:
    task = _task(client, status=status, completed_on="2026-01-01")
    assert task["completed_on"] is None
