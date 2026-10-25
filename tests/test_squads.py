from fastapi.testclient import TestClient
from sqlmodel import Session, func, select

from app.models import SquadMember

API = "/api/v1"
ADMIN = f"{API}/admin"


def _post(client: TestClient, path: str, body: dict, expected: int = 201) -> dict:
    resp = client.post(f"{ADMIN}/{path}", json=body)
    assert resp.status_code == expected, resp.text
    return resp.json()


def _squad(client: TestClient, kind: str = "national", season: str = "2025/26", **fields) -> dict:
    return _post(client, "squads", {"kind": kind, "season": season, **fields})


def _debater(client: TestClient, name: str, **fields) -> dict:
    resp = client.post(f"{API}/debaters", json={"full_name": name, **fields})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _member(client: TestClient, squad: dict, debater: dict, expected: int = 201, **fields) -> dict:
    return _post(client, "squad-members", {"squad_id": squad["id"], "debater_id": debater["id"], **fields}, expected)


# --- squads ------------------------------------------------------------------------------


def test_squad_crud_and_filters(client: TestClient) -> None:
    national = _squad(client, "national", "2025/26", notes="WSDC squad")
    _squad(client, "development", "2025/26")
    _squad(client, "national", "2024/25")

    listed = client.get(f"{ADMIN}/squads").json()
    # Newest season first.
    assert [(s["season"], s["kind"]) for s in listed] == [
        ("2025/26", "national"),
        ("2025/26", "development"),
        ("2024/25", "national"),
    ]
    assert len(client.get(f"{ADMIN}/squads", params={"kind": "national"}).json()) == 2
    assert len(client.get(f"{ADMIN}/squads", params={"season": "2024/25"}).json()) == 1

    resp = client.patch(f"{ADMIN}/squads/{national['id']}", json={"notes": "Updated"})
    assert resp.status_code == 200 and resp.json()["notes"] == "Updated"
    assert client.get(f"{ADMIN}/squads/{national['id']}").json()["season"] == "2025/26"
    assert client.delete(f"{ADMIN}/squads/{national['id']}").status_code == 204
    assert client.get(f"{ADMIN}/squads/{national['id']}").status_code == 404


def test_squad_kind_and_season_are_unique(client: TestClient) -> None:
    _squad(client, "national", "2025/26")
    resp = client.post(f"{ADMIN}/squads", json={"kind": "national", "season": "2025/26"})
    assert resp.status_code == 409
    other = _squad(client, "development", "2025/26")
    # Renaming into an existing pair conflicts too.
    assert client.patch(f"{ADMIN}/squads/{other['id']}", json={"kind": "national"}).status_code == 409


def test_squad_season_is_required(client: TestClient) -> None:
    resp = client.post(f"{ADMIN}/squads", json={"kind": "national", "season": ""})
    assert resp.status_code == 422


# --- members and staff -------------------------------------------------------------------


def test_duplicate_member_is_409_and_unknown_parents_404(client: TestClient) -> None:
    squad = _squad(client)
    debater = _debater(client, "Test Debater One")
    _member(client, squad, debater)
    _member(client, squad, debater, expected=409)
    _post(client, "squad-members", {"squad_id": 9999, "debater_id": debater["id"]}, 404)
    _post(client, "squad-members", {"squad_id": squad["id"], "debater_id": 9999}, 404)
    # The same debater can be on another squad.
    _member(client, _squad(client, "development"), debater)


def test_member_dates_must_be_in_order(client: TestClient) -> None:
    squad = _squad(client)
    debater = _debater(client, "Test Debater One")
    _member(client, squad, debater, expected=422, joined_on="2026-02-01", left_on="2026-01-01")


def test_member_filters(client: TestClient) -> None:
    squad, other = _squad(client), _squad(client, "development")
    one, two = _debater(client, "Test Debater One"), _debater(client, "Test Debater Two")
    _member(client, squad, one)
    _member(client, squad, two)
    _member(client, other, one)
    assert len(client.get(f"{ADMIN}/squad-members", params={"squad_id": squad["id"]}).json()) == 2
    assert len(client.get(f"{ADMIN}/squad-members", params={"debater_id": one["id"]}).json()) == 2


def test_roster_has_members_with_schools_and_staff(client: TestClient) -> None:
    school = client.post(f"{API}/institutions", json={"name": "Test College"}).json()
    squad = _squad(client)
    captain = _debater(client, "Test Captain", institution_id=school["id"])
    member = _debater(client, "Test Member")
    reserve = client.post(f"{API}/debaters", json={"first_name": "Test", "last_name": "Reserve"}).json()
    _member(client, squad, member)
    _member(client, squad, reserve, role="reserve")
    _member(client, squad, captain, role="captain", joined_on="2025-09-01")
    person = _post(client, "people", {"name": "Test Coach", "kind": "volunteer"})
    _post(client, "squad-staff", {"squad_id": squad["id"], "name": "Test Coach", "role": "coach", "person_id": person["id"]})
    _post(client, "squad-staff", {"squad_id": squad["id"], "name": "Test Manager", "role": "manager"})
    _post(client, "squad-staff", {"squad_id": 9999, "name": "Nobody"}, 404)

    roster = client.get(f"{ADMIN}/squads/{squad['id']}/roster").json()
    assert (roster["kind"], roster["season"]) == ("national", "2025/26")
    assert [(m["debater_name"], m["role"], m["school_name"]) for m in roster["members"]] == [
        ("Test Captain", "captain", "Test College"),
        ("Test Member", "member", None),
        ("Test Reserve", "reserve", None),
    ]
    assert roster["members"][0]["joined_on"] == "2025-09-01"
    assert [(s["name"], s["role"]) for s in roster["staff"]] == [("Test Coach", "coach"), ("Test Manager", "manager")]
    assert roster["staff"][0]["person_id"] == person["id"]
    assert client.get(f"{ADMIN}/squads/9999/roster").status_code == 404


