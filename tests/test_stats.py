import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.services.stats import core
from tests.helpers import (
    _build_debate_scaffold,
    _full_score_sheet,
    build_tournament,
)


# --- core.py primitives ---


def test_competition_ranks_shares_ties_with_gap_after() -> None:
    ranks = core.competition_ranks({"a": 90, "b": 80, "c": 80, "d": 70})
    assert ranks == {"a": 1, "b": 2, "c": 2, "d": 4}


def test_competition_ranks_empty_is_empty() -> None:
    assert core.competition_ranks({}) == {}


def test_percentile_ranks_matches_cumulative_percentage() -> None:
    percentiles = core.percentile_ranks({"a": 1, "b": 2, "c": 2, "d": 4})
    assert percentiles["a"] == 25.0
    assert percentiles["b"] == percentiles["c"] == 75.0
    assert percentiles["d"] == 100.0


def test_mean_and_stdev_empty_is_none_none() -> None:
    assert core.mean_and_stdev([]) == (None, None)


def test_panel_merge_and_iron_merge_max_not_sum(client: TestClient, session: Session) -> None:
    """A debater who is a member of two teams debating in the *same round* speaks two
    substantive positions that round (across two different debates). Their round score
    must be the max of the two, not the sum -- and each position's score must itself be
    the mean across the judging panel."""
    tournament = client.post(
        "/api/v1/tournaments", json={"name": "Iron Cup", "slug": "iron-cup"}
    ).json()
    tid = tournament["id"]

    team_a = client.post(f"/api/v1/tournaments/{tid}/teams", json={"name": "A"}).json()
    team_b = client.post(f"/api/v1/tournaments/{tid}/teams", json={"name": "B"}).json()
    team_c = client.post(f"/api/v1/tournaments/{tid}/teams", json={"name": "C"}).json()
    team_d = client.post(f"/api/v1/tournaments/{tid}/teams", json={"name": "D"}).json()

    iron = client.post("/api/v1/debaters", json={"full_name": "Iron Person"}).json()
    a2 = client.post("/api/v1/debaters", json={"full_name": "A2"}).json()
    a3 = client.post("/api/v1/debaters", json={"full_name": "A3"}).json()
    b_members = [client.post("/api/v1/debaters", json={"full_name": f"B{i}"}).json() for i in range(3)]
    c_members = [client.post("/api/v1/debaters", json={"full_name": f"C{i}"}).json() for i in range(2)]
    d_members = [client.post("/api/v1/debaters", json={"full_name": f"D{i}"}).json() for i in range(3)]

    for team, members in (
        (team_a, [iron, a2, a3]),
        (team_b, b_members),
        (team_c, [iron, *c_members]),
        (team_d, d_members),
    ):
        for member in members:
            resp = client.post(f"/api/v1/teams/{team['id']}/members", json={"debater_id": member["id"]})
            assert resp.status_code == 201, resp.text

    round_ = client.post(f"/api/v1/tournaments/{tid}/rounds", json={"seq": 1}).json()
    debate1 = client.post(
        f"/api/v1/rounds/{round_['id']}/debates",
        json={"prop_team_id": team_a["id"], "opp_team_id": team_b["id"]},
    ).json()
    debate2 = client.post(
        f"/api/v1/rounds/{round_['id']}/debates",
        json={"prop_team_id": team_c["id"], "opp_team_id": team_d["id"]},
    ).json()

    judge1 = client.post("/api/v1/judges", json={"full_name": "J1"}).json()
    judge2 = client.post("/api/v1/judges", json={"full_name": "J2"}).json()
    for debate in (debate1, debate2):
        for judge in (judge1, judge2):
            client.post(f"/api/v1/debates/{debate['id']}/judges", json={"judge_id": judge["id"]})

    def sheet(prop_ids, prop_scores, opp_ids, opp_scores):
        rows = []
        for position, (debater_id, score) in enumerate(zip(prop_ids, prop_scores), start=1):
            rows.append({"debater_id": debater_id, "side": "prop", "position": position, "final_score": score})
        for position, (debater_id, score) in enumerate(zip(opp_ids, opp_scores), start=1):
            rows.append({"debater_id": debater_id, "side": "opp", "position": position, "final_score": score})
        return rows

    # Debate 1: iron speaks position 1 for team A. Panel gives 70 and 80 -> merged 75.0.
    for judge, iron_score in ((judge1, 70.0), (judge2, 80.0)):
        resp = client.post(
            f"/api/v1/debates/{debate1['id']}/ballots",
            json={
                "judge_id": judge["id"],
                "winner": "prop",
                "scores": sheet(
                    [iron["id"], a2["id"], a3["id"], a2["id"]],
                    [iron_score, 70.0, 70.0, 35.0],
                    [b_members[0]["id"], b_members[1]["id"], b_members[2]["id"], b_members[0]["id"]],
                    [70.0, 70.0, 70.0, 35.0],
                ),
            },
        )
        assert resp.status_code == 201, resp.text

    # Debate 2: iron speaks position 3 for team C, merged score 60.0 -- lower than debate 1.
    for judge in (judge1, judge2):
        resp = client.post(
            f"/api/v1/debates/{debate2['id']}/ballots",
            json={
                "judge_id": judge["id"],
                "winner": "prop",
                "scores": sheet(
                    [c_members[0]["id"], c_members[1]["id"], iron["id"], c_members[0]["id"]],
                    [65.0, 65.0, 60.0, 32.0],
                    [d_members[0]["id"], d_members[1]["id"], d_members[2]["id"], d_members[0]["id"]],
                    [65.0, 65.0, 65.0, 32.0],
                ),
            },
        )
        assert resp.status_code == 201, resp.text

    scores = core.round_scores(session, tid)
    assert scores[iron["id"]][round_["id"]] == pytest.approx(75.0)  # max(75.0, 60.0), not their sum
    assert scores[a2["id"]][round_["id"]] == pytest.approx(70.0)  # plain panel merge, no iron


