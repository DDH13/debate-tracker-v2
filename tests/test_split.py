"""Split by tournament: move one tournament's rows off a debater, judge or school."""

from fastapi.testclient import TestClient
from sqlmodel import Session, func, select

from app.models import (
    Ballot,
    DebateJudge,
    Debater,
    DebaterProfile,
    Feedback,
    Institution,
    Judge,
    SpeakerScore,
    Team,
    TeamMember,
    TournamentRegistration,
)
from tests.helpers import build_tournament, count_queries

ADMIN = "/api/v1/admin"


def _two_tournaments(client: TestClient) -> tuple[dict, dict]:
    a = build_tournament(client, teams=4, prelim_rounds=2, elim_rounds=0, slug="cup-a")
    b = build_tournament(client, teams=4, prelim_rounds=2, elim_rounds=0, slug="cup-b")
    return a, b


def _speeches(session: Session, debater_id: int) -> int:
    return session.exec(select(func.count(SpeakerScore.id)).where(SpeakerScore.debater_id == debater_id)).one()


def _merged_debater(client: TestClient, session: Session) -> tuple[int, int, dict, dict]:
    """One debater wrongly holding team places at two tournaments (made by a merge)."""
    a, b = _two_tournaments(client)
    keep, gone = a["teams"][0]["debaters"][0]["id"], b["teams"][0]["debaters"][0]["id"]
    assert client.post("/api/v1/debaters/merge", json={"source_id": gone, "target_id": keep}).status_code == 200
    return keep, gone, a, b


# --- Debaters --------------------------------------------------------------------------------


def test_split_info_lists_each_tournament_with_counts(client: TestClient, session: Session) -> None:
    keep, _, a, b = _merged_debater(client, session)
    resp = client.get(f"{ADMIN}/debaters/{keep}/split-info")
    assert resp.status_code == 200, resp.text
    rows = {r["tournament_id"]: r["counts"] for r in resp.json()}
    assert set(rows) == {a["tournament"]["id"], b["tournament"]["id"]}
    assert rows[b["tournament"]["id"]]["teams"] == 1
    assert rows[b["tournament"]["id"]]["speeches"] == 2 * 3 * 2  # two rounds, three ballots, a speech and the reply


def test_merge_then_split_restores_two_debaters(client: TestClient, session: Session) -> None:
    keep, _, a, b = _merged_debater(client, session)
    session.add(DebaterProfile(debater_id=keep))
    session.commit()
    keep_row = session.get(Debater, keep)
    keep_row.email = "keep@example.com"
    session.add(keep_row)
    session.commit()
    before = _speeches(session, keep)

    preview = client.post(f"{ADMIN}/debaters/{keep}/split", json={"tournament_ids": [b["tournament"]["id"]], "dry_run": True})
    assert preview.status_code == 200, preview.text
    assert preview.json()["target_id"] is None and preview.json()["created"]
    session.expire_all()
    assert _speeches(session, keep) == before  # the dry run changed nothing

    resp = client.post(f"{ADMIN}/debaters/{keep}/split", json={"tournament_ids": [b["tournament"]["id"]]})
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["moved"] == preview.json()["moved"] == {"tournaments": 1, "teams": 1, "speeches": 12}
    new_id = result["target_id"]

    session.expire_all()
    new = session.get(Debater, new_id)
    assert new.full_name == keep_row.full_name and new.email is None  # name copied, personal fields not
    assert _speeches(session, keep) == before - 12 and _speeches(session, new_id) == 12
    [member] = session.exec(select(TeamMember).where(TeamMember.debater_id == new_id)).all()
    assert member.team_id == b["teams"][0]["team"]["id"]
    assert session.exec(select(DebaterProfile).where(DebaterProfile.debater_id == keep)).first() is None


