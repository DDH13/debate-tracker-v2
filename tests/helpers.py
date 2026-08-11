from fastapi.testclient import TestClient


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
        client.post("/api/v1/debaters", json={"full_name": f"Prop Speaker {i}"}).json()
        for i in range(1, 4)
    ]
    opp_debaters = [
        client.post("/api/v1/debaters", json={"full_name": f"Opp Speaker {i}"}).json()
        for i in range(1, 4)
    ]
    for debater in prop_debaters:
        client.post(
            f"/api/v1/teams/{prop_team['id']}/members",
            json={"debater_id": debater["id"]},
        )
    for debater in opp_debaters:
        client.post(
            f"/api/v1/teams/{opp_team['id']}/members",
            json={"debater_id": debater["id"]},
        )

    round_ = client.post(
        f"/api/v1/tournaments/{tournament['id']}/rounds", json={"seq": 1}
    ).json()
    debate = client.post(
        f"/api/v1/rounds/{round_['id']}/debates",
        json={"prop_team_id": prop_team["id"], "opp_team_id": opp_team["id"]},
    ).json()

    judges = [
        client.post("/api/v1/judges", json={"full_name": f"Judge {i}"}).json() for i in range(1, 4)
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
        {"debater_id": prop_debaters[0]["id"], "side": "prop", "position": 1, "final_score": 76.0},
        {"debater_id": prop_debaters[1]["id"], "side": "prop", "position": 2, "final_score": 74.5},
        {"debater_id": prop_debaters[2]["id"], "side": "prop", "position": 3, "final_score": 75.0},
        {
            "debater_id": prop_debaters[prop_reply_idx]["id"],
            "side": "prop",
            "position": 4,
            "final_score": 38.0,
        },
        {"debater_id": opp_debaters[0]["id"], "side": "opp", "position": 1, "final_score": 75.5},
        {"debater_id": opp_debaters[1]["id"], "side": "opp", "position": 2, "final_score": 75.0},
        {"debater_id": opp_debaters[2]["id"], "side": "opp", "position": 3, "final_score": 74.0},
        {
            "debater_id": opp_debaters[opp_reply_idx]["id"],
            "side": "opp",
            "position": 4,
            "final_score": 37.5,
        },
    ]


def _full_score_sheet_with_categories(
    prop_debaters: list[dict], opp_debaters: list[dict], prop_reply_idx: int = 0, opp_reply_idx: int = 1
) -> list[dict]:
    return [
        {
            "debater_id": prop_debaters[0]["id"],
            "side": "prop",
            "position": 1,
            "content": 30.5,
            "style": 30.5,
            "strategy": 15.0,
        },
        {
            "debater_id": prop_debaters[1]["id"],
            "side": "prop",
            "position": 2,
            "content": 30.0,
            "style": 29.5,
            "strategy": 15.0,
        },
        {
            "debater_id": prop_debaters[2]["id"],
            "side": "prop",
            "position": 3,
            "content": 30.0,
            "style": 30.0,
            "strategy": 15.0,
        },
        {
            "debater_id": prop_debaters[prop_reply_idx]["id"],
            "side": "prop",
            "position": 4,
            "content": 15.0,
            "style": 15.0,
            "strategy": 8.0,
        },
        {
            "debater_id": opp_debaters[0]["id"],
            "side": "opp",
            "position": 1,
            "content": 30.0,
            "style": 30.5,
            "strategy": 15.0,
        },
        {
            "debater_id": opp_debaters[1]["id"],
            "side": "opp",
            "position": 2,
            "content": 30.0,
            "style": 30.0,
            "strategy": 15.0,
        },
        {
            "debater_id": opp_debaters[2]["id"],
            "side": "opp",
            "position": 3,
            "content": 29.5,
            "style": 29.5,
            "strategy": 15.0,
        },
        {
            "debater_id": opp_debaters[opp_reply_idx]["id"],
            "side": "opp",
            "position": 4,
            "content": 15.0,
            "style": 15.0,
            "strategy": 7.5,
        },
    ]


def _round_robin_pairs(n: int, round_index: int) -> list[tuple[int, int]]:
    """Standard circle-method round-robin pairing for `n` (even) players, 0-indexed round."""
    others = list(range(1, n))
    shift = round_index % (n - 1)
    if shift:
        others = others[-shift:] + others[:-shift]
    circle = [0] + others
    half = n // 2
    return [(circle[i], circle[n - 1 - i]) for i in range(half)]