# --- speaker tab: replies, speech minimum, ties ---


def test_replies_excluded_from_tab_but_present_in_judge_average_reply(client: TestClient) -> None:
    scaffold = _build_debate_scaffold(client)
    debate_id = scaffold["debate"]["id"]
    judge = scaffold["judges"][0]
    prop = scaffold["prop_debaters"]
    opp = scaffold["opp_debaters"]

    resp = client.post(
        f"/api/v1/debates/{debate_id}/ballots",
        json={"judge_id": judge["id"], "winner": "prop", "scores": _full_score_sheet(prop, opp)},
    )
    assert resp.status_code == 201, resp.text

    tid = scaffold["tournament"]["id"]
    tab = client.get(f"/api/v1/tournaments/{tid}/speaker-tab").json()
    row = next(r for r in tab["rows"] if r["debater_id"] == prop[0]["id"])
    assert row["average"] == pytest.approx(76.0)  # not blended with the 38.0 reply score

    assert client.post("/api/v1/stats/refresh").status_code == 200
    profile = client.get(f"/api/v1/judges/{judge['id']}/profile").json()
    assert profile["average_reply"] == pytest.approx((38.0 + 37.5) / 2)


def test_speaker_tab_excludes_debaters_below_speech_minimum(client: TestClient) -> None:
    tournament = client.post("/api/v1/tournaments", json={"name": "Min Cup", "slug": "min-cup"}).json()
    tid = tournament["id"]
    team_a = client.post(f"/api/v1/tournaments/{tid}/teams", json={"name": "A"}).json()
    team_b = client.post(f"/api/v1/tournaments/{tid}/teams", json={"name": "B"}).json()

    a_members = [client.post("/api/v1/debaters", json={"full_name": f"A{i}"}).json() for i in range(4)]
    for member in a_members:
        client.post(f"/api/v1/teams/{team_a['id']}/members", json={"debater_id": member["id"]})
    b_members = [client.post("/api/v1/debaters", json={"full_name": f"B{i}"}).json() for i in range(3)]
    for member in b_members:
        client.post(f"/api/v1/teams/{team_b['id']}/members", json={"debater_id": member["id"]})

    judge = client.post("/api/v1/judges", json={"full_name": "J"}).json()

    # a_members[3] only ever fills in for round 1, so they end up with 1 speech.
    lineups = [
        [a_members[0], a_members[1], a_members[3]],
        [a_members[0], a_members[1], a_members[2]],
        [a_members[0], a_members[1], a_members[2]],
    ]
    for seq, lineup in enumerate(lineups, start=1):
        round_ = client.post(f"/api/v1/tournaments/{tid}/rounds", json={"seq": seq}).json()
        debate = client.post(
            f"/api/v1/rounds/{round_['id']}/debates",
            json={"prop_team_id": team_a["id"], "opp_team_id": team_b["id"]},
        ).json()
        client.post(f"/api/v1/debates/{debate['id']}/judges", json={"judge_id": judge["id"]})
        resp = client.post(
            f"/api/v1/debates/{debate['id']}/ballots",
            json={
                "judge_id": judge["id"],
                "winner": "prop",
                "scores": _full_score_sheet(lineup, b_members),
            },
        )
        assert resp.status_code == 201, resp.text

    tab = client.get(f"/api/v1/tournaments/{tid}/speaker-tab").json()
    assert tab["minimum_speeches"] == 2
    tab_ids = {row["debater_id"] for row in tab["rows"]}
    assert a_members[3]["id"] not in tab_ids  # 1 speech < minimum of 2
    assert a_members[0]["id"] in tab_ids  # spoke every round
    assert a_members[2]["id"] in tab_ids  # 2 speeches meets the minimum exactly


