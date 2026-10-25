import pytest
from fastapi.testclient import TestClient

from tests.helpers import _build_debate_scaffold, _full_score_sheet, build_tournament

API = "/api/v1"


def _iron_scenario(client: TestClient) -> dict:
    """One debater ("Iron Person") on two teams that debate in the same round, with a
    two-judge panel: position 1 with panel scores 70/80 (merged 75) plus a reply in debate 1,
    position 3 with 60/60 in debate 2."""
    tid = client.post(f"{API}/tournaments", json={"name": "Iron Cup", "slug": "iron-cup"}).json()["id"]
    teams = {
        name: client.post(f"{API}/tournaments/{tid}/teams", json={"name": name}).json()
        for name in "ABCD"
    }
    iron = client.post(f"{API}/debaters", json={"full_name": "Iron Person"}).json()
    members = {
        "A": [iron] + [client.post(f"{API}/debaters", json={"full_name": f"A{i}"}).json() for i in range(2)],
        "B": [client.post(f"{API}/debaters", json={"full_name": f"B{i}"}).json() for i in range(3)],
        "C": [iron] + [client.post(f"{API}/debaters", json={"full_name": f"C{i}"}).json() for i in range(2)],
        "D": [client.post(f"{API}/debaters", json={"full_name": f"D{i}"}).json() for i in range(3)],
    }
    for name, debaters in members.items():
        for debater in debaters:
            client.post(f"{API}/teams/{teams[name]['id']}/members", json={"debater_id": debater["id"]})

    round_ = client.post(f"{API}/tournaments/{tid}/rounds", json={"seq": 1, "name": "Round 1"}).json()
    debates = [
        client.post(
            f"{API}/rounds/{round_['id']}/debates",
            json={"prop_team_id": teams[p]["id"], "opp_team_id": teams[o]["id"]},
        ).json()
        for p, o in (("A", "B"), ("C", "D"))
    ]
    judges = [client.post(f"{API}/judges", json={"full_name": f"J{i}"}).json() for i in range(2)]
    for debate in debates:
        for judge in judges:
            client.post(f"{API}/debates/{debate['id']}/judges", json={"judge_id": judge["id"]})

    def sheet(prop, prop_scores, opp):
        """Full 8-row sheet: positions 1-4 (4 = reply) for each side; opp scores are fixed."""
        rows = [
            {"debater_id": d["id"], "side": "prop", "position": pos, "final_score": sc}
            for pos, (d, sc) in enumerate(zip(prop, prop_scores), start=1)
        ]
        rows += [
            {"debater_id": d["id"], "side": "opp", "position": pos, "final_score": sc}
            for pos, (d, sc) in enumerate(zip(opp, (70.0, 70.0, 70.0, 35.0)), start=1)
        ]
        return rows

    a_team, b_team, c_team, d_team = members["A"], members["B"], members["C"], members["D"]
    for judge, iron_score in zip(judges, (70.0, 80.0)):
        resp = client.post(
            f"{API}/debates/{debates[0]['id']}/ballots",
            json={
                "judge_id": judge["id"],
                "winner": "prop",
                # Iron speaks first and gives the reply for team A.
                "scores": sheet(
                    [a_team[0], a_team[1], a_team[2], a_team[0]],
                    [iron_score, 70.0, 70.0, 35.0],
                    [b_team[0], b_team[1], b_team[2], b_team[0]],
                ),
            },
        )
        assert resp.status_code == 201, resp.text
    for judge in judges:
        resp = client.post(
            f"{API}/debates/{debates[1]['id']}/ballots",
            json={
                "judge_id": judge["id"],
                "winner": "prop",
                # Iron speaks third for team C.
                "scores": sheet(
                    [c_team[1], c_team[2], c_team[0], c_team[1]],
                    [65.0, 65.0, 60.0, 32.0],
                    [d_team[0], d_team[1], d_team[2], d_team[0]],
                ),
            },
        )
        assert resp.status_code == 201, resp.text
    return {"tournament_id": tid, "iron": iron, "round": round_, "debates": debates, "judges": judges}


# --- debater scores ---


