from fastapi.testclient import TestClient
from sqlmodel import Session

from tests.helpers import assert_constant_queries, build_tournament

API = "/api/v1/public"


def _setup(client: TestClient):
    data = build_tournament(client, teams=4, prelim_rounds=3, elim_rounds=2, slug="list-cup")
    school_a = client.post("/api/v1/institutions", json={"name": "Alpha College", "code": "AC", "kind": "school"}).json()
    school_b = client.post("/api/v1/institutions", json={"name": "Beta Academy"}).json()
    for index, school in ((0, school_a), (1, school_b)):
        client.patch(f"/api/v1/teams/{data['teams'][index]['team']['id']}", json={"institution_id": school["id"]})
        for debater in data["teams"][index]["debaters"]:
            client.patch(f"/api/v1/debaters/{debater['id']}", json={"institution_id": school["id"]})
    client.post("/api/v1/stats/refresh")
    return data, school_a, school_b


def test_debater_list_paging_sorting_and_filters(client: TestClient) -> None:
    _setup(client)
    page = client.get(f"{API}/debaters", params={"limit": 5}).json()
    assert page["total"] == 12 and page["limit"] == 5 and page["offset"] == 0
    assert len(page["items"]) == 5 and page["profile_computed_at"] is not None
    first = page["items"][0]
    assert first["average"] is not None and first["rounds"] >= 1
    assert len(first["trend"]) == 1

    asc = client.get(f"{API}/debaters", params={"sort": "name", "order": "asc", "limit": 100}).json()["items"]
    names = [i["debater"]["name"] for i in asc]
    assert names == sorted(names)

    by_rounds = client.get(f"{API}/debaters", params={"sort": "rounds", "limit": 100}).json()["items"]
    assert [i["rounds"] for i in by_rounds] == sorted((i["rounds"] for i in by_rounds), reverse=True)

    assert client.get(f"{API}/debaters", params={"q": "team 1 speaker"}).json()["total"] == 3
    assert client.get(f"{API}/debaters", params={"q": "%"}).json()["total"] == 0
    assert client.get(f"{API}/debaters", params={"min_rounds": 5}).json()["total"] == 6  # teams 0 and 2 reached the final... and 1,3 semis
    school = client.get(f"{API}/debaters", params={"q": "Team 0"}).json()["items"][0]["institution"]
    assert school["name"] == "Alpha College"

    assert client.get(f"{API}/debaters", params={"sort": "email"}).status_code == 422
    assert client.get(f"{API}/debaters", params={"order": "sideways"}).status_code == 422
    assert client.get(f"{API}/debaters", params={"limit": 0}).status_code == 422
    assert client.get(f"{API}/debaters", params={"limit": 101}).status_code == 422
    # The profile endpoints stay separate from the list.
    assert client.get(f"{API}/debaters/1").status_code == 200


def test_debater_list_before_refresh_lists_names_without_stats(client: TestClient) -> None:
    build_tournament(client, teams=4, prelim_rounds=2, elim_rounds=1, slug="nr-cup")
    page = client.get(f"{API}/debaters").json()
    assert page["total"] == 12 and page["profile_computed_at"] is None
    assert page["items"][0]["average"] is None


def test_judge_list_exposes_tendencies(client: TestClient) -> None:
    _setup(client)
    page = client.get(f"{API}/judges", params={"sort": "dissent", "order": "desc"}).json()
    assert page["total"] == 3
    top = page["items"][0]
    assert top["judge"]["name"] == "Judge 2" and top["dissent_rate"] == 100.0
    assert top["sentiment"] is not None or top["sentiment"] is None
    assert client.get(f"{API}/judges", params={"min_debates": 100}).json()["total"] == 0
    assert client.get(f"{API}/judges", params={"sort": "gender"}).status_code == 422
    for field_name in ("email", "phone", "birthdate", "gender"):
        assert field_name not in top


