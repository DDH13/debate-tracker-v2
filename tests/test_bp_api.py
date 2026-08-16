from fastapi.testclient import TestClient

from tests.helpers import _bp_score_sheet, _build_bp_debate_scaffold


def test_bp_debate_requires_bp_tournament(client: TestClient) -> None:
    tournament = client.post(
        "/api/v1/tournaments", json={"name": "Two Team Cup", "slug": "two-team-cup"}
    ).json()
    team = client.post(
        f"/api/v1/tournaments/{tournament['id']}/teams", json={"name": "Team A"}
    ).json()
    round_ = client.post(
        f"/api/v1/tournaments/{tournament['id']}/rounds", json={"seq": 1}
    ).json()

    resp = client.post(
        f"/api/v1/rounds/{round_['id']}/bp-debates",
        json={"teams": [{"team_id": team["id"], "side": "og"}]},
    )
    assert resp.status_code == 409


def test_bp_debate_requires_four_distinct_sides(client: TestClient) -> None:
    scaffold = _build_bp_debate_scaffold(client, slug="bp-sides-cup")
    round_id = scaffold["round"]["id"]
    teams = scaffold["teams"]

    too_few = client.post(
        f"/api/v1/rounds/{round_id}/bp-debates",
        json={"teams": [{"team_id": teams[0]["team"]["id"], "side": "og"}]},
    )
    assert too_few.status_code == 422

    duplicate_side = client.post(
        f"/api/v1/rounds/{round_id}/bp-debates",
        json={
            "teams": [
                {"team_id": teams[0]["team"]["id"], "side": "og"},
                {"team_id": teams[1]["team"]["id"], "side": "og"},
                {"team_id": teams[2]["team"]["id"], "side": "cg"},
                {"team_id": teams[3]["team"]["id"], "side": "co"},
            ]
        },
    )
    assert duplicate_side.status_code == 422

    duplicate_team = client.post(
        f"/api/v1/rounds/{round_id}/bp-debates",
        json={
            "teams": [
                {"team_id": teams[0]["team"]["id"], "side": "og"},
                {"team_id": teams[0]["team"]["id"], "side": "oo"},
                {"team_id": teams[2]["team"]["id"], "side": "cg"},
                {"team_id": teams[3]["team"]["id"], "side": "co"},
            ]
        },
    )
    assert duplicate_team.status_code == 422


def test_bp_debate_lifecycle(client: TestClient) -> None:
    scaffold = _build_bp_debate_scaffold(client, slug="bp-lifecycle-cup")
    debate = scaffold["debate"]

    assert len(debate["teams"]) == 4
    assert {t["side"] for t in debate["teams"]} == {"og", "oo", "cg", "co"}

    get_resp = client.get(f"/api/v1/bp-debates/{debate['id']}")
    assert get_resp.status_code == 200
    assert get_resp.json()["room"] is None

    patch_resp = client.patch(f"/api/v1/bp-debates/{debate['id']}", json={"room": "Room 5"})
    assert patch_resp.status_code == 200
    assert patch_resp.json()["room"] == "Room 5"

    list_resp = client.get(f"/api/v1/rounds/{scaffold['round']['id']}/bp-debates")
    assert list_resp.status_code == 200
    assert len(list_resp.json()) == 1

    delete_resp = client.delete(f"/api/v1/bp-debates/{debate['id']}")
    assert delete_resp.status_code == 204
    assert client.get(f"/api/v1/bp-debates/{debate['id']}").status_code == 404