def test_debater_raw_scores_respect_include_replies(client: TestClient) -> None:
    scenario = _iron_scenario(client)
    iron_id = scenario["iron"]["id"]

    body = client.get(f"{API}/debaters/{iron_id}/scores").json()
    assert body["debater_id"] == iron_id
    assert body["name"] == "Iron Person"
    assert body["normalized"] is False
    (tournament,) = body["tournaments"]
    assert tournament["tournament_id"] == scenario["tournament_id"]
    assert tournament["tournament_name"] == "Iron Cup"
    assert tournament["normalized"] == []

    raw = tournament["raw"]
    assert len(raw) == 4  # position 1 from two judges + position 3 from two judges, no replies
    assert sorted((r["debate_id"], r["position"], r["score"]) for r in raw) == sorted(
        [
            (scenario["debates"][0]["id"], 1, 70.0),
            (scenario["debates"][0]["id"], 1, 80.0),
            (scenario["debates"][1]["id"], 3, 60.0),
            (scenario["debates"][1]["id"], 3, 60.0),
        ]
    )
    first = raw[0]
    assert set(first) == {
        "round_id", "round_name", "round_seq", "stage", "debate_id", "judge_id", "side", "position", "score",
    }
    assert first["round_name"] == "Round 1"
    assert first["round_seq"] == 1
    assert first["stage"] == "prelim"
    assert {r["side"] for r in raw} == {"prop"}
    assert {r["judge_id"] for r in raw} == {j["id"] for j in scenario["judges"]}

    with_replies = client.get(
        f"{API}/debaters/{iron_id}/scores", params={"include_replies": True}
    ).json()["tournaments"][0]["raw"]
    assert len(with_replies) == 6
    assert sorted(r["score"] for r in with_replies if r["position"] == 4) == [35.0, 35.0]


def test_debater_normalized_scores_apply_panel_mean_reply_drop_and_iron_max(client: TestClient) -> None:
    scenario = _iron_scenario(client)
    iron_id = scenario["iron"]["id"]

    body = client.get(f"{API}/debaters/{iron_id}/scores", params={"normalized": True}).json()
    assert body["normalized"] is True
    (tournament,) = body["tournaments"]
    assert tournament["raw"] == []
    (entry,) = tournament["normalized"]  # one round
    assert entry["round_id"] == scenario["round"]["id"]
    assert entry["stage"] == "prelim"
    assert entry["score"] == pytest.approx(75.0)  # max(panel mean 75, panel mean 60); reply ignored
    assert entry["is_iron"] is True

    # include_replies is irrelevant in normalized mode: replies are always dropped.
    again = client.get(
        f"{API}/debaters/{iron_id}/scores", params={"normalized": True, "include_replies": True}
    ).json()
    assert again["tournaments"][0]["normalized"] == tournament["normalized"]


def test_normalized_scores_flag_non_iron_debaters_and_split_stages(client: TestClient) -> None:
    built = build_tournament(client, prelim_rounds=2, elim_rounds=1, teams=4, slug="norm-cup")
    debater = built["teams"][0]["debaters"][0]  # always on the winning prop team, speaks first

    body = client.get(f"{API}/debaters/{debater['id']}/scores", params={"normalized": True}).json()
    entries = body["tournaments"][0]["normalized"]
    assert [e["stage"] for e in entries] == ["prelim", "prelim", "elim"]
    assert [e["round_seq"] for e in entries] == [1, 2, 3]
    assert all(e["score"] == pytest.approx(76.0) and e["is_iron"] is False for e in entries)


def test_debater_scores_empty_and_404(client: TestClient) -> None:
    debater = client.post(f"{API}/debaters", json={"full_name": "Nobody"}).json()
    body = client.get(f"{API}/debaters/{debater['id']}/scores").json()
    assert body == {"debater_id": debater["id"], "name": "Nobody", "normalized": False, "tournaments": []}
    assert client.get(f"{API}/debaters/9999/scores").status_code == 404


def test_discarded_ballots_are_not_counted_in_raw_scores(client: TestClient) -> None:
    scaffold = _build_debate_scaffold(client)
    prop, opp = scaffold["prop_debaters"], scaffold["opp_debaters"]
    ballots = []
    for judge in scaffold["judges"][:2]:
        resp = client.post(
            f"{API}/debates/{scaffold['debate']['id']}/ballots",
            json={"judge_id": judge["id"], "winner": "prop", "scores": _full_score_sheet(prop, opp)},
        )
        ballots.append(resp.json())
    assert client.patch(f"{API}/ballots/{ballots[1]['id']}", json={"discarded": True}).status_code == 200

    raw = client.get(f"{API}/debaters/{prop[0]['id']}/scores").json()["tournaments"][0]["raw"]
    assert [r["judge_id"] for r in raw] == [scaffold["judges"][0]["id"]]


