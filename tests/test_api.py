from fastapi.testclient import TestClient


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_full_create_flow(client: TestClient) -> None:
    tournament = client.post(
        "/api/v1/tournaments", json={"name": "Worlds", "slug": "worlds"}
    ).json()

    team_a = client.post(
        f"/api/v1/tournaments/{tournament['id']}/teams", json={"name": "Team A"}
    ).json()
    team_b = client.post(
        f"/api/v1/tournaments/{tournament['id']}/teams", json={"name": "Team B"}
    ).json()

    round_ = client.post(
        f"/api/v1/tournaments/{tournament['id']}/rounds", json={"seq": 1}
    ).json()

    motion_resp = client.put(
        f"/api/v1/rounds/{round_['id']}/motion", json={"text": "This House Would..."}
    )
    assert motion_resp.status_code == 200

    debate = client.post(
        f"/api/v1/rounds/{round_['id']}/debates",
        json={"prop_team_id": team_a["id"], "opp_team_id": team_b["id"]},
    ).json()

    patch_resp = client.patch(
        f"/api/v1/debates/{debate['id']}", json={"winner": "prop"}
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["winner"] == "prop"

    round_get = client.get(f"/api/v1/rounds/{round_['id']}")
    assert round_get.status_code == 200

    debates_list = client.get(f"/api/v1/rounds/{round_['id']}/debates").json()
    assert len(debates_list) == 1
    assert debates_list[0]["winner"] == "prop"

    motion_get = client.get(f"/api/v1/rounds/{round_['id']}/motion").json()
    assert motion_get["text"] == "This House Would..."


def test_debate_same_team_is_422(client: TestClient) -> None:
    tournament = client.post(
        "/api/v1/tournaments", json={"name": "T1", "slug": "t1"}
    ).json()
    team = client.post(
        f"/api/v1/tournaments/{tournament['id']}/teams", json={"name": "Team A"}
    ).json()
    round_ = client.post(
        f"/api/v1/tournaments/{tournament['id']}/rounds", json={"seq": 1}
    ).json()

    response = client.post(
        f"/api/v1/rounds/{round_['id']}/debates",
        json={"prop_team_id": team["id"], "opp_team_id": team["id"]},
    )
    assert response.status_code == 422


def test_debate_cross_tournament_team_is_422(client: TestClient) -> None:
    tournament_1 = client.post(
        "/api/v1/tournaments", json={"name": "T1", "slug": "t1"}
    ).json()
    tournament_2 = client.post(
        "/api/v1/tournaments", json={"name": "T2", "slug": "t2"}
    ).json()

    team_1 = client.post(
        f"/api/v1/tournaments/{tournament_1['id']}/teams", json={"name": "Team A"}
    ).json()
    team_2 = client.post(
        f"/api/v1/tournaments/{tournament_2['id']}/teams", json={"name": "Team B"}
    ).json()

    round_ = client.post(
        f"/api/v1/tournaments/{tournament_1['id']}/rounds", json={"seq": 1}
    ).json()

    response = client.post(
        f"/api/v1/rounds/{round_['id']}/debates",
        json={"prop_team_id": team_1["id"], "opp_team_id": team_2["id"]},
    )
    assert response.status_code == 422


def test_duplicate_round_seq_is_409(client: TestClient) -> None:
    tournament = client.post(
        "/api/v1/tournaments", json={"name": "T1", "slug": "t1"}
    ).json()
    first = client.post(
        f"/api/v1/tournaments/{tournament['id']}/rounds", json={"seq": 1}
    )
    assert first.status_code == 201

    second = client.post(
        f"/api/v1/tournaments/{tournament['id']}/rounds", json={"seq": 1}
    )
    assert second.status_code == 409


def test_get_missing_tournament_is_404(client: TestClient) -> None:
    response = client.get("/api/v1/tournaments/99999")
    assert response.status_code == 404


def test_institution_debater_team_roster_flow(client: TestClient) -> None:
    institution = client.post(
        "/api/v1/institutions", json={"name": "Uni Roster"}
    ).json()
    debater = client.post(
        "/api/v1/debaters",
        json={"name": "Riley Roster", "institution_id": institution["id"]},
    ).json()
    tournament = client.post(
        "/api/v1/tournaments", json={"name": "Roster Cup", "slug": "roster-cup"}
    ).json()
    team = client.post(
        f"/api/v1/tournaments/{tournament['id']}/teams",
        json={"name": "Team Roster", "institution_id": institution["id"]},
    ).json()

    add_resp = client.post(
        f"/api/v1/teams/{team['id']}/members",
        json={"debater_id": debater["id"], "speaker_position": 1},
    )
    assert add_resp.status_code == 201

    members = client.get(f"/api/v1/teams/{team['id']}/members").json()
    assert len(members) == 1


def test_debater_cross_tournament_history(client: TestClient) -> None:
    institution = client.post(
        "/api/v1/institutions", json={"name": "Cross Tournament Uni"}
    ).json()
    debater = client.post(
        "/api/v1/debaters",
        json={"name": "Casey Cross", "institution_id": institution["id"]},
    ).json()

    tournament_1 = client.post(
        "/api/v1/tournaments", json={"name": "CT1", "slug": "ct1"}
    ).json()
    tournament_2 = client.post(
        "/api/v1/tournaments", json={"name": "CT2", "slug": "ct2"}
    ).json()

    team_1 = client.post(
        f"/api/v1/tournaments/{tournament_1['id']}/teams", json={"name": "Team One"}
    ).json()
    team_2 = client.post(
        f"/api/v1/tournaments/{tournament_2['id']}/teams", json={"name": "Team Two"}
    ).json()

    resp_1 = client.post(
        f"/api/v1/teams/{team_1['id']}/members", json={"debater_id": debater["id"]}
    )
    resp_2 = client.post(
        f"/api/v1/teams/{team_2['id']}/members", json={"debater_id": debater["id"]}
    )
    assert resp_1.status_code == 201
    assert resp_2.status_code == 201

    debaters = client.get(
        "/api/v1/debaters", params={"institution_id": institution["id"]}
    ).json()
    assert len(debaters) == 1
    assert debaters[0]["id"] == debater["id"]


def test_duplicate_team_member_debater_is_409(client: TestClient) -> None:
    debater = client.post("/api/v1/debaters", json={"name": "Dup Debater"}).json()
    tournament = client.post(
        "/api/v1/tournaments", json={"name": "Dup Tournament", "slug": "dup-tournament"}
    ).json()
    team = client.post(
        f"/api/v1/tournaments/{tournament['id']}/teams", json={"name": "Dup Team"}
    ).json()

    first = client.post(
        f"/api/v1/teams/{team['id']}/members", json={"debater_id": debater["id"]}
    )
    assert first.status_code == 201

    second = client.post(
        f"/api/v1/teams/{team['id']}/members", json={"debater_id": debater["id"]}
    )
    assert second.status_code == 409


def test_duplicate_speaker_position_is_409(client: TestClient) -> None:
    debater_1 = client.post("/api/v1/debaters", json={"name": "Speaker One"}).json()
    debater_2 = client.post("/api/v1/debaters", json={"name": "Speaker Two"}).json()
    tournament = client.post(
        "/api/v1/tournaments", json={"name": "Speaker Tournament", "slug": "speaker-tournament"}
    ).json()
    team = client.post(
        f"/api/v1/tournaments/{tournament['id']}/teams", json={"name": "Speaker Team"}
    ).json()

    first = client.post(
        f"/api/v1/teams/{team['id']}/members",
        json={"debater_id": debater_1["id"], "speaker_position": 1},
    )
    assert first.status_code == 201

    second = client.post(
        f"/api/v1/teams/{team['id']}/members",
        json={"debater_id": debater_2["id"], "speaker_position": 1},
    )
    assert second.status_code == 409


def test_create_debater_with_missing_institution_is_404(client: TestClient) -> None:
    response = client.post(
        "/api/v1/debaters", json={"name": "Ghost Debater", "institution_id": 99999}
    )
    assert response.status_code == 404


def test_delete_institution_referenced_by_team_is_409_then_204(client: TestClient) -> None:
    institution = client.post(
        "/api/v1/institutions", json={"name": "Delete Me Uni"}
    ).json()
    tournament = client.post(
        "/api/v1/tournaments", json={"name": "Delete Uni Tournament", "slug": "delete-uni-tournament"}
    ).json()
    team = client.post(
        f"/api/v1/tournaments/{tournament['id']}/teams",
        json={"name": "Delete Uni Team", "institution_id": institution["id"]},
    ).json()
    debater = client.post(
        "/api/v1/debaters",
        json={"name": "Delete Uni Debater", "institution_id": institution["id"]},
    ).json()

    blocked = client.delete(f"/api/v1/institutions/{institution['id']}")
    assert blocked.status_code == 409

    assert client.delete(f"/api/v1/teams/{team['id']}").status_code == 204
    assert client.delete(f"/api/v1/debaters/{debater['id']}").status_code == 204

    allowed = client.delete(f"/api/v1/institutions/{institution['id']}")
    assert allowed.status_code == 204


def test_delete_debater_on_team_clears_roster(client: TestClient) -> None:
    debater = client.post("/api/v1/debaters", json={"name": "Removable Debater"}).json()
    tournament = client.post(
        "/api/v1/tournaments", json={"name": "Remove Tournament", "slug": "remove-tournament"}
    ).json()
    team = client.post(
        f"/api/v1/tournaments/{tournament['id']}/teams", json={"name": "Remove Team"}
    ).json()
    client.post(f"/api/v1/teams/{team['id']}/members", json={"debater_id": debater["id"]})

    delete_resp = client.delete(f"/api/v1/debaters/{debater['id']}")
    assert delete_resp.status_code == 204

    members = client.get(f"/api/v1/teams/{team['id']}/members").json()
    assert members == []


def test_get_missing_institution_is_404(client: TestClient) -> None:
    response = client.get("/api/v1/institutions/99999")
    assert response.status_code == 404


def _build_debate_scaffold(client: TestClient) -> dict:
    tournament = client.post(
        "/api/v1/tournaments", json={"name": "Ballot Cup", "slug": "ballot-cup"}
    ).json()
    prop_team = client.post(
        f"/api/v1/tournaments/{tournament['id']}/teams", json={"name": "Prop Team"}
    ).json()
    opp_team = client.post(
        f"/api/v1/tournaments/{tournament['id']}/teams", json={"name": "Opp Team"}
    ).json()

    prop_debaters = [
        client.post("/api/v1/debaters", json={"name": f"Prop Speaker {i}"}).json()
        for i in range(1, 4)
    ]
    opp_debaters = [
        client.post("/api/v1/debaters", json={"name": f"Opp Speaker {i}"}).json()
        for i in range(1, 4)
    ]
    for i, debater in enumerate(prop_debaters, start=1):
        client.post(
            f"/api/v1/teams/{prop_team['id']}/members",
            json={"debater_id": debater["id"], "speaker_position": i},
        )
    for i, debater in enumerate(opp_debaters, start=1):
        client.post(
            f"/api/v1/teams/{opp_team['id']}/members",
            json={"debater_id": debater["id"], "speaker_position": i},
        )

    round_ = client.post(
        f"/api/v1/tournaments/{tournament['id']}/rounds", json={"seq": 1}
    ).json()
    debate = client.post(
        f"/api/v1/rounds/{round_['id']}/debates",
        json={"prop_team_id": prop_team["id"], "opp_team_id": opp_team["id"]},
    ).json()

    judges = [
        client.post("/api/v1/judges", json={"name": f"Judge {i}"}).json() for i in range(1, 4)
    ]
    for judge in judges:
        resp = client.post(
            f"/api/v1/debates/{debate['id']}/judges", json={"judge_id": judge["id"]}
        )
        assert resp.status_code == 201

    return {
        "tournament": tournament,
        "prop_team": prop_team,
        "opp_team": opp_team,
        "prop_debaters": prop_debaters,
        "opp_debaters": opp_debaters,
        "round": round_,
        "debate": debate,
        "judges": judges,
    }


def _full_score_sheet(
    prop_debaters: list[dict], opp_debaters: list[dict], prop_reply_idx: int = 0, opp_reply_idx: int = 1
) -> list[dict]:
    return [
        {"debater_id": prop_debaters[0]["id"], "side": "prop", "position": 1, "score": 76.0},
        {"debater_id": prop_debaters[1]["id"], "side": "prop", "position": 2, "score": 74.5},
        {"debater_id": prop_debaters[2]["id"], "side": "prop", "position": 3, "score": 75.0},
        {
            "debater_id": prop_debaters[prop_reply_idx]["id"],
            "side": "prop",
            "position": 4,
            "score": 38.0,
        },
        {"debater_id": opp_debaters[0]["id"], "side": "opp", "position": 1, "score": 75.5},
        {"debater_id": opp_debaters[1]["id"], "side": "opp", "position": 2, "score": 75.0},
        {"debater_id": opp_debaters[2]["id"], "side": "opp", "position": 3, "score": 74.0},
        {
            "debater_id": opp_debaters[opp_reply_idx]["id"],
            "side": "opp",
            "position": 4,
            "score": 37.5,
        },
    ]


def test_ballot_happy_path_with_split_and_result(client: TestClient) -> None:
    scaffold = _build_debate_scaffold(client)
    debate_id = scaffold["debate"]["id"]
    judges = scaffold["judges"]
    prop_debaters = scaffold["prop_debaters"]
    opp_debaters = scaffold["opp_debaters"]

    winners = ["prop", "prop", "opp"]
    ballots = []
    for judge, winner in zip(judges, winners):
        resp = client.post(
            f"/api/v1/debates/{debate_id}/ballots",
            json={
                "judge_id": judge["id"],
                "winner": winner,
                "scores": _full_score_sheet(prop_debaters, opp_debaters),
            },
        )
        assert resp.status_code == 201, resp.text
        assert len(resp.json()["scores"]) == 8
        ballots.append(resp.json())

    debate = client.get(f"/api/v1/debates/{debate_id}").json()
    assert debate["winner"] == "prop"

    result = client.get(f"/api/v1/debates/{debate_id}/result").json()
    assert result["winner"] == "prop"
    assert sorted(b["winner"] for b in result["ballots"]) == ["opp", "prop", "prop"]
    prop_first = next(
        s
        for s in result["speakers"]
        if s["debater_id"] == prop_debaters[0]["id"] and s["position"] == 1
    )
    assert prop_first["average_score"] == 76.0

    # Flip the panel result by changing one prop ballot to opp (now 1 prop / 2 opp).
    flip = client.patch(f"/api/v1/ballots/{ballots[0]['id']}", json={"winner": "opp"})
    assert flip.status_code == 200
    debate_after_flip = client.get(f"/api/v1/debates/{debate_id}").json()
    assert debate_after_flip["winner"] == "opp"


def test_ballot_reply_double_row_succeeds(client: TestClient) -> None:
    scaffold = _build_debate_scaffold(client)
    debate_id = scaffold["debate"]["id"]
    judge = scaffold["judges"][0]
    prop_debaters = scaffold["prop_debaters"]
    opp_debaters = scaffold["opp_debaters"]

    scores = _full_score_sheet(prop_debaters, opp_debaters, prop_reply_idx=0, opp_reply_idx=0)
    resp = client.post(
        f"/api/v1/debates/{debate_id}/ballots",
        json={"judge_id": judge["id"], "winner": "prop", "scores": scores},
    )
    assert resp.status_code == 201, resp.text
    reply_rows = [s for s in resp.json()["scores"] if s["debater_id"] == prop_debaters[0]["id"]]
    assert sorted(r["position"] for r in reply_rows) == [1, 4]


def test_ballot_position_collision_is_409(client: TestClient) -> None:
    scaffold = _build_debate_scaffold(client)
    debate_id = scaffold["debate"]["id"]
    judge = scaffold["judges"][0]
    prop_debaters = scaffold["prop_debaters"]
    opp_debaters = scaffold["opp_debaters"]

    scores = [
        {"debater_id": prop_debaters[0]["id"], "side": "prop", "position": 1, "score": 76.0},
        {"debater_id": prop_debaters[1]["id"], "side": "prop", "position": 1, "score": 74.5},
        {"debater_id": prop_debaters[2]["id"], "side": "prop", "position": 3, "score": 75.0},
        {"debater_id": opp_debaters[0]["id"], "side": "opp", "position": 1, "score": 75.5},
        {"debater_id": opp_debaters[1]["id"], "side": "opp", "position": 2, "score": 75.0},
        {"debater_id": opp_debaters[2]["id"], "side": "opp", "position": 3, "score": 74.0},
        {"debater_id": opp_debaters[0]["id"], "side": "opp", "position": 4, "score": 37.5},
        {"debater_id": prop_debaters[0]["id"], "side": "prop", "position": 4, "score": 38.0},
    ]
    resp = client.post(
        f"/api/v1/debates/{debate_id}/ballots",
        json={"judge_id": judge["id"], "winner": "prop", "scores": scores},
    )
    assert resp.status_code == 409


def test_ballot_from_judge_not_on_panel_is_422(client: TestClient) -> None:
    scaffold = _build_debate_scaffold(client)
    debate_id = scaffold["debate"]["id"]
    outsider = client.post("/api/v1/judges", json={"name": "Outsider Judge"}).json()

    resp = client.post(
        f"/api/v1/debates/{debate_id}/ballots",
        json={"judge_id": outsider["id"], "winner": "prop"},
    )
    assert resp.status_code == 422


def test_score_debater_on_wrong_side_is_422(client: TestClient) -> None:
    scaffold = _build_debate_scaffold(client)
    debate_id = scaffold["debate"]["id"]
    judge = scaffold["judges"][0]
    prop_debaters = scaffold["prop_debaters"]
    opp_debaters = scaffold["opp_debaters"]

    scores = _full_score_sheet(prop_debaters, opp_debaters)
    scores[0] = {
        "debater_id": opp_debaters[0]["id"],
        "side": "prop",
        "position": 1,
        "score": 76.0,
    }
    resp = client.post(
        f"/api/v1/debates/{debate_id}/ballots",
        json={"judge_id": judge["id"], "winner": "prop", "scores": scores},
    )
    assert resp.status_code == 422


def test_half_point_score_violation_is_422(client: TestClient) -> None:
    scaffold = _build_debate_scaffold(client)
    debate_id = scaffold["debate"]["id"]
    judge = scaffold["judges"][0]
    prop_debaters = scaffold["prop_debaters"]
    opp_debaters = scaffold["opp_debaters"]

    scores = _full_score_sheet(prop_debaters, opp_debaters)
    scores[0]["score"] = 75.3
    resp = client.post(
        f"/api/v1/debates/{debate_id}/ballots",
        json={"judge_id": judge["id"], "winner": "prop", "scores": scores},
    )
    assert resp.status_code == 422


def test_delete_panel_judge_with_ballot_is_409_then_204(client: TestClient) -> None:
    scaffold = _build_debate_scaffold(client)
    debate_id = scaffold["debate"]["id"]
    judge = scaffold["judges"][0]

    ballot = client.post(
        f"/api/v1/debates/{debate_id}/ballots",
        json={"judge_id": judge["id"], "winner": "prop"},
    ).json()

    blocked = client.delete(f"/api/v1/debates/{debate_id}/judges/{judge['id']}")
    assert blocked.status_code == 409

    assert client.delete(f"/api/v1/ballots/{ballot['id']}").status_code == 204
    allowed = client.delete(f"/api/v1/debates/{debate_id}/judges/{judge['id']}")
    assert allowed.status_code == 204


def test_delete_debater_with_scores_is_409(client: TestClient) -> None:
    scaffold = _build_debate_scaffold(client)
    debate_id = scaffold["debate"]["id"]
    judge = scaffold["judges"][0]
    prop_debaters = scaffold["prop_debaters"]
    opp_debaters = scaffold["opp_debaters"]

    client.post(
        f"/api/v1/debates/{debate_id}/ballots",
        json={
            "judge_id": judge["id"],
            "winner": "prop",
            "scores": _full_score_sheet(prop_debaters, opp_debaters),
        },
    )

    resp = client.delete(f"/api/v1/debaters/{prop_debaters[0]['id']}")
    assert resp.status_code == 409


def test_delete_ballot_removes_scores_and_recomputes(client: TestClient) -> None:
    scaffold = _build_debate_scaffold(client)
    debate_id = scaffold["debate"]["id"]
    judges = scaffold["judges"]

    ballots = [
        client.post(
            f"/api/v1/debates/{debate_id}/ballots",
            json={"judge_id": judge["id"], "winner": winner},
        ).json()
        for judge, winner in zip(judges, ["prop", "prop", "opp"])
    ]
    assert client.get(f"/api/v1/debates/{debate_id}").json()["winner"] == "prop"

    delete_resp = client.delete(f"/api/v1/ballots/{ballots[0]['id']}")
    assert delete_resp.status_code == 204

    remaining = client.get(f"/api/v1/debates/{debate_id}/ballots").json()
    assert len(remaining) == 2
    assert client.get(f"/api/v1/ballots/{ballots[0]['id']}").status_code == 404

    # 1 prop / 1 opp is a tie on an even panel: the prior winner is left unchanged.
    assert client.get(f"/api/v1/debates/{debate_id}").json()["winner"] == "prop"


def test_replace_ballot_scores(client: TestClient) -> None:
    scaffold = _build_debate_scaffold(client)
    debate_id = scaffold["debate"]["id"]
    judge = scaffold["judges"][0]
    prop_debaters = scaffold["prop_debaters"]
    opp_debaters = scaffold["opp_debaters"]

    ballot = client.post(
        f"/api/v1/debates/{debate_id}/ballots",
        json={
            "judge_id": judge["id"],
            "winner": "prop",
            "scores": _full_score_sheet(prop_debaters, opp_debaters),
        },
    ).json()

    new_scores = _full_score_sheet(prop_debaters, opp_debaters)
    new_scores[0]["score"] = 77.0
    resp = client.put(f"/api/v1/ballots/{ballot['id']}/scores", json=new_scores)
    assert resp.status_code == 200
    updated = next(
        s for s in resp.json()["scores"] if s["debater_id"] == prop_debaters[0]["id"] and s["position"] == 1
    )
    assert updated["score"] == 77.0
