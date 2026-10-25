from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.models import (
    Debate,
    DebateJudge,
    Institution,
    Judge,
    Round,
    Team,
    Tournament,
    TournamentOps,
    TournamentRegistration,
)

API = "/api/v1"
ADMIN = f"{API}/admin"


@pytest.fixture
def tab(session: Session) -> dict:
    """An imported-style tournament: Royal fields 3 teams, Trinity 1, Ladies' none. Royal's
    judges: two full judges (one of them in the elim too) and a trainee; Trinity's: one."""
    royal, trinity, ladies = Institution(name="Royal College"), Institution(name="Trinity College"), Institution(name="Ladies' College")
    cup = Tournament(name="Schools Cup", slug="schools-cup", date=date(2026, 5, 1))
    session.add_all([royal, trinity, ladies, cup])
    session.flush()
    teams = [Team(name=f"Royal {c}", tournament_id=cup.id, institution_id=royal.id) for c in "ABC"]
    teams.append(Team(name="Trinity A", tournament_id=cup.id, institution_id=trinity.id))
    r1 = Round(seq=1, tournament_id=cup.id)
    final = Round(seq=2, tournament_id=cup.id, isElimination=True)
    session.add_all([*teams, r1, final])
    session.flush()
    d1 = Debate(round_id=r1.id, prop_team_id=teams[0].id, opp_team_id=teams[3].id)
    d2 = Debate(round_id=r1.id, prop_team_id=teams[1].id, opp_team_id=teams[2].id)
    d3 = Debate(round_id=final.id, prop_team_id=teams[0].id, opp_team_id=teams[3].id)
    judges = {
        "ann": Judge(full_name="Ann Royal", institution_id=royal.id),
        "bo": Judge(full_name="Bo Royal", institution_id=royal.id),
        "cy": Judge(full_name="Cy Trainee", institution_id=royal.id),
        "di": Judge(full_name="Di Trinity", institution_id=trinity.id),
        "ed": Judge(full_name="Ed Independent"),
    }
    session.add_all([d1, d2, d3, *judges.values()])
    session.flush()
    session.add_all([
        DebateJudge(debate_id=d1.id, judge_id=judges["ann"].id, is_chair=True),
        DebateJudge(debate_id=d1.id, judge_id=judges["cy"].id, is_trainee=True),
        DebateJudge(debate_id=d2.id, judge_id=judges["di"].id, is_chair=True),
        DebateJudge(debate_id=d2.id, judge_id=judges["ed"].id),
        DebateJudge(debate_id=d3.id, judge_id=judges["ann"].id, is_chair=True),
        DebateJudge(debate_id=d3.id, judge_id=judges["bo"].id),
    ])
    session.commit()
    return {"cup": cup, "royal": royal, "trinity": trinity, "ladies": ladies, "judges": judges}


def _register(client: TestClient, tournament_id: int, institution_id: int, expected: int = 201, **fields) -> dict:
    resp = client.post(
        f"{ADMIN}/tournament-registrations",
        json={"tournament_id": tournament_id, "institution_id": institution_id, **fields},
    )
    assert resp.status_code == expected, resp.text
    return resp.json()


def _ops(client: TestClient, tournament_id: int) -> dict:
    resp = client.get(f"{ADMIN}/tournaments/{tournament_id}/ops")
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_derived_counts_dropouts_and_shortfalls(client: TestClient, tab) -> None:
    cup = tab["cup"]
    _register(client, cup.id, tab["royal"].id, teams_registered=4, judges_promised=2)
    _register(client, cup.id, tab["ladies"].id, teams_registered=2)
    _register(client, cup.id, tab["royal"].id, expected=409)

    ops = _ops(client, cup.id)
    assert ops["tab_imported"] is True
    assert ops["settings"] == {"teams_per_judge": 2, "hosted_by_dc": False, "notes": None}
    rows = {r["name"]: r for r in ops["rows"]}
    assert list(rows) == ["Ladies' College", "Royal College", "Trinity College"]

    royal = rows["Royal College"]
    assert (royal["teams_registered"], royal["teams_fielded"], royal["dropouts"]) == (4, 3, 1)
    # 3 teams at 1 judge per 2 → 2; the trainee doesn't count towards the quota.
    assert (royal["judges_required"], royal["judges_provided"], royal["shortfall"]) == (2, 2, 0)
    assert royal["judges_promised"] == 2

    # Imported teams but no registration: still listed.
    trinity = rows["Trinity College"]
    assert trinity["registration_id"] is None
    assert (trinity["teams_fielded"], trinity["dropouts"], trinity["judges_required"], trinity["judges_provided"]) == (1, None, 1, 1)

    # Registered, fielded nobody: a full dropout, and no teams means no quota.
    ladies = rows["Ladies' College"]
    assert (ladies["teams_fielded"], ladies["dropouts"], ladies["judges_required"], ladies["shortfall"]) == (0, 2, 0, 0)

    assert ops["totals"] == {
        "teams_registered": 6, "teams_fielded": 4, "dropouts": 3,
        "judges_required": 3, "judges_provided": 3, "shortfall": 0,
    }