def test_bulk_debater_scores_match_single_endpoint(client: TestClient) -> None:
    built = build_tournament(client, prelim_rounds=2, elim_rounds=1, teams=4, slug="bulk-cup")

    for normalized in (False, True):
        bulk = client.get(f"{API}/stats/debater-scores", params={"normalized": normalized}).json()
        assert len(bulk) == 12  # 4 teams x 3 debaters, all with substantive scores
        assert [b["debater_id"] for b in bulk] == sorted(b["debater_id"] for b in bulk)
        sample = built["teams"][2]["debaters"][1]
        single = client.get(
            f"{API}/debaters/{sample['id']}/scores", params={"normalized": normalized}
        ).json()
        assert next(b for b in bulk if b["debater_id"] == sample["id"]) == single


# --- debater debates and teams ---


def test_debater_debates_by_stage(client: TestClient) -> None:
    built = build_tournament(client, prelim_rounds=2, elim_rounds=1, teams=4, slug="debates-cup")
    winner = built["teams"][0]["debaters"][0]
    loser = built["teams"][3]["debaters"][0]

    body = client.get(f"{API}/debaters/{winner['id']}/debates").json()
    assert len(body) == 1
    assert body[0]["tournament_name"] == "Debates Cup"
    debates = body[0]["debates"]
    assert [d["stage"] for d in debates] == ["prelim", "prelim", "elim"]
    assert all(d["won"] is True and d["side"] == "prop" and d["team_name"] == "Team 0" for d in debates)
    assert {d["opponent_team_name"] for d in debates} <= {"Team 1", "Team 2", "Team 3"}
    assert debates[0]["round_name"] == "Round 1"
    assert debates[0]["team_id"] == built["teams"][0]["team"]["id"]

    prelims = client.get(f"{API}/debaters/{winner['id']}/debates", params={"stage": "prelim"}).json()
    assert [d["stage"] for d in prelims[0]["debates"]] == ["prelim", "prelim"]
    elims = client.get(f"{API}/debaters/{winner['id']}/debates", params={"stage": "elim"}).json()
    assert len(elims[0]["debates"]) == 1

    lost = client.get(f"{API}/debaters/{loser['id']}/debates").json()[0]["debates"]
    assert all(d["won"] is False and d["side"] == "opp" for d in lost)

    assert client.get(f"{API}/debaters/{winner['id']}/debates", params={"stage": "bogus"}).status_code == 422
    assert client.get(f"{API}/debaters/9999/debates").status_code == 404


def test_debater_debates_empty_for_debater_without_teams(client: TestClient) -> None:
    debater = client.post(f"{API}/debaters", json={"full_name": "Lonely"}).json()
    assert client.get(f"{API}/debaters/{debater['id']}/debates").json() == []
    assert client.get(f"{API}/debaters/{debater['id']}/teams").json() == []


def test_debater_teams_joined_with_tournament_in_date_order(client: TestClient) -> None:
    built = build_tournament(client, prelim_rounds=1, elim_rounds=0, teams=2, slug="first-cup")
    debater = built["teams"][0]["debaters"][0]
    client.patch(f"{API}/tournaments/{built['tournament']['id']}", json={"date": "2024-05-01", "abbr": "FC"})

    earlier = client.post(
        f"{API}/tournaments", json={"name": "Earlier Cup", "slug": "earlier-cup", "date": "2023-01-01"}
    ).json()
    institution = client.post(f"{API}/institutions", json={"name": "Uni"}).json()
    team = client.post(
        f"{API}/tournaments/{earlier['id']}/teams",
        json={"name": "Uni 1", "institution_id": institution["id"]},
    ).json()
    client.post(f"{API}/teams/{team['id']}/members", json={"debater_id": debater["id"]})

    body = client.get(f"{API}/debaters/{debater['id']}/teams").json()
    assert [t["team_name"] for t in body] == ["Uni 1", "Team 0"]
    assert body[0] == {
        "tournament_id": earlier["id"],
        "tournament_name": "Earlier Cup",
        "tournament_abbr": None,
        "tournament_date": "2023-01-01",
        "team_id": team["id"],
        "team_name": "Uni 1",
        "institution_id": institution["id"],
    }
    assert body[1]["tournament_abbr"] == "FC"
    assert client.get(f"{API}/debaters/9999/teams").status_code == 404


# --- judge scores and debates ---