def test_speaker_tab_tie_ranks_share_and_gap_after(client: TestClient) -> None:
    scaffold = _build_debate_scaffold(client)
    debate1_id = scaffold["debate"]["id"]
    judge = scaffold["judges"][0]
    prop1, opp1 = scaffold["prop_debaters"], scaffold["opp_debaters"]

    client.post(
        f"/api/v1/debates/{debate1_id}/ballots",
        json={"judge_id": judge["id"], "winner": "prop", "scores": _full_score_sheet(prop1, opp1)},
    )

    tid = scaffold["tournament"]["id"]
    round_id = scaffold["round"]["id"]
    team_c = client.post(f"/api/v1/tournaments/{tid}/teams", json={"name": "Team C"}).json()
    team_d = client.post(f"/api/v1/tournaments/{tid}/teams", json={"name": "Team D"}).json()
    prop2 = [client.post("/api/v1/debaters", json={"full_name": f"Prop2 {i}"}).json() for i in range(3)]
    opp2 = [client.post("/api/v1/debaters", json={"full_name": f"Opp2 {i}"}).json() for i in range(3)]
    for member in prop2:
        client.post(f"/api/v1/teams/{team_c['id']}/members", json={"debater_id": member["id"]})
    for member in opp2:
        client.post(f"/api/v1/teams/{team_d['id']}/members", json={"debater_id": member["id"]})
    debate2 = client.post(
        f"/api/v1/rounds/{round_id}/debates",
        json={"prop_team_id": team_c["id"], "opp_team_id": team_d["id"]},
    ).json()
    client.post(f"/api/v1/debates/{debate2['id']}/judges", json={"judge_id": judge["id"]})
    client.post(
        f"/api/v1/debates/{debate2['id']}/ballots",
        json={"judge_id": judge["id"], "winner": "prop", "scores": _full_score_sheet(prop2, opp2)},
    )

    tab = client.get(f"/api/v1/tournaments/{tid}/speaker-tab").json()
    by_rank: dict[int, list[float]] = {}
    for row in tab["rows"]:
        by_rank.setdefault(row["rank"], []).append(row["average"])

    assert sorted(by_rank[1]) == [76.0, 76.0]  # both debate's position-1 prop speakers tie
    assert sorted(by_rank[5]) == [75.0, 75.0, 75.0, 75.0]  # 4-way tie: prop pos3 + opp pos2
    # Competition ranking: a 2-way tie at rank 1 is followed by rank 3, not rank 2 (gap after ties).
    assert sorted(set(row["rank"] for row in tab["rows"])) == [1, 3, 5, 9, 11]


# --- discarded / forfeit exclusion ---