def test_rule_and_overrides(client: TestClient, tab) -> None:
    cup = tab["cup"]
    reg = _register(client, cup.id, tab["royal"].id, teams_registered=3)
    resp = client.put(f"{ADMIN}/tournaments/{cup.id}/ops-settings", json={"teams_per_judge": 1, "hosted_by_dc": True})
    assert resp.status_code == 200
    assert client.put(f"{ADMIN}/tournaments/{cup.id}/ops-settings", json={"teams_per_judge": 0}).status_code == 422

    royal = next(r for r in _ops(client, cup.id)["rows"] if r["name"] == "Royal College")
    assert (royal["judges_required"], royal["shortfall"]) == (3, 1)  # 1 per team now

    client.patch(f"{ADMIN}/tournament-registrations/{reg['id']}", json={
        "teams_fielded_override": 2, "judges_provided_override": 1, "judges_required_override": 4,
    })
    royal = next(r for r in _ops(client, cup.id)["rows"] if r["name"] == "Royal College")
    assert (royal["teams_fielded"], royal["teams_fielded_from_tab"], royal["fielded_overridden"]) == (2, 3, True)
    assert (royal["judges_provided"], royal["judges_provided_from_tab"], royal["provided_overridden"]) == (1, 2, True)
    assert (royal["judges_required"], royal["required_overridden"], royal["shortfall"]) == (4, True, 3)
    assert royal["dropouts"] == 1


def test_tournament_without_a_tab(client: TestClient, session: Session, tab) -> None:
    empty = Tournament(name="Future Open", slug="future-open")
    session.add(empty)
    session.commit()
    _register(client, empty.id, tab["royal"].id, teams_registered=3)
    ops = _ops(client, empty.id)
    assert ops["tab_imported"] is False
    [row] = ops["rows"]
    # Nothing imported yet: fielded / provided are unknown, not zero, so nothing is invented.
    assert (row["teams_fielded"], row["dropouts"], row["judges_provided"], row["shortfall"]) == (None, None, None, None)
    assert row["judges_required"] == 2  # from the registered teams


def test_summary_school_record_and_feedback(client: TestClient, tab) -> None:
    cup = tab["cup"]
    _register(client, cup.id, tab["royal"].id, teams_registered=4)
    client.post(f"{ADMIN}/feedback", json={"content": "Rooms ran late", "tournament_id": cup.id})
    client.put(f"{ADMIN}/tournaments/{cup.id}/ops-settings", json={"teams_per_judge": 1, "hosted_by_dc": True})

    [summary] = client.get(f"{ADMIN}/tournament-ops").json()
    assert (summary["schools"], summary["registrations"], summary["dropouts"], summary["shortfall"]) == (2, 1, 1, 1)
    assert summary["hosted_by_dc"] is True
    assert [f["content"] for f in _ops(client, cup.id)["feedback"]] == ["Rooms ran late"]

    record = client.get(f"{ADMIN}/schools/{tab['royal'].id}").json()["tournament_record"]
    assert [(r["tournament_name"], r["dropouts"], r["shortfall"]) for r in record] == [("Schools Cup", 1, 1)]


def test_tournament_delete_cascades(client: TestClient, session: Session) -> None:
    school = client.post(f"{API}/institutions", json={"name": "Royal College"}).json()
    cup = client.post(f"{API}/tournaments", json={"name": "Cup", "slug": "cup"}).json()
    _register(client, cup["id"], school["id"], teams_registered=1)
    client.put(f"{ADMIN}/tournaments/{cup['id']}/ops-settings", json={"teams_per_judge": 3})
    assert client.delete(f"{API}/tournaments/{cup['id']}").status_code == 204
    session.expire_all()
    assert session.exec(select(TournamentRegistration)).all() == []
    assert session.exec(select(TournamentOps)).all() == []


def test_institution_merge_and_delete_guard(client: TestClient) -> None:
    keep = client.post(f"{API}/institutions", json={"name": "Royal College"}).json()
    dupe = client.post(f"{API}/institutions", json={"name": "Royal College Colombo"}).json()
    cup = client.post(f"{API}/tournaments", json={"name": "Cup", "slug": "cup"}).json()
    other = client.post(f"{API}/tournaments", json={"name": "Open", "slug": "open"}).json()
    _register(client, cup["id"], keep["id"], teams_registered=1)
    reg = _register(client, cup["id"], dupe["id"], teams_registered=2)

    resp = client.post(f"{API}/institutions/merge", json={"target_id": keep["id"], "source_ids": [dupe["id"]]})
    assert resp.status_code == 409
    assert "both registered" in resp.json()["detail"]

    client.delete(f"{ADMIN}/tournament-registrations/{reg['id']}")
    moved = _register(client, other["id"], dupe["id"], teams_registered=2)
    resp = client.post(f"{API}/institutions/merge", json={"target_id": keep["id"], "source_ids": [dupe["id"]]})
    assert resp.status_code == 200, resp.text
    assert client.get(f"{ADMIN}/tournament-registrations/{moved['id']}").json()["institution_id"] == keep["id"]

    resp = client.delete(f"{API}/institutions/{keep['id']}")
    assert resp.status_code == 409
    assert "tournament registration" in resp.json()["detail"]