def test_judge_scores_grouped_by_tournament_with_reply_toggle(client: TestClient) -> None:
    built = build_tournament(client, prelim_rounds=2, elim_rounds=1, teams=4, slug="judge-cup")
    judge = built["judges"][0]
    # 2 prelim rounds x 2 debates + 1 elim round x 2 debates = 6 debates; 6 substantive scores each.

    body = client.get(f"{API}/judges/{judge['id']}/scores").json()
    assert len(body) == 1
    scores = body[0]["scores"]
    assert body[0]["tournament_name"] == "Judge Cup"
    assert len(scores) == 36
    assert sum(1 for s in scores if s["stage"] == "prelim") == 24
    assert sum(1 for s in scores if s["stage"] == "elim") == 12
    assert [s["round_seq"] for s in scores] == sorted(s["round_seq"] for s in scores)
    assert all(s["position"] != 4 for s in scores)
    assert set(scores[0]) == {
        "round_id", "round_name", "round_seq", "stage", "debate_id", "debater_id", "side", "position", "score",
    }

    with_replies = client.get(f"{API}/judges/{judge['id']}/scores", params={"include_replies": True}).json()
    assert len(with_replies[0]["scores"]) == 48
    assert client.get(f"{API}/judges/9999/scores").status_code == 404


def test_judge_debates_flags_and_stage_filter(client: TestClient) -> None:
    built = build_tournament(client, prelim_rounds=2, elim_rounds=1, teams=4, slug="panel-cup")
    judge = built["judges"][0]

    body = client.get(f"{API}/judges/{judge['id']}/debates").json()
    assert len(body) == 1
    debates = body[0]["debates"]
    assert len(debates) == 6
    assert all(d["ballot_submitted"] is True and d["is_chair"] is False and d["is_trainee"] is False for d in debates)
    assert sum(1 for d in debates if d["stage"] == "prelim") == 4

    prelim = client.get(f"{API}/judges/{judge['id']}/debates", params={"stage": "prelim"}).json()
    assert len(prelim[0]["debates"]) == 4
    elim = client.get(f"{API}/judges/{judge['id']}/debates", params={"stage": "elim"}).json()
    assert len(elim[0]["debates"]) == 2
    assert client.get(f"{API}/judges/{judge['id']}/debates", params={"stage": "x"}).status_code == 422
    assert client.get(f"{API}/judges/9999/debates").status_code == 404


def test_judge_debates_trainee_without_ballot(client: TestClient) -> None:
    scaffold = _build_debate_scaffold(client)
    trainee = client.post(f"{API}/judges", json={"full_name": "Trainee"}).json()
    client.post(
        f"{API}/debates/{scaffold['debate']['id']}/judges",
        json={"judge_id": trainee["id"], "is_trainee": True},
    )
    chair2 = client.post(f"{API}/judges", json={"full_name": "Chair"}).json()
    client.post(
        f"{API}/debates/{scaffold['debate']['id']}/judges",
        json={"judge_id": chair2["id"], "is_chair": True},
    )

    (group,) = client.get(f"{API}/judges/{trainee['id']}/debates").json()
    assert group["debates"] == [
        {
            "debate_id": scaffold["debate"]["id"],
            "round_id": scaffold["round"]["id"],
            "round_name": None,
            "round_seq": 1,
            "stage": "prelim",
            "is_chair": False,
            "is_trainee": True,
            "ballot_submitted": False,
        }
    ]
    (chair_group,) = client.get(f"{API}/judges/{chair2['id']}/debates").json()
    assert chair_group["debates"][0]["is_chair"] is True


# --- tournament averages ---


def test_tournament_averages_use_counted_substantive_prelim_scores(client: TestClient) -> None:
    built = build_tournament(client, prelim_rounds=2, elim_rounds=1, teams=4, slug="avg-cup")
    empty = client.post(f"{API}/tournaments", json={"name": "Empty", "slug": "empty"}).json()

    rows = {r["tournament_id"]: r for r in client.get(f"{API}/stats/tournament-averages").json()}
    built_row = rows[built["tournament"]["id"]]
    # 2 prelim rounds x 2 debates x 3 ballots x 6 substantive scores; elims excluded.
    assert built_row["scores"] == 72
    assert built_row["average"] == pytest.approx(75.0)  # (76 + 74.5 + 75 + 75.5 + 75 + 74) / 6
    assert built_row["tournament_name"] == "Avg Cup"
    assert rows[empty["id"]]["scores"] == 0
    assert rows[empty["id"]]["average"] is None
