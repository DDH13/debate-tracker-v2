from fastapi.testclient import TestClient
from sqlmodel import Session, func, select

from app.models import (
    Ballot,
    DebateJudge,
    Debater,
    DebaterProfile,
    Judge,
    JudgeProfile,
    SpeakerScore,
    TeamMember,
)
from tests.helpers import _build_debate_scaffold, _full_score_sheet

API = "/api/v1"


def _post_ballot(client: TestClient, debate_id: int, judge_id: int, scores: list[dict]) -> dict:
    resp = client.post(
        f"{API}/debates/{debate_id}/ballots",
        json={"judge_id": judge_id, "winner": "prop", "scores": scores},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _second_debate(client: TestClient, scaffold: dict, *, prop_team_id: int | None = None) -> dict:
    """A round-2 debate in the scaffold's tournament; returns the debate."""
    round2 = client.post(
        f"{API}/tournaments/{scaffold['tournament']['id']}/rounds", json={"seq": 2}
    ).json()
    return client.post(
        f"{API}/rounds/{round2['id']}/debates",
        json={
            "prop_team_id": prop_team_id or scaffold["prop_team"]["id"],
            "opp_team_id": scaffold["opp_team"]["id"],
        },
    ).json()


def _new_debater(client: TestClient, **fields) -> dict:
    resp = client.post(f"{API}/debaters", json=fields)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _new_judge(client: TestClient, **fields) -> dict:
    resp = client.post(f"{API}/judges", json=fields)
    assert resp.status_code == 201, resp.text
    return resp.json()


# --- debater merge ---


def test_merge_debaters_moves_scores_and_teams_and_fills_null_fields(
    client: TestClient, session: Session
) -> None:
    scaffold = _build_debate_scaffold(client)
    prop, opp = scaffold["prop_debaters"], scaffold["opp_debaters"]
    institution = client.post(f"{API}/institutions", json={"name": "Merge U"}).json()

    target = prop[0]
    source = _new_debater(
        client,
        full_name="Prop Speaker 1 (typo)",
        email="p1@example.com",
        phone="0771234567",
        gender="female",
        birthdate="2000-01-02",
        institution_id=institution["id"],
    )

    # The source speaks in round 2 for a team of its own, so it has scores and a membership
    # the target lacks.
    other_team = client.post(
        f"{API}/tournaments/{scaffold['tournament']['id']}/teams", json={"name": "Other Team"}
    ).json()
    extras = [_new_debater(client, full_name=f"Other {i}") for i in range(2)]
    for member in (source, *extras):
        client.post(f"{API}/teams/{other_team['id']}/members", json={"debater_id": member["id"]})
    debate2 = _second_debate(client, scaffold, prop_team_id=other_team["id"])
    for judge in scaffold["judges"]:
        client.post(f"{API}/debates/{debate2['id']}/judges", json={"judge_id": judge["id"]})
    _post_ballot(
        client,
        debate2["id"],
        scaffold["judges"][0]["id"],
        _full_score_sheet([source, *extras], opp),
    )
    # Target also has a score of its own in the original debate.
    _post_ballot(
        client, scaffold["debate"]["id"], scaffold["judges"][0]["id"], _full_score_sheet(prop, opp)
    )
    # Source is also (redundantly) on the target's team: that membership must collapse.
    client.post(
        f"{API}/teams/{scaffold['prop_team']['id']}/members", json={"debater_id": source["id"]}
    )
    assert client.post(f"{API}/stats/refresh").status_code == 200
    assert session.exec(
        select(func.count()).select_from(DebaterProfile).where(DebaterProfile.debater_id == source["id"])
    ).one() == 1

    resp = client.post(f"{API}/debaters/merge", json={"source_id": source["id"], "target_id": target["id"]})
    assert resp.status_code == 200, resp.text
    merged = resp.json()

    assert merged["id"] == target["id"]
    assert merged["full_name"] == "Prop Speaker 1"  # target's own value wins
    assert merged["email"] == "p1@example.com"
    assert merged["phone"] == "0771234567"
    assert merged["gender"] == "female"
    assert merged["birthdate"] == "2000-01-02"
    assert merged["institution_id"] == institution["id"]

    assert client.get(f"{API}/debaters/{source['id']}").status_code == 404
    session.expire_all()
    assert session.exec(select(Debater).where(Debater.id == source["id"])).first() is None
    assert session.exec(
        select(func.count()).select_from(DebaterProfile).where(DebaterProfile.debater_id == source["id"])
    ).one() == 0

    scored_debates = {
        row
        for row in session.exec(
            select(Ballot.debate_id)
            .join(SpeakerScore, SpeakerScore.ballot_id == Ballot.id)
            .where(SpeakerScore.debater_id == target["id"])
        ).all()
    }
    assert scored_debates == {scaffold["debate"]["id"], debate2["id"]}
    assert session.exec(
        select(func.count()).select_from(SpeakerScore).where(SpeakerScore.debater_id == source["id"])
    ).one() == 0

    team_ids = sorted(
        session.exec(select(TeamMember.team_id).where(TeamMember.debater_id == target["id"])).all()
    )
    assert team_ids == sorted([scaffold["prop_team"]["id"], other_team["id"]])  # no duplicate row


def test_merge_debaters_co_scoring_on_same_ballot_is_409(client: TestClient, session: Session) -> None:
    scaffold = _build_debate_scaffold(client)
    prop, opp = scaffold["prop_debaters"], scaffold["opp_debaters"]
    _post_ballot(
        client, scaffold["debate"]["id"], scaffold["judges"][0]["id"], _full_score_sheet(prop, opp)
    )

    resp = client.post(
        f"{API}/debaters/merge", json={"source_id": prop[1]["id"], "target_id": prop[0]["id"]}
    )
    assert resp.status_code == 409
    # Nothing moved.
    assert client.get(f"{API}/debaters/{prop[1]['id']}").status_code == 200


def test_merge_debaters_validation_errors(client: TestClient) -> None:
    debater = _new_debater(client, full_name="Solo")
    assert (
        client.post(f"{API}/debaters/merge", json={"source_id": debater["id"], "target_id": debater["id"]}).status_code
        == 422
    )
    assert (
        client.post(f"{API}/debaters/merge", json={"source_id": 9999, "target_id": debater["id"]}).status_code
        == 404
    )
    assert (
        client.post(f"{API}/debaters/merge", json={"source_id": debater["id"], "target_id": 9999}).status_code
        == 404
    )


# --- judge merge ---


def test_merge_judges_moves_ballots_and_panels(client: TestClient, session: Session) -> None:
    scaffold = _build_debate_scaffold(client)
    prop, opp = scaffold["prop_debaters"], scaffold["opp_debaters"]
    target = scaffold["judges"][0]
    source = _new_judge(client, full_name="Judge Typo", email="j@example.com", gender="male")

    _post_ballot(client, scaffold["debate"]["id"], target["id"], _full_score_sheet(prop, opp))
    debate2 = _second_debate(client, scaffold)
    client.post(f"{API}/debates/{debate2['id']}/judges", json={"judge_id": source["id"], "is_chair": True})
    _post_ballot(client, debate2["id"], source["id"], _full_score_sheet(prop, opp))
    assert client.post(f"{API}/stats/refresh").status_code == 200

    resp = client.post(f"{API}/judges/merge", json={"source_id": source["id"], "target_id": target["id"]})
    assert resp.status_code == 200, resp.text
    merged = resp.json()
    assert merged["id"] == target["id"]
    assert merged["full_name"] == "Judge 1"
    assert merged["email"] == "j@example.com"
    assert merged["gender"] == "male"

    session.expire_all()
    assert session.get(Judge, source["id"]) is None
    assert session.exec(
        select(func.count()).select_from(JudgeProfile).where(JudgeProfile.judge_id == source["id"])
    ).one() == 0
    assert sorted(session.exec(select(Ballot.debate_id).where(Ballot.judge_id == target["id"])).all()) == sorted(
        [scaffold["debate"]["id"], debate2["id"]]
    )
    chair_link = session.exec(
        select(DebateJudge).where(DebateJudge.debate_id == debate2["id"], DebateJudge.judge_id == target["id"])
    ).one()
    assert chair_link.is_chair is True


def test_merge_judges_sharing_a_panel_is_409(client: TestClient) -> None:
    scaffold = _build_debate_scaffold(client)
    first, second = scaffold["judges"][0], scaffold["judges"][1]
    resp = client.post(f"{API}/judges/merge", json={"source_id": second["id"], "target_id": first["id"]})
    assert resp.status_code == 409
    assert client.get(f"{API}/judges/{second['id']}").status_code == 200


def test_merge_judges_validation_errors(client: TestClient) -> None:
    judge = _new_judge(client, full_name="Solo")
    assert (
        client.post(f"{API}/judges/merge", json={"source_id": judge["id"], "target_id": judge["id"]}).status_code
        == 422
    )
    assert (
        client.post(f"{API}/judges/merge", json={"source_id": 9999, "target_id": judge["id"]}).status_code == 404
    )


# --- institution merge ---


def test_merge_institutions_reassigns_everything_and_copies_code(
    client: TestClient, session: Session
) -> None:
    target = client.post(f"{API}/institutions", json={"name": "Target U"}).json()
    source_a = client.post(f"{API}/institutions", json={"name": "Source A", "code": "SA"}).json()
    source_b = client.post(f"{API}/institutions", json={"name": "Source B", "code": "SB"}).json()
    tournament = client.post(f"{API}/tournaments", json={"name": "T", "slug": "t"}).json()

    team = client.post(
        f"{API}/tournaments/{tournament['id']}/teams",
        json={"name": "Team A", "institution_id": source_a["id"]},
    ).json()
    debater = _new_debater(client, full_name="D", institution_id=source_b["id"])
    judge = _new_judge(client, full_name="J", institution_id=source_a["id"])

    resp = client.post(
        f"{API}/institutions/merge",
        json={"target_id": target["id"], "source_ids": [source_a["id"], source_b["id"]]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {  # first non-null code
        "id": target["id"],
        "name": "Target U",
        "code": "SA",
        "kind": None,
        "district": None,
    }

    assert client.get(f"{API}/teams/{team['id']}").json()["institution_id"] == target["id"]
    assert client.get(f"{API}/debaters/{debater['id']}").json()["institution_id"] == target["id"]
    assert client.get(f"{API}/judges/{judge['id']}").json()["institution_id"] == target["id"]
    assert client.get(f"{API}/institutions/{source_a['id']}").status_code == 404
    assert client.get(f"{API}/institutions/{source_b['id']}").status_code == 404


def test_merge_institutions_keeps_existing_target_code_and_validates(client: TestClient) -> None:
    target = client.post(f"{API}/institutions", json={"name": "Target U", "code": "TU"}).json()
    source = client.post(f"{API}/institutions", json={"name": "Source", "code": "SS"}).json()

    assert (
        client.post(
            f"{API}/institutions/merge", json={"target_id": target["id"], "source_ids": [target["id"]]}
        ).status_code
        == 422
    )
    assert (
        client.post(f"{API}/institutions/merge", json={"target_id": target["id"], "source_ids": []}).status_code
        == 422
    )
    assert (
        client.post(
            f"{API}/institutions/merge", json={"target_id": target["id"], "source_ids": [9999]}
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"{API}/institutions/merge", json={"target_id": 9999, "source_ids": [source["id"]]}
        ).status_code
        == 404
    )

    resp = client.post(
        f"{API}/institutions/merge", json={"target_id": target["id"], "source_ids": [source["id"]]}
    )
    assert resp.status_code == 200
    assert resp.json()["code"] == "TU"


# --- duplicate finders ---


def test_find_duplicate_debaters_by_name_and_birthdate(client: TestClient) -> None:
    a = _new_debater(client, full_name="John Smith", birthdate="2000-01-01")
    b = _new_debater(client, full_name="  john   SMITH ", birthdate="2000-01-01")
    c = _new_debater(client, full_name="JOHN SMITH", birthdate="1999-05-05")
    _new_debater(client, full_name="Unique Person")

    by_name = client.get(f"{API}/debaters/duplicates").json()
    assert len(by_name) == 1
    assert sorted(m["id"] for m in by_name[0]["members"]) == sorted([a["id"], b["id"], c["id"]])

    by_birthdate = client.get(f"{API}/debaters/duplicates", params={"by": "name_birthdate"}).json()
    assert len(by_birthdate) == 1
    assert sorted(m["id"] for m in by_birthdate[0]["members"]) == sorted([a["id"], b["id"]])

    assert client.get(f"{API}/debaters/duplicates", params={"by": "nope"}).status_code == 422


def test_name_birthdate_ignores_missing_birthdates(client: TestClient) -> None:
    _new_debater(client, full_name="No Birthday")
    _new_debater(client, full_name="No Birthday")
    assert client.get(f"{API}/debaters/duplicates", params={"by": "name_birthdate"}).json() == []
    assert len(client.get(f"{API}/debaters/duplicates").json()) == 1


def test_find_duplicate_judges(client: TestClient) -> None:
    a = _new_judge(client, first_name="Priya", last_name="Patel")
    b = _new_judge(client, full_name="priya patel")
    _new_judge(client, full_name="Someone Else")
    groups = client.get(f"{API}/judges/duplicates").json()
    assert len(groups) == 1
    assert sorted(m["id"] for m in groups[0]["members"]) == sorted([a["id"], b["id"]])


# --- similar institutions ---


def test_similar_institutions_endpoint_ranks_and_thresholds(client: TestClient) -> None:
    exact = client.post(f"{API}/institutions", json={"name": "Royal College", "code": "RC"}).json()
    close = client.post(f"{API}/institutions", json={"name": "Royal College Colombo"}).json()
    client.post(f"{API}/institutions", json={"name": "Zahira College"})

    results = client.get(f"{API}/institutions/similar", params={"name": "royal college"}).json()
    assert [r["id"] for r in results] == [exact["id"], close["id"]]
    assert results[0]["score"] == 1.0
    assert results[0]["code"] == "RC"
    assert results[0]["score"] > results[1]["score"]

    strict = client.get(
        f"{API}/institutions/similar", params={"name": "royal college", "threshold": 1.0}
    ).json()
    # The longer name is no longer similar enough, but still contains the query.
    assert [r["id"] for r in strict] == [exact["id"], close["id"]]

    assert client.get(f"{API}/institutions/similar").status_code == 422  # name is required
    assert client.get(f"{API}/institutions/similar", params={"name": "x", "threshold": 2}).status_code == 422


# --- merge-info lists ---


def test_debater_merge_info(client: TestClient) -> None:
    scaffold = _build_debate_scaffold(client)
    prop, opp = scaffold["prop_debaters"], scaffold["opp_debaters"]
    client.patch(f"{API}/tournaments/{scaffold['tournament']['id']}", json={"abbr": "BC"})
    _post_ballot(
        client, scaffold["debate"]["id"], scaffold["judges"][0]["id"], _full_score_sheet(prop, opp)
    )
    _post_ballot(
        client, scaffold["debate"]["id"], scaffold["judges"][1]["id"], _full_score_sheet(prop, opp)
    )

    resp = client.get(f"{API}/debaters/merge-info")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 6
    names = [r["full_name"] for r in rows]
    assert names == sorted(names, key=str.casefold)  # ordered by name

    first = next(r for r in rows if r["id"] == prop[0]["id"])
    assert first["debates"] == 1  # two ballots, one distinct debate
    assert first["teams"] == [
        {"team_id": scaffold["prop_team"]["id"], "name": "Prop Team", "tournament_abbr": "BC"}
    ]


def test_judge_merge_info_matches_profile_counts(client: TestClient) -> None:
    scaffold = _build_debate_scaffold(client)
    prop, opp = scaffold["prop_debaters"], scaffold["opp_debaters"]
    judge = scaffold["judges"][0]
    _post_ballot(client, scaffold["debate"]["id"], judge["id"], _full_score_sheet(prop, opp))
    client.post(f"{API}/stats/refresh")

    rows = {r["id"]: r for r in client.get(f"{API}/judges/merge-info").json()}
    profile = client.get(f"{API}/judges/{judge['id']}/profile").json()
    assert rows[judge["id"]]["prelims_judged"] == profile["prelims_judged"] == 1
    assert rows[judge["id"]]["elims_judged"] == profile["elims_judged"] == 0
    assert rows[judge["id"]]["tournaments"] == ["Ballot Cup"]
    # Counts come from ballots (like the profiles), so a panellist who submitted none shows zero.
    quiet = scaffold["judges"][1]
    assert rows[quiet["id"]]["prelims_judged"] == 0
    assert rows[quiet["id"]]["tournaments"] == []


def test_institution_merge_info(client: TestClient) -> None:
    inst = client.post(f"{API}/institutions", json={"name": "Inst", "code": "I"}).json()
    client.post(f"{API}/institutions", json={"name": "Aardvark U"})
    tournament = client.post(f"{API}/tournaments", json={"name": "T", "slug": "t"}).json()
    for name in ("Beta", "Alpha"):
        client.post(
            f"{API}/tournaments/{tournament['id']}/teams", json={"name": name, "institution_id": inst["id"]}
        )

    rows = client.get(f"{API}/institutions/merge-info").json()
    assert [r["name"] for r in rows] == ["Aardvark U", "Inst"]
    assert rows[0]["team_count"] == 0
    assert rows[1] == {
        "id": inst["id"],
        "name": "Inst",
        "code": "I",
        "team_count": 2,
        "team_names": ["Alpha", "Beta"],
    }


# --- routing and deletes ---


def test_static_routes_are_not_shadowed_by_id_routes(client: TestClient) -> None:
    for path in (
        "/debaters/merge-info",
        "/debaters/duplicates",
        "/judges/merge-info",
        "/judges/duplicates",
        "/institutions/merge-info",
        "/institutions/similar?name=x",
    ):
        resp = client.get(f"{API}{path}")
        assert resp.status_code == 200, f"{path}: {resp.status_code} {resp.text}"


def test_delete_judge_with_ballots_or_panel_is_409(client: TestClient) -> None:
    scaffold = _build_debate_scaffold(client)
    prop, opp = scaffold["prop_debaters"], scaffold["opp_debaters"]
    judge = scaffold["judges"][0]
    _post_ballot(client, scaffold["debate"]["id"], judge["id"], _full_score_sheet(prop, opp))

    assert client.delete(f"{API}/judges/{judge['id']}").status_code == 409  # ballot
    assert client.delete(f"{API}/judges/{scaffold['judges'][1]['id']}").status_code == 409  # panel only
    assert client.get(f"{API}/judges/{judge['id']}").status_code == 200


def test_delete_judge_with_stale_profile_succeeds(client: TestClient, session: Session) -> None:
    scaffold = _build_debate_scaffold(client)
    prop, opp = scaffold["prop_debaters"], scaffold["opp_debaters"]
    judge = scaffold["judges"][0]
    ballot = _post_ballot(client, scaffold["debate"]["id"], judge["id"], _full_score_sheet(prop, opp))
    client.post(f"{API}/stats/refresh")
    assert session.exec(select(JudgeProfile).where(JudgeProfile.judge_id == judge["id"])).first() is not None

    assert client.delete(f"{API}/ballots/{ballot['id']}").status_code == 204
    assert client.delete(f"{API}/debates/{scaffold['debate']['id']}/judges/{judge['id']}").status_code == 204
    assert client.delete(f"{API}/judges/{judge['id']}").status_code == 204  # was an FK 500

    session.expire_all()
    assert session.exec(select(JudgeProfile).where(JudgeProfile.judge_id == judge["id"])).first() is None


def test_delete_debater_with_stale_profile_succeeds(client: TestClient, session: Session) -> None:
    scaffold = _build_debate_scaffold(client)  # no ballots: roster fallback gives a profile, no scores
    debater = scaffold["prop_debaters"][0]
    client.post(f"{API}/stats/refresh")
    assert session.exec(select(DebaterProfile).where(DebaterProfile.debater_id == debater["id"])).first() is not None

    assert client.delete(f"{API}/debaters/{debater['id']}").status_code == 204  # was an FK 500
    session.expire_all()
    assert session.exec(select(DebaterProfile).where(DebaterProfile.debater_id == debater["id"])).first() is None
