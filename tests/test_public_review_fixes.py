"""Regression tests for the public-stats code-review findings."""

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, delete, select

from app.core.config import settings
from app.models import Debate, DebateJudge, Team, TeamMember
from app.services.public import cache
from tests.helpers import _build_debate_scaffold, _full_score_sheet, assert_constant_queries, build_tournament

API = "/api/v1"
PUBLIC = f"{API}/public"


@pytest.fixture(name="cached")
def cached_fixture(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "public_cache_ttl_seconds", 600)
    cache.clear()
    yield
    cache.clear()


def _school(client: TestClient, name: str) -> dict:
    return client.post(f"{API}/institutions", json={"name": name, "kind": "school"}).json()


def _set_team_school(client: TestClient, data: dict, index: int, school: dict) -> None:
    team = data["teams"][index]["team"]
    assert client.patch(f"{API}/teams/{team['id']}", json={"institution_id": school["id"]}).status_code == 200


# --- 1. a stale cached snapshot never crashes a page ---


def test_pages_survive_a_snapshot_older_than_a_team(client: TestClient, session: Session, cached) -> None:
    data = build_tournament(client, teams=4, prelim_rounds=2, elim_rounds=1, slug="stale-cup")
    school = _school(client, "Stale College")
    _set_team_school(client, data, 0, school)
    tid = data["tournament"]["id"]
    debater_id = data["teams"][0]["debaters"][0]["id"]
    urls = [
        f"{PUBLIC}/debaters/{debater_id}",
        f"{PUBLIC}/institutions/{school['id']}",
        f"{PUBLIC}/tournaments/{tid}",
        f"{PUBLIC}/judges/{data['judges'][0]['id']}",
    ]
    for url in urls:  # primes the tournament snapshot cache
        assert client.get(url).status_code == 200

    # A write the cache never hears about (another worker): a new team at the school, with
    # the debater on it, that the cached snapshot predates.
    late = Team(tournament_id=tid, name="Late Team", institution_id=school["id"])
    session.add(late)
    session.commit()
    session.add(TeamMember(team_id=late.id, debater_id=debater_id))
    session.commit()

    for url in urls:
        assert client.get(url).status_code == 200, url
    page = client.get(f"{PUBLIC}/debaters/{debater_id}").json()
    assert {r["team"]["name"] for r in page["tournaments"]} == {"Team 0", "Late Team"}
    institution = client.get(f"{PUBLIC}/institutions/{school['id']}").json()
    assert {r["team"]["name"] for r in institution["teams"]} == {"Team 0", "Late Team"}


# --- 3. series reconcile with the summary ---


def test_institution_series_excludes_intra_school_debates(client: TestClient) -> None:
    data = build_tournament(client, teams=4, prelim_rounds=3, elim_rounds=2, slug="series-cup")
    school = _school(client, "Series College")
    for index in (0, 2):
        _set_team_school(client, data, index, school)
    page = client.get(f"{PUBLIC}/institutions/{school['id']}").json()
    record = page["summary"]["record"]
    assert sum(p["wins"] for p in page["series"]) == record["wins"] == 5
    assert sum(p["losses"] for p in page["series"]) == record["losses"] == 1


# --- 4. one school rule everywhere ---


def test_debater_school_falls_back_to_latest_team(client: TestClient) -> None:
    data = build_tournament(client, teams=4, prelim_rounds=3, elim_rounds=2, slug="school-cup")
    alpha, beta = _school(client, "Alpha College"), _school(client, "Beta Academy")
    _set_team_school(client, data, 0, alpha)  # debaters themselves keep institution_id NULL
    client.post(f"{API}/stats/refresh")
    team0 = data["teams"][0]["debaters"]

    page = client.get(f"{PUBLIC}/institutions/{alpha['id']}").json()
    assert {r["debater"]["id"] for r in page["roster"]} == {d["id"] for d in team0}
    assert page["summary"]["debaters"] == 3
    assert client.get(f"{PUBLIC}/debaters/{team0[0]['id']}").json()["institution"]["name"] == "Alpha College"

    listed = client.get(f"{PUBLIC}/debaters", params={"institution_id": alpha["id"]}).json()
    assert listed["total"] == 3
    assert {i["institution"]["name"] for i in listed["items"]} == {"Alpha College"}
    hit = client.get(f"{PUBLIC}/search", params={"q": team0[0]["full_name"]}).json()["debaters"][0]
    assert "Alpha College" in hit["subline"]

    # Their most recent team (the newer tournament) is Beta's: Beta wins, Alpha loses them.
    second = client.post(f"{API}/tournaments", json={"name": "Later Cup", "slug": "later-cup"}).json()
    late = client.post(
        f"{API}/tournaments/{second['id']}/teams", json={"name": "Late", "institution_id": beta["id"]}
    ).json()
    client.post(f"{API}/teams/{late['id']}/members", json={"debater_id": team0[0]["id"]})
    assert client.get(f"{PUBLIC}/debaters/{team0[0]['id']}").json()["institution"]["name"] == "Beta Academy"
    assert client.get(f"{PUBLIC}/debaters", params={"institution_id": alpha["id"]}).json()["total"] == 2
    assert client.get(f"{PUBLIC}/debaters", params={"institution_id": beta["id"]}).json()["total"] == 1
    assert client.get(f"{PUBLIC}/institutions/{beta['id']}").json()["summary"]["debaters"] == 1

    # An explicit school on the debater always wins.
    client.patch(f"{API}/debaters/{team0[0]['id']}", json={"institution_id": alpha["id"]})
    assert client.get(f"{PUBLIC}/debaters/{team0[0]['id']}").json()["institution"]["name"] == "Alpha College"


