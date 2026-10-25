from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.models import Feedback
from app.services.schools import MembershipStatus, membership_standing

API = "/api/v1"
ADMIN = f"{API}/admin"
THIS_YEAR = date.today().year


def _institution(client: TestClient, name: str, **fields) -> dict:
    resp = client.post(f"{API}/institutions", json={"name": name, **fields})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _membership(client: TestClient, institution_id: int, year: int, **fields) -> dict:
    resp = client.post(
        f"{ADMIN}/memberships", json={"institution_id": institution_id, "year": year, **fields}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _tournament_with_team(
    client: TestClient, slug: str, institution_id: int, when: str | None, teams: int = 1
) -> dict:
    tournament = client.post(
        f"{API}/tournaments", json={"name": slug.title(), "slug": slug, "date": when}
    ).json()
    for i in range(teams):
        client.post(
            f"{API}/tournaments/{tournament['id']}/teams",
            json={"name": f"{slug} {i}", "institution_id": institution_id},
        )
    return tournament


def _summary(client: TestClient, institution_id: int, year: int | None = None) -> dict:
    params = {"year": year} if year else {}
    rows = client.get(f"{ADMIN}/schools", params=params).json()
    return next(r for r in rows if r["institution_id"] == institution_id)


# --- membership_standing (pure) ------------------------------------------------------


@pytest.mark.parametrize(
    ("years", "year", "status", "irregular", "streak"),
    [
        ([2024, 2025, 2026], 2026, MembershipStatus.MEMBER, False, 3),
        ([2022, 2024, 2026], 2026, MembershipStatus.MEMBER, True, 1),
        ([2024, 2025], 2026, MembershipStatus.LAPSED, False, 0),
        ([2021, 2022], 2026, MembershipStatus.FORMER, False, 0),
        ([2020, 2022], 2026, MembershipStatus.FORMER, True, 0),
        ([], 2026, MembershipStatus.NEVER, False, 0),
        # A membership recorded in advance doesn't make them a member (or irregular) now.
        ([2028], 2026, MembershipStatus.NEVER, False, 0),
        ([2025, 2027], 2026, MembershipStatus.LAPSED, False, 0),
    ],
)
def test_membership_standing(years, year, status, irregular, streak) -> None:
    standing = membership_standing(years, year)
    assert standing.status == status
    assert standing.irregular is irregular
    assert standing.streak == streak
    assert standing.member_years == sorted(years)
    assert standing.first_year == (min(years) if years else None)
    assert standing.last_year == (max(years) if years else None)


# --- institution fields --------------------------------------------------------------


def test_institution_kind_and_district_round_trip(client: TestClient) -> None:
    inst = _institution(client, "Royal College", kind="school", district="Colombo")
    assert inst["kind"] == "school" and inst["district"] == "Colombo"
    updated = client.patch(f"{API}/institutions/{inst['id']}", json={"kind": "university"}).json()
    assert updated["kind"] == "university" and updated["district"] == "Colombo"
    assert client.patch(f"{API}/institutions/{inst['id']}", json={"kind": "bogus"}).status_code == 422


# --- schools list and detail ---------------------------------------------------------


def test_schools_summary_standing_tournaments_and_contact(client: TestClient) -> None:
    royal = _institution(client, "Royal College", kind="school")
    other = _institution(client, "Other School")
    _membership(client, royal["id"], THIS_YEAR - 1, fee_lkr="5000.00")
    this_year = _membership(client, royal["id"], THIS_YEAR, fee_lkr=7500, paid_on=f"{THIS_YEAR}-02-01")

    _tournament_with_team(client, "early-cup", royal["id"], "2024-03-01", teams=2)
    _tournament_with_team(client, "late-cup", royal["id"], "2025-08-01")
    _tournament_with_team(client, "undated-cup", royal["id"], None)

    client.post(f"{ADMIN}/school-contacts", json={"institution_id": royal["id"], "name": "Coach", "role": "coach"})
    poc = client.post(
        f"{ADMIN}/school-contacts",
        json={"institution_id": royal["id"], "name": "Ms Perera", "role": "point_of_contact"},
    ).json()
    client.post(
        f"{ADMIN}/feedback", json={"institution_id": royal["id"], "content": "Great event"}
    )
    client.post(
        f"{ADMIN}/feedback",
        json={"institution_id": royal["id"], "content": "Handled", "status": "addressed"},
    )

    row = _summary(client, royal["id"])
    assert row["standing"]["status"] == "member"
    assert row["standing"]["streak"] == 2
    assert row["membership"]["id"] == this_year["id"]
    assert row["membership"]["fee_lkr"] == "7500.00"
    assert row["tournament_count"] == 3
    assert row["latest_tournament"]["name"] == "Late-Cup"
    # With no primary contact, a point of contact beats other roles.
    assert row["primary_contact"]["id"] == poc["id"]
    assert row["open_feedback"] == 1

    # Looking at next year, they've lapsed.
    assert _summary(client, royal["id"], THIS_YEAR + 1)["standing"]["status"] == "lapsed"

    other_row = _summary(client, other["id"])
    assert other_row["standing"]["status"] == "never"
    assert other_row["tournament_count"] == 0
    assert other_row["latest_tournament"] is None
    assert other_row["primary_contact"] is None


def test_school_detail_merges_imported_and_manual_history(client: TestClient) -> None:
    royal = _institution(client, "Royal College")
    tournament = _tournament_with_team(client, "imported-cup", royal["id"], "2024-05-01", teams=2)
    client.post(
        f"{ADMIN}/school-participations",
        json={"institution_id": royal["id"], "year": 2025, "event_name": "Local Open", "teams": 1},
    )
    client.post(
        f"{ADMIN}/school-achievements",
        json={
            "institution_id": royal["id"],
            "year": 2024,
            "title": "Champions",
            "tournament_id": tournament["id"],
        },
    )

    detail = client.get(f"{ADMIN}/schools/{royal['id']}").json()
    assert detail["institution"]["name"] == "Royal College"
    assert [(h["source"], h["event_name"], h["year"], h["teams"]) for h in detail["history"]] == [
        ("manual", "Local Open", 2025, 1),
        ("imported", "Imported-Cup", 2024, 2),
    ]
    assert detail["history"][1]["tournament_id"] == tournament["id"]
    assert detail["achievements"][0]["title"] == "Champions"
    assert detail["summary"]["institution_id"] == royal["id"]
    assert client.get(f"{ADMIN}/schools/9999").status_code == 404


# --- generated CRUD ------------------------------------------------------------------


def test_membership_crud_conflict_and_validation(client: TestClient) -> None:
    inst = _institution(client, "Royal College")
    created = _membership(client, inst["id"], 2025, reason="Joined after workshop")

    dup = client.post(f"{ADMIN}/memberships", json={"institution_id": inst["id"], "year": 2025})
    assert dup.status_code == 409
    assert "already has a membership" in dup.json()["detail"]

    missing_parent = client.post(f"{ADMIN}/memberships", json={"institution_id": 9999, "year": 2025})
    assert missing_parent.status_code == 404

    assert client.post(
        f"{ADMIN}/memberships", json={"institution_id": inst["id"], "year": 1800}
    ).status_code == 422
    assert client.post(
        f"{ADMIN}/memberships", json={"institution_id": inst["id"], "year": 2024, "fee_lkr": -1}
    ).status_code == 422

    patched = client.patch(
        f"{ADMIN}/memberships/{created['id']}", json={"paid_on": "2025-03-01", "fee_lkr": "2500.5"}
    )
    assert patched.status_code == 200
    assert patched.json()["paid_on"] == "2025-03-01"
    assert patched.json()["fee_lkr"] == "2500.50"
    assert patched.json()["reason"] == "Joined after workshop"

    # NOT NULL columns can't be nulled through PATCH.
    nulled = client.patch(f"{ADMIN}/memberships/{created['id']}", json={"year": None})
    assert nulled.status_code == 422

    _membership(client, inst["id"], 2026)
    other = _institution(client, "Other")
    _membership(client, other["id"], 2026)
    by_year = client.get(f"{ADMIN}/memberships", params={"year": 2026}).json()
    assert len(by_year) == 2
    by_school = client.get(f"{ADMIN}/memberships", params={"institution_id": inst["id"]}).json()
    assert [m["year"] for m in by_school] == [2026, 2025]  # newest first
    assert client.get(f"{ADMIN}/memberships", params={"limit": 101}).status_code == 422

    assert client.delete(f"{ADMIN}/memberships/{created['id']}").status_code == 204
    assert client.get(f"{ADMIN}/memberships/{created['id']}").status_code == 404


def test_only_one_primary_contact_per_school(client: TestClient) -> None:
    inst = _institution(client, "Royal College")
    other = _institution(client, "Other")
    a = client.post(
        f"{ADMIN}/school-contacts", json={"institution_id": inst["id"], "name": "A", "is_primary": True}
    ).json()
    elsewhere = client.post(
        f"{ADMIN}/school-contacts", json={"institution_id": other["id"], "name": "X", "is_primary": True}
    ).json()
    b = client.post(
        f"{ADMIN}/school-contacts", json={"institution_id": inst["id"], "name": "B", "is_primary": True}
    ).json()

    def primary(contact_id: int) -> bool:
        return client.get(f"{ADMIN}/school-contacts/{contact_id}").json()["is_primary"]

    assert (primary(a["id"]), primary(b["id"]), primary(elsewhere["id"])) == (False, True, True)

    client.patch(f"{ADMIN}/school-contacts/{a['id']}", json={"is_primary": True})
    assert (primary(a["id"]), primary(b["id"]), primary(elsewhere["id"])) == (True, False, True)

    assert client.post(
        f"{ADMIN}/school-contacts", json={"institution_id": inst["id"], "name": ""}
    ).status_code == 422


def test_feedback_filters_and_parents(client: TestClient) -> None:
    inst = _institution(client, "Royal College")
    judge = client.post(f"{API}/judges", json={"full_name": "Judge Dredd"}).json()
    client.post(f"{ADMIN}/feedback", json={"institution_id": inst["id"], "content": "About school"})
    client.post(
        f"{ADMIN}/feedback",
        json={"judge_id": judge["id"], "content": "About judge", "sentiment": "negative"},
    )
    assert client.post(f"{ADMIN}/feedback", json={"judge_id": 9999, "content": "x"}).status_code == 404
    assert client.post(f"{ADMIN}/feedback", json={"content": ""}).status_code == 422

    about_judge = client.get(f"{ADMIN}/feedback", params={"judge_id": judge["id"]}).json()
    assert [f["content"] for f in about_judge] == ["About judge"]
    open_items = client.get(f"{ADMIN}/feedback", params={"status": "open"}).json()
    assert len(open_items) == 2


# --- delete guard and merges ---------------------------------------------------------


def test_institution_delete_blocked_by_school_records(client: TestClient) -> None:
    inst = _institution(client, "Royal College")
    membership = _membership(client, inst["id"], 2025)
    lead = client.post(f"{ADMIN}/outreach", json={"institution_id": inst["id"]}).json()

    blocked = client.delete(f"{API}/institutions/{inst['id']}")
    assert blocked.status_code == 409
    assert "1 membership(s)" in blocked.json()["detail"]
    assert "team(s)" not in blocked.json()["detail"]

    client.delete(f"{ADMIN}/memberships/{membership['id']}")
    assert client.delete(f"{API}/institutions/{inst['id']}").status_code == 204
    # The outreach lead survives, unlinked.
    assert client.get(f"{ADMIN}/outreach/{lead['id']}").json()["institution_id"] is None


def test_institution_merge_moves_school_records(client: TestClient) -> None:
    target = _institution(client, "Royal College")
    source = _institution(client, "Royal Collage", kind="school", district="Colombo")
    _membership(client, target["id"], 2024)
    moved = _membership(client, source["id"], 2025)
    contact = client.post(
        f"{ADMIN}/school-contacts", json={"institution_id": source["id"], "name": "Ms Perera"}
    ).json()
    feedback = client.post(
        f"{ADMIN}/feedback", json={"institution_id": source["id"], "content": "Hi"}
    ).json()

    resp = client.post(
        f"{API}/institutions/merge", json={"target_id": target["id"], "source_ids": [source["id"]]}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["kind"] == "school" and resp.json()["district"] == "Colombo"

    assert client.get(f"{ADMIN}/memberships/{moved['id']}").json()["institution_id"] == target["id"]
    assert client.get(f"{ADMIN}/school-contacts/{contact['id']}").json()["institution_id"] == target["id"]
    assert client.get(f"{ADMIN}/feedback/{feedback['id']}").json()["institution_id"] == target["id"]
    assert _summary(client, target["id"], 2025)["standing"]["member_years"] == [2024, 2025]


def test_institution_merge_refuses_overlapping_membership_years(client: TestClient) -> None:
    target = _institution(client, "Royal College")
    source = _institution(client, "Royal Collage")
    _membership(client, target["id"], 2025)
    _membership(client, source["id"], 2025)

    resp = client.post(
        f"{API}/institutions/merge", json={"target_id": target["id"], "source_ids": [source["id"]]}
    )
    assert resp.status_code == 409
    assert "2025" in resp.json()["detail"]
    # Nothing moved.
    assert client.get(f"{API}/institutions/{source['id']}").status_code == 200


def test_judge_merge_moves_feedback(client: TestClient) -> None:
    target = client.post(f"{API}/judges", json={"full_name": "Jane Judge"}).json()
    source = client.post(f"{API}/judges", json={"full_name": "Jane  Judge"}).json()
    feedback = client.post(f"{ADMIN}/feedback", json={"judge_id": source["id"], "content": "Fair"}).json()

    resp = client.post(f"{API}/judges/merge", json={"source_id": source["id"], "target_id": target["id"]})
    assert resp.status_code == 200, resp.text
    assert client.get(f"{ADMIN}/feedback/{feedback['id']}").json()["judge_id"] == target["id"]


def test_deleting_tournament_unlinks_feedback_and_achievements(
    client: TestClient, session: Session
) -> None:
    inst = _institution(client, "Royal College")
    tournament = client.post(f"{API}/tournaments", json={"name": "Cup", "slug": "cup"}).json()
    feedback = client.post(
        f"{ADMIN}/feedback", json={"tournament_id": tournament["id"], "content": "Ran late"}
    ).json()
    achievement = client.post(
        f"{ADMIN}/school-achievements",
        json={"institution_id": inst["id"], "year": 2025, "title": "Finalists", "tournament_id": tournament["id"]},
    ).json()

    assert client.delete(f"{API}/tournaments/{tournament['id']}").status_code == 204
    session.expire_all()
    assert session.exec(select(Feedback)).one().tournament_id is None
    assert client.get(f"{ADMIN}/feedback/{feedback['id']}").json()["tournament_id"] is None
    assert client.get(f"{ADMIN}/school-achievements/{achievement['id']}").json()["tournament_id"] is None