def test_deleting_a_squad_removes_members_and_staff(session: Session, client: TestClient) -> None:
    squad = _squad(client)
    _member(client, squad, _debater(client, "Test Debater One"))
    _post(client, "squad-staff", {"squad_id": squad["id"], "name": "Test Coach"})
    assert client.delete(f"{ADMIN}/squads/{squad['id']}").status_code == 204
    assert session.exec(select(func.count()).select_from(SquadMember)).one() == 0
    assert client.get(f"{ADMIN}/squad-staff").json() == []


def test_deleting_a_person_unlinks_staff(client: TestClient) -> None:
    squad = _squad(client)
    person = _post(client, "people", {"name": "Test Coach", "kind": "staff"})
    staff = _post(client, "squad-staff", {"squad_id": squad["id"], "name": "Test Coach", "person_id": person["id"]})
    assert client.delete(f"{ADMIN}/people/{person['id']}").status_code == 204
    assert client.get(f"{ADMIN}/squad-staff/{staff['id']}").json()["person_id"] is None


# --- debater FK rules --------------------------------------------------------------------


def test_deleting_a_debater_on_a_squad_is_409(client: TestClient) -> None:
    squad = _squad(client)
    debater = _debater(client, "Test Debater One")
    member = _member(client, squad, debater)
    resp = client.delete(f"{API}/debaters/{debater['id']}")
    assert resp.status_code == 409 and "squad" in resp.json()["detail"]
    assert client.delete(f"{ADMIN}/squad-members/{member['id']}").status_code == 204
    assert client.delete(f"{API}/debaters/{debater['id']}").status_code == 204


def test_merge_repoints_squad_memberships(session: Session, client: TestClient) -> None:
    source, target = _debater(client, "Test Debater One"), _debater(client, "Test Debater One")
    squad, other = _squad(client), _squad(client, "development")
    _member(client, squad, source, role="captain")
    _member(client, other, source)
    resp = client.post(f"{API}/debaters/merge", json={"source_id": source["id"], "target_id": target["id"]})
    assert resp.status_code == 200, resp.text
    rows = session.exec(select(SquadMember)).all()
    assert sorted((r.squad_id, r.debater_id) for r in rows) == sorted(
        [(squad["id"], target["id"]), (other["id"], target["id"])]
    )


def test_merge_with_both_debaters_in_one_squad_keeps_the_targets_row(session: Session, client: TestClient) -> None:
    source, target = _debater(client, "Test Debater One"), _debater(client, "Test Debater One")
    squad, other = _squad(client), _squad(client, "development")
    _member(client, squad, source, role="captain")
    _member(client, squad, target, role="reserve")
    _member(client, other, source)
    resp = client.post(f"{API}/debaters/merge", json={"source_id": source["id"], "target_id": target["id"]})
    assert resp.status_code == 200, resp.text
    rows = {(r.squad_id, r.debater_id): r.role.value for r in session.exec(select(SquadMember)).all()}
    assert rows == {(squad["id"], target["id"]): "reserve", (other["id"], target["id"]): "member"}


# --- GET /debaters?q= --------------------------------------------------------------------


def test_debater_search_by_name(client: TestClient) -> None:
    _debater(client, "Nimal Perera")
    _debater(client, "Kasun PERERA")
    _debater(client, "Sam Silva")
    client.post(f"{API}/debaters", json={"first_name": "Amal", "last_name": "Perera"})
    client.post(f"{API}/debaters", json={"first_name": "100%", "last_name": "Sure"})

    def names(q: str) -> list[str]:
        resp = client.get(f"{API}/debaters", params={"q": q})
        assert resp.status_code == 200
        return sorted(d["full_name"] or f"{d['first_name']} {d['last_name']}" for d in resp.json())

    assert names("perera") == ["Amal Perera", "Kasun PERERA", "Nimal Perera"]
    assert names("amal per") == ["Amal Perera"]
    assert names("  silva ") == ["Sam Silva"]
    # LIKE wildcards are matched literally.
    assert names("%") == ["100% Sure"]
    assert names("_") == []
    assert len(client.get(f"{API}/debaters", params={"q": ""}).json()) == 5
    assert client.get(f"{API}/debaters", params={"q": "nobody"}).json() == []