def test_discarded_and_forfeit_ballots_excluded_from_stats(client: TestClient) -> None:
    scaffold = _build_debate_scaffold(client)
    debate_id = scaffold["debate"]["id"]
    judges = scaffold["judges"]
    prop, opp = scaffold["prop_debaters"], scaffold["opp_debaters"]

    ballots = []
    for judge, outlier in zip(judges, [40.0, 95.0, 76.0]):
        sheet = _full_score_sheet(prop, opp)
        sheet[0]["final_score"] = outlier
        resp = client.post(
            f"/api/v1/debates/{debate_id}/ballots",
            json={"judge_id": judge["id"], "winner": "prop", "scores": sheet},
        )
        assert resp.status_code == 201, resp.text
        ballots.append(resp.json())

    client.patch(f"/api/v1/ballots/{ballots[0]['id']}", json={"discarded": True})
    client.patch(f"/api/v1/ballots/{ballots[1]['id']}", json={"forfeit": True})

    tid = scaffold["tournament"]["id"]
    tab = client.get(f"/api/v1/tournaments/{tid}/speaker-tab").json()
    row = next(r for r in tab["rows"] if r["debater_id"] == prop[0]["id"])
    assert row["average"] == pytest.approx(76.0)  # neither 999 nor 888 leak into the merge

    summary = client.get(f"/api/v1/tournaments/{tid}/summary").json()
    assert summary["ballots"] == 3
    assert summary["speaker_scores"] == 8  # only the one counted ballot's 8 rows


# --- judge sentiment ---


def test_judge_sentiment_lenient_judge_and_baseline_threshold(client: TestClient) -> None:
    tournament = client.post(
        "/api/v1/tournaments", json={"name": "Sentiment Cup", "slug": "sentiment-cup"}
    ).json()
    tid = tournament["id"]

    team_a = client.post(f"/api/v1/tournaments/{tid}/teams", json={"name": "A"}).json()
    team_b = client.post(f"/api/v1/tournaments/{tid}/teams", json={"name": "B"}).json()
    target, a2, a3 = (
        client.post("/api/v1/debaters", json={"full_name": n}).json() for n in ("Target", "A2", "A3")
    )
    b_members = [client.post("/api/v1/debaters", json={"full_name": f"B{i}"}).json() for i in range(3)]
    for member in (target, a2, a3):
        client.post(f"/api/v1/teams/{team_a['id']}/members", json={"debater_id": member["id"]})
    for member in b_members:
        client.post(f"/api/v1/teams/{team_b['id']}/members", json={"debater_id": member["id"]})

    normal1 = client.post("/api/v1/judges", json={"full_name": "Normal 1"}).json()
    normal2 = client.post("/api/v1/judges", json={"full_name": "Normal 2"}).json()
    lenient = client.post("/api/v1/judges", json={"full_name": "Lenient"}).json()

    for seq in range(1, 4):
        round_ = client.post(f"/api/v1/tournaments/{tid}/rounds", json={"seq": seq}).json()
        debate = client.post(
            f"/api/v1/rounds/{round_['id']}/debates",
            json={"prop_team_id": team_a["id"], "opp_team_id": team_b["id"]},
        ).json()
        for judge in (normal1, normal2, lenient):
            client.post(f"/api/v1/debates/{debate['id']}/judges", json={"judge_id": judge["id"]})
        for judge, target_score in ((normal1, 75.0), (normal2, 75.0), (lenient, 80.0)):
            sheet = _full_score_sheet([target, a2, a3], b_members)
            sheet[0]["final_score"] = target_score
            resp = client.post(
                f"/api/v1/debates/{debate['id']}/ballots",
                json={"judge_id": judge["id"], "winner": "prop", "scores": sheet},
            )
            assert resp.status_code == 201, resp.text

    # A separate, single-round debate: every debater here only ever gets 3 total scores,
    # so the leave-one-out baseline (2 remaining) never reaches the 5-ballot minimum.
    team_c = client.post(f"/api/v1/tournaments/{tid}/teams", json={"name": "C"}).json()
    team_d = client.post(f"/api/v1/tournaments/{tid}/teams", json={"name": "D"}).json()
    c_members = [client.post("/api/v1/debaters", json={"full_name": f"C{i}"}).json() for i in range(3)]
    d_members = [client.post("/api/v1/debaters", json={"full_name": f"D{i}"}).json() for i in range(3)]
    for member in c_members:
        client.post(f"/api/v1/teams/{team_c['id']}/members", json={"debater_id": member["id"]})
    for member in d_members:
        client.post(f"/api/v1/teams/{team_d['id']}/members", json={"debater_id": member["id"]})
    lone_round = client.post(f"/api/v1/tournaments/{tid}/rounds", json={"seq": 4}).json()
    lone_debate = client.post(
        f"/api/v1/rounds/{lone_round['id']}/debates",
        json={"prop_team_id": team_c["id"], "opp_team_id": team_d["id"]},
    ).json()
    for judge in (normal1, normal2, lenient):
        client.post(f"/api/v1/debates/{lone_debate['id']}/judges", json={"judge_id": judge["id"]})
    for judge, score in ((normal1, 70.0), (normal2, 70.0), (lenient, 75.0)):
        sheet = _full_score_sheet(c_members, d_members)
        sheet[0]["final_score"] = score
        client.post(
            f"/api/v1/debates/{lone_debate['id']}/ballots",
            json={"judge_id": judge["id"], "winner": "prop", "scores": sheet},
        )

    live = client.get("/api/v1/stats/judge-sentiment").json()
    lenient_live = next(r for r in live if r["judge_id"] == lenient["id"])
    assert lenient_live["leniency_count"] == 3
    assert lenient_live["harshness_count"] == 0
    assert lenient_live["neutral_count"] == 15
    assert lenient_live["speeches_considered"] == 18  # the lone debate's 6 speeches are all skipped
    assert lenient_live["leniency"] == pytest.approx(5.0)
    assert lenient_live["overall_sentiment"] == pytest.approx(15 / 18, abs=1e-4)

    assert client.post("/api/v1/stats/refresh").status_code == 200
    profile = client.get(f"/api/v1/judges/{lenient['id']}/profile").json()
    assert profile["leniency_count"] == 3
    assert profile["speeches_considered"] == 18