def test_bp_panel_allocation(client: TestClient) -> None:
    scaffold = _build_bp_debate_scaffold(client, slug="bp-panel-cup")
    debate_id = scaffold["debate"]["id"]
    judges = scaffold["judges"]

    judges_list = client.get(f"/api/v1/bp-debates/{debate_id}/judges").json()
    assert len(judges_list) == 2

    duplicate = client.post(
        f"/api/v1/bp-debates/{debate_id}/judges", json={"judge_id": judges[0]["id"]}
    )
    assert duplicate.status_code == 409

    rankings, scores = _bp_score_sheet(
        scaffold["teams"], rank_by_side={"og": 1, "co": 2, "cg": 3, "oo": 4}
    )
    ballot_resp = client.post(
        f"/api/v1/bp-debates/{debate_id}/ballots",
        json={"judge_id": judges[0]["id"], "rankings": rankings, "scores": scores},
    )
    assert ballot_resp.status_code == 201, ballot_resp.text

    remove_resp = client.delete(f"/api/v1/bp-debates/{debate_id}/judges/{judges[0]['id']}")
    assert remove_resp.status_code == 409

    remove_other = client.delete(f"/api/v1/bp-debates/{debate_id}/judges/{judges[1]['id']}")
    assert remove_other.status_code == 204


def test_bp_ballot_rankings_must_be_permutation(client: TestClient) -> None:
    scaffold = _build_bp_debate_scaffold(client, slug="bp-rank-cup")
    debate_id = scaffold["debate"]["id"]
    judge_id = scaffold["judges"][0]["id"]

    resp = client.post(
        f"/api/v1/bp-debates/{debate_id}/ballots",
        json={
            "judge_id": judge_id,
            "rankings": [
                {"side": "og", "rank": 1},
                {"side": "oo", "rank": 1},
                {"side": "cg", "rank": 3},
                {"side": "co", "rank": 4},
            ],
        },
    )
    assert resp.status_code == 422


def test_bp_ballot_score_sheet_must_have_two_distinct_debaters_per_side(client: TestClient) -> None:
    scaffold = _build_bp_debate_scaffold(client, slug="bp-scoresheet-cup")
    debate_id = scaffold["debate"]["id"]
    judge_id = scaffold["judges"][0]["id"]
    teams = scaffold["teams"]

    rankings, scores = _bp_score_sheet(teams, rank_by_side={"og": 1, "co": 2, "cg": 3, "oo": 4})
    # Force both OG rows onto the same debater, which should fail the 2-distinct-debaters rule.
    og_debater_id = teams[0]["debaters"][0]["id"]
    for row in scores:
        if row["side"] == "og":
            row["debater_id"] = og_debater_id

    resp = client.post(
        f"/api/v1/bp-debates/{debate_id}/ballots",
        json={"judge_id": judge_id, "rankings": rankings, "scores": scores},
    )
    assert resp.status_code == 422


def test_bp_debate_result_recomputes_rank_and_points(client: TestClient) -> None:
    scaffold = _build_bp_debate_scaffold(client, slug="bp-result-cup")
    debate_id = scaffold["debate"]["id"]
    teams = scaffold["teams"]
    judges = scaffold["judges"]

    rankings_a, scores_a = _bp_score_sheet(
        teams, rank_by_side={"og": 1, "co": 2, "cg": 3, "oo": 4}
    )
    resp_a = client.post(
        f"/api/v1/bp-debates/{debate_id}/ballots",
        json={"judge_id": judges[0]["id"], "rankings": rankings_a, "scores": scores_a},
    )
    assert resp_a.status_code == 201, resp_a.text

    rankings_b, scores_b = _bp_score_sheet(
        teams, rank_by_side={"og": 1, "co": 3, "cg": 2, "oo": 4}
    )
    resp_b = client.post(
        f"/api/v1/bp-debates/{debate_id}/ballots",
        json={"judge_id": judges[1]["id"], "rankings": rankings_b, "scores": scores_b},
    )
    assert resp_b.status_code == 201, resp_b.text

    result = client.get(f"/api/v1/bp-debates/{debate_id}/result").json()
    by_side = {t["side"]: t for t in result["teams"]}
    # og: 1+1=2 (best); oo: 4+4=8 (worst); cg: 3+2=5; co: 2+3=5 (tied with cg, stable order).
    assert by_side["og"]["rank"] == 1
    assert by_side["og"]["points"] == 3
    assert by_side["oo"]["rank"] == 4
    assert by_side["oo"]["points"] == 0
    assert len(result["ballots"]) == 2
    assert len(result["speakers"]) == 8

    ballot_id = resp_a.json()["id"]
    discard_resp = client.patch(f"/api/v1/bp-ballots/{ballot_id}", json={"discarded": True})
    assert discard_resp.status_code == 200

    result_after_discard = client.get(f"/api/v1/bp-debates/{debate_id}/result").json()
    by_side_after = {t["side"]: t for t in result_after_discard["teams"]}
    # only ballot B counted now: og=1, cg=2, co=3, oo=4
    assert by_side_after["og"]["rank"] == 1
    assert by_side_after["cg"]["rank"] == 2
    assert by_side_after["co"]["rank"] == 3
    assert by_side_after["oo"]["rank"] == 4