def test_split_debater_into_an_existing_record(client: TestClient, session: Session) -> None:
    keep, _, a, b = _merged_debater(client, session)
    other = client.post("/api/v1/debaters", json={"full_name": "The Right One"}).json()["id"]
    resp = client.post(
        f"{ADMIN}/debaters/{keep}/split",
        json={"tournament_ids": [b["tournament"]["id"]], "target_id": other},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["created"] is False and resp.json()["target_name"] == "The Right One"
    assert _speeches(session, other) == 12

    # Its teammate spoke in the same debates: moving the other tournament onto them is refused.
    teammate = a["teams"][0]["debaters"][1]["id"]
    clash = client.post(
        f"{ADMIN}/debaters/{keep}/split", json={"tournament_ids": [a["tournament"]["id"]], "target_id": teammate}
    )
    assert clash.status_code == 409


def test_split_rejects_bad_requests(client: TestClient, session: Session) -> None:
    keep, _, a, b = _merged_debater(client, session)
    url = f"{ADMIN}/debaters/{keep}/split"
    both = [a["tournament"]["id"], b["tournament"]["id"]]
    assert client.post(url, json={"tournament_ids": []}).status_code == 422
    assert client.post(url, json={"tournament_ids": both}).status_code == 422  # nothing left
    assert client.post(url, json={"tournament_ids": [9999]}).status_code == 422
    assert client.post(url, json={"tournament_ids": both[:1], "target_id": keep}).status_code == 422
    assert client.post(url, json={"tournament_ids": both[:1], "target_id": 9999}).status_code == 404
    assert client.post(f"{ADMIN}/debaters/9999/split", json={"tournament_ids": both[:1]}).status_code == 404
    # Moving everything onto an existing record is allowed (it's a reassignment).
    other = client.post("/api/v1/debaters", json={"full_name": "Someone"}).json()["id"]
    assert client.post(url, json={"tournament_ids": both, "target_id": other}).status_code == 200


def test_split_info_query_count_is_constant(client: TestClient, session: Session) -> None:
    small = build_tournament(client, teams=4, prelim_rounds=2, elim_rounds=0, slug="small")
    large = build_tournament(client, teams=8, prelim_rounds=4, elim_rounds=0, slug="large")
    counts = []
    for data in (small, large):
        judge = data["judges"][0]["id"]
        session.expire_all()
        with count_queries(session) as statements:
            assert client.get(f"{ADMIN}/judges/{judge}/split-info").status_code == 200
        counts.append(len(statements))
    assert counts[0] == counts[1]


# --- Judges ----------------------------------------------------------------------------------


def test_split_judge_moves_panels_ballots_and_feedback(client: TestClient, session: Session) -> None:
    a, b = _two_tournaments(client)
    keep, gone = a["judges"][0]["id"], b["judges"][0]["id"]
    assert client.post("/api/v1/judges/merge", json={"source_id": gone, "target_id": keep}).status_code == 200
    session.add(Feedback(judge_id=keep, tournament_id=b["tournament"]["id"], content="Fine"))
    session.commit()

    resp = client.post(
        f"{ADMIN}/judges/{keep}/split",
        json={"tournament_ids": [b["tournament"]["id"]], "new": {"full_name": "Judge Zero (Cup B)"}},
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["moved"] == {"tournaments": 1, "panels": 4, "ballots": 4, "feedback": 1}
    new_id = result["target_id"]
    session.expire_all()
    new_judge = session.get(Judge, new_id)
    assert (new_judge.full_name, new_judge.first_name) == ("Judge Zero (Cup B)", "Judge")
    assert session.exec(select(func.count(DebateJudge.id)).where(DebateJudge.judge_id == keep)).one() == 4
    assert session.exec(select(func.count(Ballot.id)).where(Ballot.judge_id == new_id)).one() == 4

    # Another judge from cup A sat on the same panels: moving cup A onto them is refused.
    colleague = a["judges"][1]["id"]
    clash = client.post(
        f"{ADMIN}/judges/{keep}/split", json={"tournament_ids": [a["tournament"]["id"]], "target_id": colleague}
    )
    assert clash.status_code == 409


# --- Institutions ----------------------------------------------------------------------------


def _school_at_two_tournaments(client: TestClient, session: Session) -> tuple[Institution, dict, dict]:
    a, b = _two_tournaments(client)
    school = Institution(name="Nalanda College")
    session.add(school)
    session.flush()
    for data in (a, b):
        team = session.get(Team, data["teams"][0]["team"]["id"])
        team.institution_id = school.id
        session.add(team)
    # Cup B's speakers and one of its judges were made with this school by the bad match.
    for d in b["teams"][0]["debaters"]:
        row = session.get(Debater, d["id"])
        row.institution_id = school.id
        session.add(row)
    judge = session.get(Judge, b["judges"][0]["id"])
    judge.institution_id = school.id
    a_speaker = session.get(Debater, a["teams"][0]["debaters"][0]["id"])
    a_speaker.institution_id = school.id
    session.add_all([judge, a_speaker])
    session.add(TournamentRegistration(tournament_id=b["tournament"]["id"], institution_id=school.id))
    session.commit()
    return school, a, b


def test_split_institution_into_a_new_school(client: TestClient, session: Session) -> None:
    school, a, b = _school_at_two_tournaments(client, session)
    body = {"tournament_ids": [b["tournament"]["id"]], "new": {"name": "Ananda College"}}

    preview = client.post(f"{ADMIN}/institutions/{school.id}/split", json={**body, "dry_run": True})
    assert preview.status_code == 200, preview.text
    assert len(preview.json()["school_moves"]) == 4  # cup B's three speakers and its judge
    session.expire_all()
    assert session.exec(select(Institution).where(Institution.name == "Ananda College")).first() is None

    resp = client.post(f"{ADMIN}/institutions/{school.id}/split", json=body)
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["moved"]["teams"] == 1 and result["moved"]["registrations"] == 1
    new_id = result["target_id"]
    session.expire_all()
    assert session.get(Team, b["teams"][0]["team"]["id"]).institution_id == new_id
    assert session.get(Team, a["teams"][0]["team"]["id"]).institution_id == school.id
    assert all(session.get(Debater, d["id"]).institution_id == new_id for d in b["teams"][0]["debaters"])
    assert session.get(Debater, a["teams"][0]["debaters"][0]["id"]).institution_id == school.id
    assert session.get(Judge, b["judges"][0]["id"]).institution_id == new_id

    again = client.post(f"{ADMIN}/institutions/{new_id}/split", json={"tournament_ids": [b["tournament"]["id"]], "new": {"name": "Nalanda College"}})
    assert again.status_code == 422  # every tournament of the new school: nothing left


def test_split_institution_conflicts(client: TestClient, session: Session) -> None:
    school, a, b = _school_at_two_tournaments(client, session)
    url = f"{ADMIN}/institutions/{school.id}/split"
    other = Institution(name="Ananda College")
    session.add(other)
    session.flush()
    session.add(TournamentRegistration(tournament_id=b["tournament"]["id"], institution_id=other.id))
    session.commit()
    tids = [b["tournament"]["id"]]
    assert client.post(url, json={"tournament_ids": tids, "target_id": other.id}).status_code == 409
    assert client.post(url, json={"tournament_ids": tids, "new": {"name": "Ananda College"}}).status_code == 409
    assert client.post(url, json={"tournament_ids": tids}).status_code == 422  # no name for the new school