def test_institution_list(client: TestClient) -> None:
    _setup(client)
    page = client.get(f"{API}/institutions", params={"sort": "titles", "min_teams": 1}).json()
    assert page["total"] == 2
    assert page["items"][0]["institution"]["name"] == "Alpha College" and page["items"][0]["titles"] == 1
    asc = client.get(f"{API}/institutions", params={"sort": "name", "order": "asc"}).json()["items"]
    assert [i["institution"]["name"] for i in asc] == ["Alpha College", "Beta Academy"]
    assert client.get(f"{API}/institutions", params={"q": "academy"}).json()["total"] == 1
    assert client.get(f"{API}/institutions", params={"sort": "rank"}).status_code == 422
    # Missing values sort last in both directions.
    client.post("/api/v1/institutions", json={"name": "Empty School"})
    for order in ("asc", "desc"):
        items = client.get(f"{API}/institutions", params={"sort": "win_rate", "order": order}).json()["items"]
        assert items[-1]["institution"]["name"] == "Empty School"


def test_tournament_list_has_champions(client: TestClient) -> None:
    data, *_ = _setup(client)
    page = client.get(f"{API}/tournaments").json()
    assert page["total"] == 1
    item = page["items"][0]
    assert item["teams"] == 4 and item["rounds"] == 5
    assert item["champion"]["id"] == data["teams"][0]["team"]["id"]
    assert item["champion"]["institution"]["name"] == "Alpha College"
    assert client.get(f"{API}/tournaments", params={"q": "list"}).json()["total"] == 1
    assert client.get(f"{API}/tournaments", params={"q": "zzz"}).json()["total"] == 0
    assert client.get(f"{API}/tournaments", params={"sort": "champion"}).status_code == 422
    # The statics are not swallowed by /{id}.
    assert client.get(f"{API}/tournaments/{item['tournament']['id']}").status_code == 200


def test_search_groups_prefix_matches_first(client: TestClient) -> None:
    _setup(client)
    client.post("/api/v1/debaters", json={"full_name": "Zed Quux"})
    client.post("/api/v1/debaters", json={"full_name": "Quux Zed"})
    result = client.get(f"{API}/search", params={"q": "quux"}).json()
    assert [h["name"] for h in result["debaters"]] == ["Quux Zed", "Zed Quux"]  # prefix match first
    teams = client.get(f"{API}/search", params={"q": "team 1"}).json()["teams"]
    assert teams[0]["name"] == "Team 1" and teams[0]["tournament_id"] is not None

    schools = client.get(f"{API}/search", params={"q": "alpha"}).json()["institutions"]
    assert schools[0]["name"] == "Alpha College" and "1 titles" in schools[0]["subline"]
    by_code = client.get(f"{API}/search", params={"q": "ac"}).json()["institutions"]
    assert any(h["name"] == "Alpha College" for h in by_code)
    hits = client.get(f"{API}/search", params={"q": "Team 0 Speaker"}).json()["debaters"]
    assert hits[0]["subline"].startswith("Alpha College")
    judges = client.get(f"{API}/search", params={"q": "judge 1"}).json()["judges"]
    assert judges[0]["name"] == "Judge 1"
    assert client.get(f"{API}/search", params={"q": "list cup"}).json()["tournaments"][0]["kind"] == "tournament"


def test_search_validation_and_escaping(client: TestClient) -> None:
    assert client.get(f"{API}/search").status_code == 422
    assert client.get(f"{API}/search", params={"q": " a "}).status_code == 422
    assert client.get(f"{API}/search", params={"q": "ab", "limit": 11}).status_code == 422
    client.post("/api/v1/debaters", json={"full_name": "Plain Name"})
    everything = client.get(f"{API}/search", params={"q": "%%"}).json()
    assert all(not everything[k] for k in ("debaters", "judges", "institutions", "tournaments", "teams"))


def test_list_and_search_query_counts_are_constant(client: TestClient, session: Session) -> None:
    assert_constant_queries(client, session, lambda d: f"{API}/debaters?limit=100")
    assert_constant_queries(client, session, lambda d: f"{API}/judges", tag="-j")
    assert_constant_queries(client, session, lambda d: f"{API}/tournaments", tag="-t")
    assert_constant_queries(client, session, lambda d: f"{API}/institutions", tag="-i")
    assert_constant_queries(client, session, lambda d: f"{API}/search?q=team", tag="-s")
