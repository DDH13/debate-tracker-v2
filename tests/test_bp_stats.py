from fastapi.testclient import TestClient

from tests.helpers import _bp_score_sheet, _build_bp_debate_scaffold, _build_debate_scaffold, _full_score_sheet


def _submit_bp_ballot(client: TestClient, debate_id: int, judge_id: int, teams, rank_by_side: dict) -> None:
    rankings, scores = _bp_score_sheet(teams, rank_by_side=rank_by_side)
    resp = client.post(
        f"/api/v1/bp-debates/{debate_id}/ballots",
        json={"judge_id": judge_id, "rankings": rankings, "scores": scores},
    )
    assert resp.status_code == 201, resp.text


def test_bp_speaker_tab_and_team_standings(client: TestClient) -> None:
    scaffold = _build_bp_debate_scaffold(client, slug="bp-stats-cup")
    tournament_id = scaffold["tournament"]["id"]
    debate_id = scaffold["debate"]["id"]
    teams = scaffold["teams"]
    judges = scaffold["judges"]

    _submit_bp_ballot(client, debate_id, judges[0]["id"], teams, {"og": 1, "co": 2, "cg": 3, "oo": 4})
    _submit_bp_ballot(client, debate_id, judges[1]["id"], teams, {"og": 1, "co": 2, "cg": 3, "oo": 4})

    tab = client.get(f"/api/v1/tournaments/{tournament_id}/bp/speaker-tab").json()
    assert tab["minimum_speeches"] == 1
    assert len(tab["rows"]) == 8
    assert tab["rows"][0]["average"] >= tab["rows"][-1]["average"]

    standings = client.get(f"/api/v1/tournaments/{tournament_id}/bp/team-standings").json()
    assert len(standings) == 4
    by_side = {}
    for team in teams:
        row = next(s for s in standings if s["team_id"] == team["team"]["id"])
        by_side[team["side"]] = row
    assert by_side["og"]["prelim_points"] == 3  # one debate, 3 team points for 1st
    assert by_side["oo"]["prelim_points"] == 0
    assert by_side["og"]["rank"] == 1
    assert by_side["oo"]["rank"] == 4
    # OG has the highest total speaks (highest-ranked side gets the highest base score).
    assert by_side["og"]["total_speaks"] > by_side["oo"]["total_speaks"]


def test_bp_tournament_summary(client: TestClient) -> None:
    scaffold = _build_bp_debate_scaffold(client, slug="bp-summary-cup")
    tournament_id = scaffold["tournament"]["id"]
    debate_id = scaffold["debate"]["id"]
    teams = scaffold["teams"]
    judges = scaffold["judges"]

    _submit_bp_ballot(client, debate_id, judges[0]["id"], teams, {"og": 1, "co": 2, "cg": 3, "oo": 4})

    summary = client.get(f"/api/v1/tournaments/{tournament_id}/bp/summary").json()
    assert summary["teams"] == 4
    assert summary["debaters"] == 8
    assert summary["debates"] == 1
    assert summary["ballots"] == 1
    assert summary["speaker_scores"] == 8
    assert summary["mean_speaker_score"] is not None


def test_bp_side_stats_and_motion_stats(client: TestClient) -> None:
    scaffold = _build_bp_debate_scaffold(client, slug="bp-side-cup")
    tournament_id = scaffold["tournament"]["id"]
    round_id = scaffold["round"]["id"]
    debate_id = scaffold["debate"]["id"]
    teams = scaffold["teams"]
    judges = scaffold["judges"]

    motion_resp = client.put(f"/api/v1/rounds/{round_id}/motion", json={"text": "This House Would BP."})
    assert motion_resp.status_code == 200

    _submit_bp_ballot(client, debate_id, judges[0]["id"], teams, {"og": 1, "co": 2, "cg": 3, "oo": 4})

    side_stats = client.get(f"/api/v1/tournaments/{tournament_id}/bp/side-stats").json()
    overall_by_side = {s["side"]: s for s in side_stats["overall"]}
    assert overall_by_side["og"]["firsts"] == 1
    assert overall_by_side["og"]["average_points"] == 3
    assert overall_by_side["oo"]["fourths"] == 1
    assert len(side_stats["by_round"]) == 1

    motion_stats = client.get(f"/api/v1/tournaments/{tournament_id}/bp/motion-stats").json()
    assert len(motion_stats) == 1
    sides = {s["side"]: s for s in motion_stats[0]["sides"]}
    assert sides["og"]["average_points"] == 3
    assert sides["og"]["average_speaks"] is not None


def test_bp_stats_require_bp_tournament(client: TestClient) -> None:
    tournament = client.post(
        "/api/v1/tournaments", json={"name": "Two Team Cup", "slug": "two-team-stats-cup"}
    ).json()
    resp = client.get(f"/api/v1/tournaments/{tournament['id']}/bp/speaker-tab")
    assert resp.status_code == 409


def test_refresh_profiles_excludes_bp_tournaments(client: TestClient) -> None:
    two_team = _build_debate_scaffold(client)
    resp = client.post(
        f"/api/v1/debates/{two_team['debate']['id']}/ballots",
        json={
            "judge_id": two_team["judges"][0]["id"],
            "winner": "prop",
            "scores": _full_score_sheet(two_team["prop_debaters"], two_team["opp_debaters"]),
        },
    )
    assert resp.status_code == 201, resp.text

    bp_scaffold = _build_bp_debate_scaffold(client, slug="bp-refresh-cup")
    _submit_bp_ballot(
        client,
        bp_scaffold["debate"]["id"],
        bp_scaffold["judges"][0]["id"],
        bp_scaffold["teams"],
        {"og": 1, "co": 2, "cg": 3, "oo": 4},
    )

    refresh = client.post("/api/v1/stats/refresh")
    assert refresh.status_code == 200
    body = refresh.json()
    assert body["bp_tournaments_excluded"] == 1
    assert body["debater_profiles"] == 6  # only the two-team scaffold's 6 debaters

    profile = client.get(f"/api/v1/debaters/{two_team['prop_debaters'][0]['id']}/profile")
    assert profile.status_code == 200

    bp_debater_id = bp_scaffold["teams"][0]["debaters"][0]["id"]
    bp_profile = client.get(f"/api/v1/debaters/{bp_debater_id}/profile")
    assert bp_profile.status_code == 404
