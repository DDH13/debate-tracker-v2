from fastapi.testclient import TestClient
from sqlmodel import Session

from tests.helpers import assert_constant_queries, build_tournament

API = "/api/v1"


def _built(client: TestClient):
    return build_tournament(client, teams=4, prelim_rounds=3, elim_rounds=2, slug="pub-cup")


def test_tournament_page_bundles_everything(client: TestClient) -> None:
    data = _built(client)
    tid = data["tournament"]["id"]
    page = client.get(f"{API}/public/tournaments/{tid}").json()

    assert page["tournament"]["name"] == "Pub Cup"
    assert page["summary"]["teams"] == 4
    assert page["summary"]["debates"] == 3 * 2 + 2 + 1
    team0, team1, team2, team3 = (t["team"]["id"] for t in data["teams"])

    assert page["champion"]["id"] == team0
    assert page["finalist"]["id"] == team2
    (bracket,) = page["brackets"]
    assert bracket["primary"] and bracket["label"] == "Grand Final"
    assert [len(r["debates"]) for r in bracket["rounds"]] == [2, 1]
    final = bracket["rounds"][1]["debates"][0]
    assert final["winner_team_id"] == team0
    assert final["votes"] == {"prop": 2, "opp": 1}
    assert final["margin"] is not None

    standings = {row["team"]["id"]: row for row in page["standings"]}
    assert standings[team0]["finish"] == "champion"
    assert standings[team2]["finish"] == "finalist"
    assert standings[team1]["finish"] == "semi" and standings[team3]["finish"] == "semi"
    assert standings[team0]["rank"] == 1
    assert len(standings[team0]["speakers"]) == 3
    assert standings[team0]["prelim"] == {"wins": 3, "losses": 0, "rate": 100.0}

    assert page["speakers"][0]["rank"] == 1
    assert page["speakers"][0]["team"] is not None
    assert [r["round"]["seq"] for r in page["rounds"]] == [1, 2, 3, 4, 5]
    assert page["rounds"][0]["motion"] == "Motion 1"
    assert len(page["judges"]) == 3
    assert all(j["debates"] == 9 for j in page["judges"])
    assert page["judges"][0]["highest_round"]["seq"] == 5


def test_standings_rank_by_prelim_wins_then_total_speaks(client: TestClient) -> None:
    data = _built(client)
    tid = data["tournament"]["id"]
    standings = client.get(f"{API}/tournaments/{tid}/team-standings").json()
    assert [s["prelim_wins"] for s in standings] == sorted((s["prelim_wins"] for s in standings), reverse=True)
    assert all("institution_id" in s for s in standings)
    assert [s["rank"] for s in standings] == [1, 2, 3, 4]


def test_unknown_tournament_is_404(client: TestClient) -> None:
    assert client.get(f"{API}/public/tournaments/999").status_code == 404


def test_tournament_page_without_rounds(client: TestClient) -> None:
    tid = client.post(f"{API}/tournaments", json={"name": "Empty", "slug": "empty-pub"}).json()["id"]
    page = client.get(f"{API}/public/tournaments/{tid}").json()
    assert page["champion"] is None and page["brackets"] == [] and page["standings"] == []


def test_tournament_page_is_open_and_cacheable(anon_client: TestClient, client: TestClient) -> None:
    tid = _built(client)["tournament"]["id"]
    resp = anon_client.get(f"{API}/public/tournaments/{tid}")
    assert resp.status_code == 200
    assert "max-age=60" in resp.headers["cache-control"]


def test_tournament_page_query_count_is_constant(client: TestClient, session: Session) -> None:
    assert_constant_queries(client, session, lambda d: f"{API}/public/tournaments/{d['tournament']['id']}")


def test_motion_stats_query_count_is_constant(client: TestClient, session: Session) -> None:
    assert_constant_queries(client, session, lambda d: f"{API}/tournaments/{d['tournament']['id']}/motion-stats")