# --- furthest round ---


def test_furthest_round_picks_highest_elim_seq(client: TestClient) -> None:
    data = build_tournament(client, prelim_rounds=3, elim_rounds=2, teams=4)
    tid = data["tournament"]["id"]
    assert client.post("/api/v1/stats/refresh").status_code == 200

    champion = data["teams"][0]["debaters"][0]  # index 0 always wins (lowest index wins any pairing)
    profile = client.get(f"/api/v1/debaters/{champion['id']}/profile").json()
    final_seq = 3 + 2
    assert len(profile["furthest_rounds"]) == 1
    assert profile["furthest_rounds"][0]["round_seq"] == final_seq
    assert profile["furthest_rounds"][0]["won"] is True

    semi_debates = data["elim_rounds"][0]["debates"]
    other_semi = next(d for d in semi_debates if 0 not in (d["prop_index"], d["opp_index"]))
    loser_index = (
        other_semi["opp_index"]
        if other_semi["winner_index"] == other_semi["prop_index"]
        else other_semi["prop_index"]
    )
    loser_debater = data["teams"][loser_index]["debaters"][0]
    loser_profile = client.get(f"/api/v1/debaters/{loser_debater['id']}/profile").json()
    assert loser_profile["furthest_rounds"][0]["round_seq"] == 3 + 1
    assert loser_profile["furthest_rounds"][0]["won"] is False

    assert tid  # tournament actually got used


# --- refresh idempotency + profile 404/200 ---


def test_refresh_is_idempotent(client: TestClient) -> None:
    build_tournament(client, prelim_rounds=2, elim_rounds=0, teams=4)
    first = client.post("/api/v1/stats/refresh").json()
    debaters_first = client.get("/api/v1/stats/debater-profiles").json()
    judges_first = client.get("/api/v1/stats/judge-profiles").json()

    second = client.post("/api/v1/stats/refresh").json()
    debaters_second = client.get("/api/v1/stats/debater-profiles").json()
    judges_second = client.get("/api/v1/stats/judge-profiles").json()

    assert first["debater_profiles"] == second["debater_profiles"]
    assert first["judge_profiles"] == second["judge_profiles"]

    def _strip(rows: list[dict]) -> list[dict]:
        return [{k: v for k, v in row.items() if k not in ("id", "computed_at")} for row in rows]

    assert _strip(debaters_first) == _strip(debaters_second)
    assert _strip(judges_first) == _strip(judges_second)


def test_debater_profile_404_before_refresh_then_200_after(client: TestClient) -> None:
    scaffold = _build_debate_scaffold(client)
    debater_id = scaffold["prop_debaters"][0]["id"]

    assert client.get(f"/api/v1/debaters/{debater_id}/profile").status_code == 404

    client.post(
        f"/api/v1/debates/{scaffold['debate']['id']}/ballots",
        json={
            "judge_id": scaffold["judges"][0]["id"],
            "winner": "prop",
            "scores": _full_score_sheet(scaffold["prop_debaters"], scaffold["opp_debaters"]),
        },
    )
    assert client.post("/api/v1/stats/refresh").status_code == 200
    assert client.get(f"/api/v1/debaters/{debater_id}/profile").status_code == 200