def build_tournament(
    client: TestClient,
    *,
    prelim_rounds: int = 4,
    elim_rounds: int = 1,
    teams: int = 8,
    slug: str = "built-cup",
) -> dict:
    """Builds a complete tournament: `teams` teams (3 debaters each), a 3-judge panel voting
    2-1, `prelim_rounds` round-robin-paired preliminary rounds, followed by `elim_rounds`
    single-elimination rounds seeded on prelim win count. The lower-indexed team of any pair
    always wins (2-1 on the panel), giving a deterministic win/speaks structure to assert on.
    """
    assert teams % 2 == 0

    tournament = client.post(
        "/api/v1/tournaments", json={"name": slug.replace("-", " ").title(), "slug": slug}
    ).json()
    tid = tournament["id"]

    team_objs = []
    for i in range(teams):
        team = client.post(f"/api/v1/tournaments/{tid}/teams", json={"name": f"Team {i}"}).json()
        debaters = [
            client.post("/api/v1/debaters", json={"full_name": f"Team {i} Speaker {p}"}).json()
            for p in range(3)
        ]
        for debater in debaters:
            client.post(f"/api/v1/teams/{team['id']}/members", json={"debater_id": debater["id"]})
        team_objs.append({"team": team, "debaters": debaters})

    judges = [client.post("/api/v1/judges", json={"full_name": f"Judge {i}"}).json() for i in range(3)]

    def _run_debate(round_id: int, prop_index: int, opp_index: int) -> dict:
        debate = client.post(
            f"/api/v1/rounds/{round_id}/debates",
            json={
                "prop_team_id": team_objs[prop_index]["team"]["id"],
                "opp_team_id": team_objs[opp_index]["team"]["id"],
            },
        ).json()
        for judge in judges:
            client.post(f"/api/v1/debates/{debate['id']}/judges", json={"judge_id": judge["id"]})

        winner_index = min(prop_index, opp_index)
        majority_winner = "prop" if winner_index == prop_index else "opp"
        minority_winner = "opp" if majority_winner == "prop" else "prop"
        winners = [majority_winner, majority_winner, minority_winner]
        for judge, winner in zip(judges, winners):
            resp = client.post(
                f"/api/v1/debates/{debate['id']}/ballots",
                json={
                    "judge_id": judge["id"],
                    "winner": winner,
                    "scores": _full_score_sheet(
                        team_objs[prop_index]["debaters"], team_objs[opp_index]["debaters"]
                    ),
                },
            )
            assert resp.status_code == 201, resp.text
        return {"debate": debate, "prop_index": prop_index, "opp_index": opp_index, "winner_index": winner_index}

    wins = [0] * teams
    prelim_round_records = []
    for round_index in range(prelim_rounds):
        seq = round_index + 1
        round_ = client.post(
            f"/api/v1/tournaments/{tid}/rounds", json={"seq": seq, "name": f"Round {seq}"}
        ).json()
        client.put(f"/api/v1/rounds/{round_['id']}/motion", json={"text": f"Motion {seq}"})

        round_debates = []
        for a, b in _round_robin_pairs(teams, round_index):
            prop_index, opp_index = (a, b) if a < b else (b, a)
            outcome = _run_debate(round_["id"], prop_index, opp_index)
            wins[outcome["winner_index"]] += 1
            round_debates.append(outcome)
        prelim_round_records.append({"round": round_, "debates": round_debates})

    seeding = sorted(range(teams), key=lambda i: (-wins[i], i))

    elim_round_records = []
    remaining = seeding
    for elim_index in range(elim_rounds):
        seq = prelim_rounds + elim_index + 1
        name = "Grand Final" if len(remaining) <= 2 else f"Elim Round {elim_index + 1}"
        round_ = client.post(
            f"/api/v1/tournaments/{tid}/rounds",
            json={"seq": seq, "name": name, "isElimination": True},
        ).json()
        client.put(f"/api/v1/rounds/{round_['id']}/motion", json={"text": f"Elim Motion {seq}"})

        round_debates = []
        next_remaining = []
        for pair_index in range(len(remaining) // 2):
            prop_index = remaining[pair_index * 2]
            opp_index = remaining[pair_index * 2 + 1]
            outcome = _run_debate(round_["id"], prop_index, opp_index)
            round_debates.append(outcome)
            next_remaining.append(outcome["winner_index"])
        elim_round_records.append({"round": round_, "debates": round_debates})
        remaining = next_remaining

    return {
        "tournament": tournament,
        "teams": team_objs,
        "judges": judges,
        "prelim_wins": wins,
        "prelim_rounds": prelim_round_records,
        "elim_rounds": elim_round_records,
    }
