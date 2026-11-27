from fastapi.testclient import TestClient
from sqlmodel import Session

from tests.helpers import assert_constant_queries, build_tournament

API = "/api/v1"


def _built(client: TestClient, slug: str = "dbt-cup"):
    return build_tournament(client, teams=4, prelim_rounds=2, elim_rounds=2, slug=slug)


def test_explorer_filters_and_paging(client: TestClient) -> None:
    data = _built(client)
    tid = data["tournament"]["id"]
    team0 = data["teams"][0]["team"]["id"]

    everything = client.get(f"{API}/public/debates").json()
    assert everything["total"] == 2 * 2 + 2 + 1
    assert everything["limit"] == 25 and everything["offset"] == 0
    first = everything["items"][0]
    assert first["tournament"]["id"] == tid
    assert first["round"]["seq"] == 1
    assert first["motion"] == "Motion 1"
    assert first["votes"] == {"prop": 2, "opp": 1} or first["votes"] == {"prop": 1, "opp": 2}
    assert first["chair"] is None

    paged = client.get(f"{API}/public/debates", params={"limit": 2, "offset": 2}).json()
    assert len(paged["items"]) == 2 and paged["total"] == 7

    for params, expected in (
        ({"tournament_id": tid}, 7),
        ({"stage": "elim"}, 3),
        ({"stage": "prelim"}, 4),
        ({"team_id": team0}, 4),
        ({"q": "elim motion"}, 3),
        ({"q": "%"}, 0),  # LIKE wildcards are escaped
        ({"judge_id": data["judges"][0]["id"]}, 7),
    ):
        assert client.get(f"{API}/public/debates", params=params).json()["total"] == expected, params

    debater = data["teams"][0]["debaters"][0]["id"]
    assert client.get(f"{API}/public/debates", params={"debater_id": debater}).json()["total"] == 4
    assert client.get(f"{API}/public/debates", params={"limit": 101}).status_code == 422
    assert client.get(f"{API}/public/debates", params={"stage": "nope"}).status_code == 422


def test_explorer_institution_filter(client: TestClient) -> None:
    data = _built(client)
    inst = client.post(f"{API}/institutions", json={"name": "Filter College"}).json()
    client.patch(f"{API}/teams/{data['teams'][1]['team']['id']}", json={"institution_id": inst["id"]})
    result = client.get(f"{API}/public/debates", params={"institution_id": inst["id"]}).json()
    assert result["total"] == 3
    assert result["items"][0]["prop_team"]["institution"] is not None or result["items"][0]["opp_team"]["institution"] is not None


def test_debate_detail_sheet_and_neighbours(client: TestClient) -> None:
    data = _built(client)
    debates = client.get(f"{API}/public/debates").json()["items"]
    first, second = debates[0], debates[1]
    detail = client.get(f"{API}/public/debates/{first['id']}").json()

    assert detail["motion"]["text"] == "Motion 1"
    assert detail["previous_debate_id"] is None
    assert detail["next_debate_id"] == second["id"]
    assert len(detail["panel"]) == 3
    assert {p["winner"] for p in detail["panel"]} == {"prop", "opp"}
    assert len(detail["speakers"]) == 8
    top = detail["speakers"][0]
    assert top["side"] == "prop" and top["position"] == 1
    assert len(top["by_judge"]) == 3
    assert top["average"] == 76.0
    assert detail["prop_total"] == 76.0 + 74.5 + 75.0 + 38.0
    assert detail["margin"] == detail["prop_total"] - detail["opp_total"]
    assert client.get(f"{API}/public/debates/99999").status_code == 404
    last = client.get(f"{API}/public/debates/{debates[-1]['id']}").json()
    assert last["next_debate_id"] is None and last["previous_debate_id"] is not None
    assert data  # built


def test_discarded_ballots_are_flagged_and_not_counted(client: TestClient) -> None:
    _built(client)
    debate_id = client.get(f"{API}/public/debates").json()["items"][0]["id"]
    ballots = client.get(f"{API}/debates/{debate_id}/ballots").json()
    majority = ballots[0]
    assert client.patch(f"{API}/ballots/{majority['id']}", json={"discarded": True}).status_code == 200

    detail = client.get(f"{API}/public/debates/{debate_id}").json()
    assert sum(1 for p in detail["panel"] if p["discarded"]) == 1
    assert detail["votes"]["prop"] + detail["votes"]["opp"] == 2
    top = detail["speakers"][0]
    assert sum(1 for j in top["by_judge"] if j["discarded"]) == 1
    listed = client.get(f"{API}/public/debates").json()["items"][0]
    assert listed["votes"]["prop"] + listed["votes"]["opp"] == 2

    # The legacy result endpoint leaves discarded ballots out too.
    result = client.get(f"{API}/debates/{debate_id}/result").json()
    assert len(result["ballots"]) == 2
    assert all(b["ballot_id"] != majority["id"] for b in result["ballots"])


def test_debate_endpoints_query_counts_are_constant(client: TestClient, session: Session) -> None:
    assert_constant_queries(client, session, lambda d: f"{API}/public/debates?tournament_id={d['tournament']['id']}")

    def detail_url(d) -> str:
        return f"{API}/public/debates/{d['elim_rounds'][-1]['debates'][-1]['debate']['id']}"

    assert_constant_queries(client, session, detail_url, tag='-detail')