def test_judge_profile_404_before_refresh(client: TestClient) -> None:
    judge = client.post("/api/v1/judges", json={"full_name": "Nobody Judged Yet"}).json()
    assert client.get(f"/api/v1/judges/{judge['id']}/profile").status_code == 404


# --- empty tournament ---


def test_empty_tournament_returns_empty_structures(client: TestClient) -> None:
    tournament = client.post("/api/v1/tournaments", json={"name": "Empty", "slug": "empty"}).json()
    tid = tournament["id"]

    tab = client.get(f"/api/v1/tournaments/{tid}/speaker-tab").json()
    assert tab == {"minimum_speeches": 0, "rows": []}

    assert client.get(f"/api/v1/tournaments/{tid}/team-standings").json() == []
    assert client.get(f"/api/v1/tournaments/{tid}/motion-stats").json() == []

    summary = client.get(f"/api/v1/tournaments/{tid}/summary").json()
    assert summary["debates"] == 0
    assert summary["mean_speaker_score"] is None
    assert summary["prop_win_rate"] is None

    side = client.get(f"/api/v1/tournaments/{tid}/side-stats").json()
    assert side == {"overall": {"prop_wins": 0, "opp_wins": 0, "prop_win_rate": None}, "by_round": []}

    refresh = client.post("/api/v1/stats/refresh").json()
    assert refresh["debater_profiles"] == 0
    assert refresh["judge_profiles"] == 0

    distribution = client.get("/api/v1/stats/global-distribution").json()
    assert distribution == {"count": 0, "mean": None, "stdev": None, "q1": None, "median": None, "q3": None}


# --- broader endpoint smoke coverage ---


def test_tournament_endpoints_smoke(client: TestClient) -> None:
    data = build_tournament(client, prelim_rounds=3, elim_rounds=1, teams=4)
    tid = data["tournament"]["id"]

    standings = client.get(f"/api/v1/tournaments/{tid}/team-standings").json()
    assert standings[0]["team_id"] == data["teams"][0]["team"]["id"]
    assert standings[0]["rank"] == 1
    assert standings[0]["prelim_wins"] == 3

    summary = client.get(f"/api/v1/tournaments/{tid}/summary").json()
    assert summary["teams"] == 4
    assert summary["debates"] == 3 * 2 + 2  # 3 prelim rounds x 2 debates + 1 elim round x 2 debates

    side = client.get(f"/api/v1/tournaments/{tid}/side-stats").json()
    assert side["overall"]["prop_wins"] + side["overall"]["opp_wins"] == summary["debates"]

    motions = client.get(f"/api/v1/tournaments/{tid}/motion-stats").json()
    assert len(motions) == 4  # one per round


def test_global_and_institution_endpoints_smoke(client: TestClient) -> None:
    # 3 prelim rounds = a full round-robin for 4 teams, so team 0 is guaranteed to face team 1.
    data = build_tournament(client, prelim_rounds=3, elim_rounds=0, teams=4)
    assert client.post("/api/v1/stats/refresh").status_code == 200

    distribution = client.get("/api/v1/stats/global-distribution").json()
    assert distribution["count"] > 0

    leaderboard = client.get("/api/v1/stats/speaker-leaderboard?limit=5").json()
    assert len(leaderboard) <= 5
    assert leaderboard == sorted(leaderboard, key=lambda e: -e["average"])

    champion = data["teams"][0]["debaters"][0]
    runner_up = data["teams"][1]["debaters"][0]
    h2h = client.get(f"/api/v1/debaters/{champion['id']}/head-to-head").json()
    assert any(r["opponent_id"] == runner_up["id"] for r in h2h)

    institution = client.post("/api/v1/institutions", json={"name": "Big Uni"}).json()
    stats_resp = client.get(f"/api/v1/institutions/{institution['id']}/stats")
    assert stats_resp.status_code == 200
    assert stats_resp.json()["teams"] == 0

    profiles = client.get("/api/v1/stats/debater-profiles?sort_by=speaker_rank").json()
    assert profiles[0]["speaker_rank"] == 1
