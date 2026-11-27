from fastapi.testclient import TestClient
from sqlmodel import Session

from tests.helpers import assert_constant_queries, build_tournament

API = "/api/v1"


def _school(client: TestClient, name: str) -> dict:
    return client.post(f"{API}/institutions", json={"name": name, "kind": "school", "district": "Colombo"}).json()


def _assign(client: TestClient, data: dict, mapping: dict[int, dict]) -> None:
    for index, school in mapping.items():
        team = data["teams"][index]["team"]
        assert client.patch(f"{API}/teams/{team['id']}", json={"institution_id": school["id"]}).status_code == 200
        for debater in data["teams"][index]["debaters"]:
            client.patch(f"{API}/debaters/{debater['id']}", json={"institution_id": school["id"]})


def test_institution_page_titles_finals_and_history(client: TestClient) -> None:
    data = build_tournament(client, teams=4, prelim_rounds=3, elim_rounds=2, slug="inst-cup")
    champion_school, other_school = _school(client, "Champion College"), _school(client, "Other College")
    # Team 0 won, team 2 lost the final, team 1 lost a semi.
    _assign(client, data, {0: champion_school, 1: other_school})
    client.post(
        f"{API}/admin/school-participations",
        json={"institution_id": champion_school["id"], "year": 2019, "event_name": "Old Cup", "teams": 2, "notes": "INTERNAL-NOTE"},
    )
    client.post(f"{API}/admin/school-achievements", json={"institution_id": champion_school["id"], "year": 2019, "title": "SECRET-ACHIEVEMENT"})
    client.post(f"{API}/stats/refresh")

    resp = client.get(f"{API}/public/institutions/{champion_school['id']}")
    assert resp.status_code == 200
    assert "INTERNAL-NOTE" not in resp.text and "SECRET-ACHIEVEMENT" not in resp.text
    page = resp.json()

    assert page["institution"]["name"] == "Champion College"
    summary = page["summary"]
    assert summary["titles"] == 1 and summary["finals"] == 1 and summary["breaks"]["value"] == 1
    assert summary["tournaments"] == 1 and summary["teams"] == 1 and summary["debaters"] == 3
    assert summary["record"] == {"wins": 5, "losses": 0, "rate": 100.0}

    sources = {h["source"]: h for h in page["history"]}
    assert sources["imported"]["best_finish"] == "champion"
    assert sources["manual"]["event_name"] == "Old Cup" and "notes" not in sources["manual"]
    assert [t["finish"] for t in page["teams"]] == ["champion"]
    assert len(page["teams"][0]["speakers"]) == 3
    assert len(page["roster"]) == 3 and page["roster"][0]["rounds"] == 5
    assert page["series"][0]["win_rate"] == 100.0 and page["series"][0]["mean_speaks"] is not None
    assert page["judges"] == []

    other = client.get(f"{API}/public/institutions/{other_school['id']}").json()
    assert other["summary"]["titles"] == 0 and other["summary"]["finals"] == 0
    assert other["teams"][0]["finish"] == "semi"
    assert other["summary"]["record"]["wins"] == 2  # beat teams 2 and 3 in prelims, lost to team 0 twice


def test_intra_school_debates_do_not_count_toward_the_record(client: TestClient) -> None:
    data = build_tournament(client, teams=4, prelim_rounds=3, elim_rounds=2, slug="intra-cup")
    school = _school(client, "Twin College")
    _assign(client, data, {0: school, 2: school})  # they meet in the final
    page = client.get(f"{API}/public/institutions/{school['id']}").json()
    # Both teams: 3 prelims + a semi, plus their meeting in the prelims and the final, which
    # are excluded: (3 + 1 - 1) + (3 + 1 - 1) debates that count.
    record = page["summary"]["record"]
    assert page["summary"]["titles"] == 1 and page["summary"]["finals"] == 2
    assert record["wins"] + record["losses"] == 6
    assert record["wins"] == 5 and record["losses"] == 1  # team 2's only loss is to team 1


def test_institution_judges_and_unknown(client: TestClient) -> None:
    data = build_tournament(client, teams=4, prelim_rounds=2, elim_rounds=1, slug="ij-cup")
    school = _school(client, "Judging College")
    client.patch(f"{API}/judges/{data['judges'][0]['id']}", json={"institution_id": school["id"]})
    page = client.get(f"{API}/public/institutions/{school['id']}").json()
    assert page["judges"][0]["debates"] == 6 and page["judges"][0]["tournaments"] == 1
    assert client.get(f"{API}/public/institutions/9999").status_code == 404


def test_institution_page_query_count_is_constant(client: TestClient, session: Session) -> None:
    counter = {"n": 0}

    def url(d) -> str:
        counter["n"] += 1
        school = _school(client, f"Query College {counter['n']}")
        _assign(client, d, {0: school, 1: school})
        return f"{API}/public/institutions/{school['id']}"

    assert_constant_queries(client, session, url)