def test_bp_ballot_advance_only_ranking(client: TestClient) -> None:
    # Elimination rounds sometimes only report who advanced/was eliminated, not a full
    # 1-4 placement (e.g. 2 advance + 2 eliminated in a quarter/semi).
    scaffold = _build_bp_debate_scaffold(client, slug="bp-advance-cup")
    debate_id = scaffold["debate"]["id"]
    judges = scaffold["judges"]

    resp = client.post(
        f"/api/v1/bp-debates/{debate_id}/ballots",
        json={
            "judge_id": judges[0]["id"],
            "rankings": [
                {"side": "og", "advanced": True},
                {"side": "oo", "advanced": False},
                {"side": "cg", "advanced": True},
                {"side": "co", "advanced": False},
            ],
        },
    )
    assert resp.status_code == 201, resp.text

    result = client.get(f"/api/v1/bp-debates/{debate_id}/result").json()
    by_side = {t["side"]: t for t in result["teams"]}
    assert by_side["og"]["advanced"] is True
    assert by_side["cg"]["advanced"] is True
    assert by_side["oo"]["advanced"] is False
    assert by_side["co"]["advanced"] is False
    assert by_side["og"]["rank"] is None
    assert by_side["og"]["points"] is None

    ballot_rankings = result["ballots"][0]["rankings"]
    assert all(r["rank"] is None for r in ballot_rankings)
    assert {r["side"]: r["advanced"] for r in ballot_rankings} == {
        "og": True,
        "oo": False,
        "cg": True,
        "co": False,
    }


def test_bp_ballot_rankings_cannot_mix_rank_and_advanced(client: TestClient) -> None:
    scaffold = _build_bp_debate_scaffold(client, slug="bp-mixed-cup")
    debate_id = scaffold["debate"]["id"]
    judge_id = scaffold["judges"][0]["id"]

    resp = client.post(
        f"/api/v1/bp-debates/{debate_id}/ballots",
        json={
            "judge_id": judge_id,
            "rankings": [
                {"side": "og", "rank": 1},
                {"side": "oo", "advanced": False},
                {"side": "cg", "rank": 3},
                {"side": "co", "rank": 4},
            ],
        },
    )
    assert resp.status_code == 422


def test_bp_ballot_advance_only_needs_at_least_one_advancing_side(client: TestClient) -> None:
    scaffold = _build_bp_debate_scaffold(client, slug="bp-noadvance-cup")
    debate_id = scaffold["debate"]["id"]
    judge_id = scaffold["judges"][0]["id"]

    resp = client.post(
        f"/api/v1/bp-debates/{debate_id}/ballots",
        json={
            "judge_id": judge_id,
            "rankings": [
                {"side": "og", "advanced": False},
                {"side": "oo", "advanced": False},
                {"side": "cg", "advanced": False},
                {"side": "co", "advanced": False},
            ],
        },
    )
    assert resp.status_code == 422