# --- 5. batched fallbacks keep the query count flat ---


def test_debater_page_off_roster_scores_are_batched(client: TestClient, session: Session) -> None:
    def url(data: dict) -> str:
        debater_id = data["teams"][0]["debaters"][0]["id"]
        # Off the roster: scored, but on no team any more.
        session.exec(delete(TeamMember).where(TeamMember.debater_id == debater_id))
        session.commit()
        return f"{PUBLIC}/debaters/{debater_id}"

    assert_constant_queries(client, session, url, tag="-offroster")
    data = build_tournament(client, teams=4, prelim_rounds=3, elim_rounds=1, slug="offroster-cup")
    debater_id = data["teams"][0]["debaters"][0]["id"]
    session.exec(delete(TeamMember).where(TeamMember.debater_id == debater_id))
    session.commit()
    assert len(client.get(f"{PUBLIC}/debaters/{debater_id}").json()["speeches"]) > 0


def test_judge_page_ballots_without_panel_link_are_batched(client: TestClient, session: Session) -> None:
    def url(data: dict) -> str:
        judge_id = data["judges"][0]["id"]
        session.exec(delete(DebateJudge).where(DebateJudge.judge_id == judge_id))
        session.commit()
        return f"{PUBLIC}/judges/{judge_id}"

    assert_constant_queries(client, session, url, tag="-nolink")


# --- 6. a solo-judged debate is not unanimous ---


def test_solo_judged_debate_is_not_unanimous(client: TestClient) -> None:
    s = _build_debate_scaffold(client)
    sheet = _full_score_sheet(s["prop_debaters"], s["opp_debaters"])
    solo = s["judges"][0]
    resp = client.post(
        f"{API}/debates/{s['debate']['id']}/ballots", json={"judge_id": solo["id"], "winner": "prop", "scores": sheet}
    )
    assert resp.status_code == 201
    page = client.get(f"{PUBLIC}/judges/{solo['id']}").json()
    assert page["decisions"]["unanimous"] == 0

    for judge in s["judges"][1:]:  # the rest of the panel agrees: now it is unanimous
        client.post(
            f"{API}/debates/{s['debate']['id']}/ballots",
            json={"judge_id": judge["id"], "winner": "prop", "scores": sheet},
        )
    assert client.get(f"{PUBLIC}/judges/{solo['id']}").json()["decisions"]["unanimous"] == 1


# --- 7. admin paging bounds ---


def test_admin_list_paging_bounds(client: TestClient) -> None:
    for path in ("/admin/debaters", "/admin/judges/records"):
        assert client.get(f"{API}{path}", params={"offset": -1}).status_code == 422, path
        assert client.get(f"{API}{path}", params={"limit": 0}).status_code == 422, path


# --- 9. speaker rank sorts best first by default ---


def test_debater_list_rank_sort_is_best_first(client: TestClient) -> None:
    build_tournament(client, teams=4, prelim_rounds=3, elim_rounds=1, slug="rank-cup")
    client.post(f"{API}/stats/refresh")

    def ranks(order: str | None) -> list[int]:
        params = {"sort": "rank", "limit": 100} | ({"order": order} if order else {})
        items = client.get(f"{PUBLIC}/debaters", params=params).json()["items"]
        return [i["speaker_rank"] for i in items if i["speaker_rank"] is not None]

    best_first = ranks(None)
    assert best_first and best_first[0] == min(best_first)
    assert best_first == sorted(best_first) == ranks("desc")
    assert ranks("asc") == sorted(best_first, reverse=True)
