from fastapi.testclient import TestClient
from sqlmodel import Session

from tests.helpers import assert_constant_queries, build_tournament

API = "/api/v1"


def _built(client: TestClient):
    return build_tournament(client, teams=4, prelim_rounds=3, elim_rounds=2, slug="deb-cup")


def test_debater_page_own_history_and_summary(client: TestClient) -> None:
    data = _built(client)
    star = data["teams"][0]["debaters"][0]
    page = client.get(f"{API}/public/debaters/{star['id']}").json()

    assert page["debater"] == {"id": star["id"], "name": "Team 0 Speaker 0"}
    assert page["institution"] is None
    assert page["profile_computed_at"] is None  # /stats/refresh never ran: not a 404

    summary = page["summary"]
    assert summary["tournaments"] == 1
    assert summary["debates"] == 3 + 2
    assert summary["prelim"] == {"wins": 3, "losses": 0, "rate": 100.0}
    assert summary["elim"] == {"wins": 2, "losses": 0, "rate": 100.0}
    # Debater 0 speaks first (76.0 on every ballot): the tab score is that, prelims only.
    assert summary["average"]["value"] == 76.0
    assert summary["average"]["field_mean"] is None  # no materialized profiles yet
    assert summary["speeches"] == 3
    assert summary["speaker_rank"] is None
    assert summary["sides"]["prop"]["wins"] + summary["sides"]["opp"]["wins"] == 5

    (row,) = page["tournaments"]
    assert row["team"]["name"] == "Team 0"
    assert [p["name"] for p in row["partners"]] == ["Team 0 Speaker 1", "Team 0 Speaker 2"]
    assert row["finish"] == "champion"
    assert row["rank"] == 1 and row["team_rank"] == 1
    assert row["teams"] == 4

    speeches = page["speeches"]
    assert len(speeches) == 2 * (3 + 2)  # first speaker and reply in each of five debates
    assert speeches[0]["round"]["seq"] == 5  # newest first
    assert all(s["won"] is True for s in speeches)
    assert all(s["margin"] is not None and s["opponent"]["name"] for s in speeches)
    assert speeches[-1]["motion"] == "Motion 1"

    cells = {(c["side"], c["position"]): c for c in page["positions"]}
    assert len(cells) == 8
    assert cells[("prop", 1)]["count"] + cells[("opp", 1)]["count"] == 3  # prelims only
    assert [p["debater"]["name"] for p in page["partners"]] == ["Team 0 Speaker 1", "Team 0 Speaker 2"]


def test_debater_page_uses_profile_after_refresh(client: TestClient) -> None:
    data = _built(client)
    star = data["teams"][0]["debaters"][0]
    client.post(f"{API}/stats/refresh")
    page = client.get(f"{API}/public/debaters/{star['id']}").json()
    assert page["profile_computed_at"] is not None
    avg = page["summary"]["average"]
    assert avg["field_mean"] is not None and avg["delta"] is not None
    assert avg["percentile"] is not None
    assert page["summary"]["speaker_rank"] == 1


def test_debater_page_without_history_and_404(client: TestClient) -> None:
    debater = client.post(f"{API}/debaters", json={"full_name": "Nobody"}).json()
    page = client.get(f"{API}/public/debaters/{debater['id']}").json()
    assert page["tournaments"] == [] and page["speeches"] == []
    assert page["summary"]["average"]["value"] is None
    assert client.get(f"{API}/public/debaters/99999").status_code == 404


def test_debater_page_has_no_personal_fields(client: TestClient) -> None:
    debater = client.post(
        f"{API}/debaters", json={"full_name": "Private", "email": "p@secret.example", "phone": "0771112222"}
    ).json()
    text = client.get(f"{API}/public/debaters/{debater['id']}").text
    assert "secret.example" not in text and "0771112222" not in text


def test_debater_page_query_count_is_constant(client: TestClient, session: Session) -> None:
    assert_constant_queries(
        client, session, lambda d: f"{API}/public/debaters/{d['teams'][0]['debaters'][0]['id']}"
    )
