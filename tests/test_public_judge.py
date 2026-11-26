from fastapi.testclient import TestClient
from sqlmodel import Session

from tests.helpers import assert_constant_queries, build_tournament

API = "/api/v1"


def _built(client: TestClient):
    return build_tournament(client, teams=4, prelim_rounds=3, elim_rounds=2, slug="jdg-cup")


def test_judge_page_minority_judge_in_two_one_panels(client: TestClient) -> None:
    data = _built(client)
    majority_judge, _other, minority_judge = data["judges"]
    client.post(f"{API}/stats/refresh")

    minority = client.get(f"{API}/public/judges/{minority_judge['id']}").json()
    assert minority["judge"]["name"] == "Judge 2"
    assert minority["summary"]["debates"] == 9
    assert minority["summary"]["ballots_cast"] == 9
    assert minority["summary"]["dissent_rate"]["value"] == 100.0
    assert minority["decisions"]["minority"] == 9 and minority["decisions"]["with_majority"] == 0
    assert minority["decisions"]["unanimous"] == 0
    assert minority["profile_computed_at"] is not None

    majority = client.get(f"{API}/public/judges/{majority_judge['id']}").json()
    assert majority["summary"]["dissent_rate"]["value"] == 0.0
    assert majority["decisions"]["with_majority"] == 9
    assert majority["summary"]["dissent_rate"]["field_mean"] is not None

    # Judged debates carry the whole panel, with this judge's vote.
    row = minority["debates"][0]
    assert len(row["panel"]) == 3
    assert row["dissented"] is True and row["vote"] in ("prop", "opp")
    assert row["debate"]["round"]["seq"] == 5  # newest first
    assert minority["tournaments"][0]["highest_round"]["seq"] == 5
    assert minority["tournaments"][0]["dissents"] == 9
    assert [c["position"] for c in minority["positions"]] == [1, 2, 3, 4]
    assert minority["positions"][0]["average"] is not None
    assert minority["summary"]["average"]["value"] is not None
    assert minority["summary"]["sentiment"]["field_mean"] is not None or minority["summary"]["sentiment"]["value"] is None


def test_judge_page_before_refresh_and_404(client: TestClient) -> None:
    data = _built(client)
    page = client.get(f"{API}/public/judges/{data['judges'][0]['id']}").json()
    assert page["profile_computed_at"] is None
    assert page["summary"]["sentiment"]["value"] is None
    assert page["summary"]["debates"] == 9
    assert client.get(f"{API}/public/judges/99999").status_code == 404


def test_discarded_ballots_are_not_cast_votes(client: TestClient) -> None:
    data = _built(client)
    judge = data["judges"][0]
    debate_id = data["prelim_rounds"][0]["debates"][0]["debate"]["id"]
    ballot = next(
        b for b in client.get(f"{API}/debates/{debate_id}/ballots").json() if b["judge_id"] == judge["id"]
    )
    client.patch(f"{API}/ballots/{ballot['id']}", json={"discarded": True})
    page = client.get(f"{API}/public/judges/{judge['id']}").json()
    assert page["summary"]["ballots_cast"] == 8
    assert page["summary"]["debates"] == 9  # still on the panel
    discarded_row = next(d for d in page["debates"] if d["debate"]["id"] == debate_id)
    assert discarded_row["vote"] is None and discarded_row["dissented"] is None


def test_judge_page_has_no_personal_fields(client: TestClient) -> None:
    judge = client.post(f"{API}/judges", json={"full_name": "J", "email": "j@secret.example", "gender": "x-gender"}).json()
    text = client.get(f"{API}/public/judges/{judge['id']}").text
    assert "secret.example" not in text and "x-gender" not in text


def test_judge_page_query_count_is_constant(client: TestClient, session: Session) -> None:
    assert_constant_queries(client, session, lambda d: f"{API}/public/judges/{d['judges'][0]['id']}")
