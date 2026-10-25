from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.models import (
    Debate,
    DebateJudge,
    Institution,
    Judge,
    JudgeAffiliation,
    JudgeRecord,
    Round,
    Team,
    Tournament,
)

API = "/api/v1"
ADMIN = f"{API}/admin"


@pytest.fixture
def judges(session: Session) -> dict:
    """Ann judged two tournaments (chairing, incl. an elim); Bo one (as a trainee); Cy none."""
    royal = Institution(name="Royal College")
    trinity = Institution(name="Trinity College")
    old = Tournament(name="Old Cup", slug="old-cup", date=date(2025, 3, 1))
    new = Tournament(name="New Cup", slug="new-cup", date=date(2026, 3, 1))
    session.add_all([royal, trinity, old, new])
    session.flush()
    debates = []
    for t in (old, new):
        a, b = Team(name="A", tournament_id=t.id), Team(name="B", tournament_id=t.id)
        r1, final = Round(seq=1, tournament_id=t.id), Round(seq=2, tournament_id=t.id, isElimination=True)
        session.add_all([a, b, r1, final])
        session.flush()
        d1 = Debate(round_id=r1.id, prop_team_id=a.id, opp_team_id=b.id)
        d2 = Debate(round_id=final.id, prop_team_id=a.id, opp_team_id=b.id)
        session.add_all([d1, d2])
        session.flush()
        debates.append((d1, d2))
    ann = Judge(full_name="Ann Adjudicator", institution_id=royal.id)
    bo = Judge(first_name="Bo", last_name="Beginner", institution_id=trinity.id)
    cy = Judge(full_name="Cy Newcomer")
    session.add_all([ann, bo, cy])
    session.flush()
    (old_r1, _), (new_r1, new_final) = debates
    session.add_all([
        DebateJudge(debate_id=old_r1.id, judge_id=ann.id),
        DebateJudge(debate_id=new_r1.id, judge_id=ann.id, is_chair=True),
        DebateJudge(debate_id=new_final.id, judge_id=ann.id, is_chair=True),
        DebateJudge(debate_id=new_r1.id, judge_id=bo.id, is_trainee=True),
    ])
    session.commit()
    return {"ann": ann, "bo": bo, "cy": cy, "royal": royal, "trinity": trinity, "new": new}


def test_registry_involvement_and_filters(client: TestClient, judges) -> None:
    rows = client.get(f"{ADMIN}/judges/registry").json()
    # Most recently active first; never-judged last.
    assert [r["name"] for r in rows] == ["Ann Adjudicator", "Bo Beginner", "Cy Newcomer"]
    ann = rows[0]["involvement"]
    assert (ann["tournaments"], ann["debates"], ann["prelims"], ann["elims"], ann["chaired"]) == (2, 3, 2, 1, 2)
    assert (ann["last_tournament_name"], ann["last_tournament_date"]) == ("New Cup", "2026-03-01")
    assert rows[1]["involvement"]["trainee"] == 1
    assert rows[2]["involvement"]["debates"] == 0

    client.put(f"{ADMIN}/judges/{judges['bo'].id}/record", json={"level": "trainee"})
    names = lambda **params: [r["name"] for r in client.get(f"{ADMIN}/judges/registry", params=params).json()]  # noqa: E731
    assert names(q="bo b") == ["Bo Beginner"]
    assert names(level="trainee") == ["Bo Beginner"]
    assert names(institution_id=judges["royal"].id) == ["Ann Adjudicator"]


def test_record_affiliations_and_detail(client: TestClient, judges) -> None:
    ann = judges["ann"]
    resp = client.put(f"{ADMIN}/judges/{ann.id}/record", json={"level": "chair", "experience_notes": "Broke at nationals"})
    assert resp.status_code == 200
    resp = client.put(f"{ADMIN}/judges/{ann.id}/record", json={"level": "adjudication_core"})
    assert (resp.json()["level"], resp.json()["experience_notes"]) == ("adjudication_core", None)  # a full replace
    assert client.put(f"{ADMIN}/judges/999/record", json={}).status_code == 404

    base = {"judge_id": ann.id, "institution_id": judges["royal"].id}
    assert client.post(f"{ADMIN}/judge-affiliations", json={**base, "role": "alumnus", "from_year": 2019, "to_year": 2015}).status_code == 422
    client.post(f"{ADMIN}/judge-affiliations", json={**base, "role": "alumnus", "from_year": 2012, "to_year": 2016})
    client.post(f"{ADMIN}/judge-affiliations", json={"judge_id": ann.id, "institution_id": judges["trinity"].id, "role": "coach", "from_year": 2022})
    client.post(f"{ADMIN}/feedback", json={"content": "Clear oral adjudications", "judge_id": ann.id})

    detail = client.get(f"{ADMIN}/judges/{ann.id}/detail").json()
    assert (detail["name"], detail["institution_name"], detail["record"]["level"]) == ("Ann Adjudicator", "Royal College", "adjudication_core")
    assert [(a["institution_name"], a["role"]) for a in detail["affiliations"]] == [("Trinity College", "coach"), ("Royal College", "alumnus")]
    assert [(h["name"], h["debates"], h["chaired"], h["elims"]) for h in detail["history"]] == [("New Cup", 2, 2, 1), ("Old Cup", 1, 0, 0)]
    assert detail["profile"] is None
    assert [f["content"] for f in detail["feedback"]] == ["Clear oral adjudications"]

    school = client.get(f"{ADMIN}/schools/{judges['trinity'].id}").json()
    assert [(j["name"], j["role"]) for j in school["affiliated_judges"]] == [("Ann Adjudicator", "coach")]


def test_judge_merge_moves_registry_data(client: TestClient, session: Session, judges) -> None:
    ann, cy = judges["ann"], judges["cy"]
    royal = judges["royal"].id
    client.put(f"{ADMIN}/judges/{cy.id}/record", json={"level": "chair", "notes": "From the dupe"})
    client.put(f"{ADMIN}/judges/{ann.id}/record", json={"experience_notes": "Kept"})
    for judge_id in (ann.id, cy.id):
        client.post(f"{ADMIN}/judge-affiliations", json={"judge_id": judge_id, "institution_id": royal, "role": "alumnus", "from_year": 2012})
    client.post(f"{ADMIN}/judge-affiliations", json={"judge_id": cy.id, "institution_id": royal, "role": "coach"})

    resp = client.post(f"{API}/judges/merge", json={"source_id": cy.id, "target_id": ann.id})
    assert resp.status_code == 200, resp.text
    session.expire_all()
    [record] = session.exec(select(JudgeRecord)).all()
    assert (record.judge_id, record.level, record.experience_notes, record.notes) == (ann.id, "chair", "Kept", "From the dupe")
    roles = sorted(a.role.value for a in session.exec(select(JudgeAffiliation)).all())
    assert roles == ["alumnus", "coach"]  # the duplicate alumnus row was dropped
    assert {a.judge_id for a in session.exec(select(JudgeAffiliation)).all()} == {ann.id}


def test_judge_delete_cascades_registry_data(client: TestClient, session: Session, judges) -> None:
    cy = judges["cy"]
    client.put(f"{ADMIN}/judges/{cy.id}/record", json={"level": "panellist"})
    client.post(f"{ADMIN}/judge-affiliations", json={"judge_id": cy.id, "institution_id": judges["royal"].id})
    assert client.delete(f"{API}/judges/{cy.id}").status_code == 204
    session.expire_all()
    assert session.exec(select(JudgeRecord)).all() == []
    assert session.exec(select(JudgeAffiliation)).all() == []
