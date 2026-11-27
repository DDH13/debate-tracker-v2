"""Console imports: check (dry run + staging), decisions, import from staging, guards."""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.core.config import settings
from app.models import Debater, ImportBatch, ImportJob, Institution, Team, TeamMember, Tournament
from app.services import imports as service
from app.services.tabbycat import ImportBlocked, ImportOverrides, import_tournament
from tests.test_tabbycat_import import BASE_URL, SLUG, register_routes, u

IMPORTS = "/api/v1/admin/imports"
KEY = "sekrit-api-key-9f3a"


def _speaker(slug: str, id_: int, name: str) -> dict:
    return {
        "id": id_,
        "url": u(f"/api/v1/tournaments/{slug}/speakers/{id_}"),
        "name": name,
        "last_name": name.split()[-1],
        "email": None,
        "phone": "",
        "gender": None,
    }


def register_simple(slug: str, *, school: str = "Royal Colege Colombo", speakers: list[str] | None = None) -> None:
    """One school, two teams, one round with one two-team debate, no ballots."""
    names = speakers or ["Kamal Perera", "Nimal Silva", "Sunil Fernando"]
    institution = {"id": 7, "url": u("/api/v1/institutions/7"), "name": school, "code": None}
    teams = []
    for team_id in (1, 2):
        teams.append(
            {
                "id": team_id,
                "url": u(f"/api/v1/tournaments/{slug}/teams/{team_id}"),
                "institution": institution["url"],
                "short_name": f"{school} {team_id}",
                "long_name": f"{school} {team_id}",
                "speakers": [
                    _speaker(slug, team_id * 10 + i, n if team_id == 1 else f"{n} {team_id}")
                    for i, n in enumerate(names)
                ],
            }
        )
    round_ = {
        "id": 1,
        "url": u(f"/api/v1/tournaments/{slug}/rounds/1"),
        "seq": 1,
        "name": "Round 1",
        "abbreviation": "R1",
        "stage": "P",
        "completed": True,
        "starts_at": "2025-01-04T09:00:00Z",
    }
    pairing = {
        "id": 1,
        "teams": [{"team": teams[0]["url"], "side": "aff"}, {"team": teams[1]["url"], "side": "neg"}],
        "adjudicators": None,
    }
    tournament = {"name": f"Open {slug}", "short_name": slug.upper(), "slug": slug}
    respx.get(u(f"/api/v1/tournaments/{slug}")).mock(return_value=httpx.Response(200, json=tournament))
    respx.get(u("/api/v1/institutions")).mock(return_value=httpx.Response(200, json=[institution]))
    respx.get(u(f"/api/v1/tournaments/{slug}/teams")).mock(return_value=httpx.Response(200, json=teams))
    for path in ("adjudicators", "venues", "score-criteria", "motions"):
        respx.get(u(f"/api/v1/tournaments/{slug}/{path}")).mock(return_value=httpx.Response(200, json=[]))
    respx.get(u(f"/api/v1/tournaments/{slug}/rounds")).mock(return_value=httpx.Response(200, json=[round_]))
    respx.get(u(f"/api/v1/tournaments/{slug}/rounds/1/pairings")).mock(
        return_value=httpx.Response(200, json=[pairing])
    )
    respx.get(u(f"/api/v1/tournaments/{slug}/rounds/1/pairings/1/ballots"), params={"confirmed": "true"}).mock(
        return_value=httpx.Response(200, json=[])
    )


def _create(client: TestClient, *slugs: str, key: str | None = KEY, **options) -> dict:
    rows = [{"url": f"{BASE_URL}/{slug}/tab/", "api_key": key} for slug in slugs]
    resp = client.post(IMPORTS, json={"rows": rows, **options})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _issues(job: dict, code: str) -> list[dict]:
    return [i for i in job["report"]["issues"] if i["code"] == code]


# --- URL parsing -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://x.calicotab.com/isdc25/", ("https://x.calicotab.com", "isdc25")),
        ("https://x.calicotab.com/isdc25/tab/team/", ("https://x.calicotab.com", "isdc25")),
        ("http://localhost:8000/api/v1/tournaments/abc-1", ("http://localhost:8000", "abc-1")),
        ("  https://tab.example.com/my_open  ", ("https://tab.example.com", "my_open")),
    ],
)
def test_parse_tabbycat_url(url: str, expected: tuple[str, str]) -> None:
    assert service.parse_tabbycat_url(url) == expected


@pytest.mark.parametrize("url", ["tab.example.com/x", "https://tab.example.com/", "https://tab.example.com/admin/", "ftp://a/b"])
def test_parse_tabbycat_url_rejects(url: str) -> None:
    with pytest.raises(service.ImportsInvalid):
        service.parse_tabbycat_url(url)


def test_create_rejects_bad_and_duplicate_rows(client: TestClient) -> None:
    bad = client.post(IMPORTS, json={"rows": [{"url": f"{BASE_URL}/a/"}, {"url": "nonsense"}]})
    assert bad.status_code == 422
    assert "Row 2" in bad.json()["detail"]
    dup = client.post(IMPORTS, json={"rows": [{"url": f"{BASE_URL}/a/"}, {"url": f"{BASE_URL}/a/tab/"}]})
    assert dup.status_code == 422
    assert "row 1" in dup.json()["detail"]
    assert client.post(IMPORTS, json={"rows": []}).status_code == 422


# --- Issues from the check -------------------------------------------------------------------


def test_check_reports_structured_issues_and_writes_nothing(client: TestClient, session: Session) -> None:
    session.add(Institution(name="Royal College Colombo"))
    other = Institution(name="Somewhere Else")
    session.add(other)
    session.flush()
    session.add(Debater(full_name="Kamal Perera", institution_id=other.id))
    session.commit()

    with respx.mock:
        register_simple("open")
        batch = _create(client, "open")

    job = batch["jobs"][0]
    assert job["status"] == "ready", job["error"]
    assert job["report"]["tournament_name"] == "Open open"

    [school] = _issues(job, "school_similar")
    assert school["severity"] == "decision"
    assert school["kind"] == "institution"
    assert school["candidates"][0]["name"] == "Royal College Colombo"
    assert school["suggested"] == school["candidates"][0]["id"]  # one letter off: suggested

    [namesake] = _issues(job, "person_namesake")
    assert namesake["kind"] == "debater"
    assert namesake["candidates"][0]["detail"] == "Somewhere Else"
    assert namesake["suggested"] == "new"

    # Suggestions are preselected, so nothing is left undecided.
    assert job["decisions"]["institutions"] == {school["upstream_url"]: school["suggested"]}
    assert job["decisions"]["debaters"] == {namesake["upstream_url"]: "new"}
    assert job["undecided"] == 0
    assert _issues(job, "no_adjudicator")
    assert _issues(job, "matched_existing")[0]["severity"] == "info"

    session.expire_all()
    assert session.exec(select(Tournament)).all() == []
    assert len(session.exec(select(Institution)).all()) == 2


def test_check_flags_bp_debates_and_already_imported(client: TestClient, session: Session) -> None:
    with respx.mock:
        register_routes()
        batch = _create(client, SLUG)
    job = batch["jobs"][0]
    assert any(i["code"] == "bp_debate" for i in job["report"]["issues"])
    assert any(i["code"] == "team_size" for i in job["report"]["issues"])  # team 4 has two speakers

    session.add(Tournament(name="Sample Tab", slug=SLUG))
    session.commit()
    with respx.mock:
        register_routes()
        again = _create(client, SLUG)
    [blocking] = _issues(again["jobs"][0], "already_imported")
    assert blocking["severity"] == "blocking"
    resp = client.post(f"{IMPORTS}/{again['id']}/run", json={"job_ids": [again["jobs"][0]["id"]]})
    assert resp.status_code == 409


def test_bp_only_tournament_is_blocking(session: Session) -> None:
    with respx.mock:
        register_routes()
        respx.get(u(f"/api/v1/tournaments/{SLUG}/rounds/1/pairings")).mock(return_value=httpx.Response(200, json=[]))
        report = import_tournament(BASE_URL, SLUG, session=session, dry_run=True)
    assert [i.code for i in report.blocking] == ["no_two_team_debates"]


def test_upstream_401_fails_the_check_with_a_clear_error(client: TestClient) -> None:
    with respx.mock:
        respx.get(u("/api/v1/tournaments/locked")).mock(return_value=httpx.Response(401, json={}))
        batch = _create(client, "locked")
    job = batch["jobs"][0]
    assert job["status"] == "failed"
    assert "API key" in job["error"]


# --- Overrides --------------------------------------------------------------------------------


def test_overrides_map_school_and_person_to_existing_rows(session: Session) -> None:
    school = Institution(name="Royal College, Colombo 7")
    session.add(school)
    session.flush()
    kamal = Debater(full_name="K. Perera", institution_id=school.id)
    session.add(kamal)
    session.commit()

    overrides = ImportOverrides(
        institutions={u("/api/v1/institutions/7"): school.id},
        debaters={u("/api/v1/tournaments/open/speakers/10"): kamal.id},
    )
    with respx.mock:
        register_simple("open")
        report = import_tournament(BASE_URL, "open", session=session, overrides=overrides)

    assert report.institutions == 0
    assert len(session.exec(select(Institution)).all()) == 1
    assert {t.institution_id for t in session.exec(select(Team)).all()} == {school.id}
    team1 = session.exec(select(Team).where(Team.name == "Royal Colege Colombo 1")).one()
    members = {m.debater_id for m in session.exec(select(TeamMember).where(TeamMember.team_id == team1.id)).all()}
    assert kamal.id in members
    assert any(r.how == "override" and r.local_id == kamal.id for r in report.resolutions)


def test_new_override_rules_out_candidates_but_keeps_exact_matches(session: Session) -> None:
    """ "new" means "none of the fuzzy candidates": a school with the same key (here created
    by an earlier tournament in the batch) is still matched, not duplicated."""
    session.add(Institution(name="Royal Colege Colombo"))
    session.commit()
    overrides = ImportOverrides(institutions={u("/api/v1/institutions/7"): "new"})
    with respx.mock:
        register_simple("open")
        report = import_tournament(BASE_URL, "open", session=session, overrides=overrides)
    assert report.institutions == 0
    assert len(session.exec(select(Institution)).all()) == 1


def test_override_to_missing_row_blocks_a_real_import(session: Session) -> None:
    overrides = ImportOverrides(institutions={u("/api/v1/institutions/7"): 9999})
    with respx.mock:
        register_simple("open")
        with pytest.raises(ImportBlocked, match="no longer exists"):
            import_tournament(BASE_URL, "open", session=session, overrides=overrides)
    session.expire_all()
    assert session.exec(select(Tournament)).all() == []


# --- The whole flow ---------------------------------------------------------------------------


def test_check_decide_import_replays_staged_data(client: TestClient, session: Session, caplog) -> None:
    caplog.set_level(logging.INFO)
    existing = Institution(name="Royal College Colombo")
    session.add(existing)
    session.commit()

    with respx.mock:
        register_simple("open")
        batch = _create(client, "open")
    job = batch["jobs"][0]
    assert job["status"] == "ready"
    staging = Path(settings.import_staging_dir) / str(job["id"])
    assert staging.is_dir() and any(staging.rglob("*.json"))

    [school] = _issues(job, "school_similar")
    resp = client.put(f"{IMPORTS}/jobs/{job['id']}/decisions", json={"institutions": {school["upstream_url"]: "new"}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["decisions"]["institutions"][school["upstream_url"]] == "new"

    # No upstream routes: the import must replay entirely from the staged responses.
    with respx.mock:
        run = client.post(f"{IMPORTS}/{batch['id']}/run", json={"job_ids": [job["id"]]})
    assert run.status_code == 200, run.text
    done = run.json()["jobs"][0]
    assert done["status"] == "imported", done["error"]
    tournament = session.get(Tournament, done["tournament_id"])
    assert tournament is not None and tournament.slug == "open"
    assert len(session.exec(select(Institution)).all()) == 2  # "new" was honoured
    assert not staging.exists()

    # The key is never stored, returned, used in a file name or logged.
    rows = [j.model_dump(mode="json") for j in session.exec(select(ImportJob)).all()]
    rows += [b.model_dump(mode="json") for b in session.exec(select(ImportBatch)).all()]
    assert KEY not in json.dumps(rows)
    assert KEY not in client.get(f"{IMPORTS}/{batch['id']}").text
    assert KEY not in client.get(IMPORTS).text
    assert KEY not in caplog.text


def test_run_guards(client: TestClient, session: Session) -> None:
    session.add(Institution(name="Royal College Colombo"))
    session.commit()
    with respx.mock:
        register_simple("open")
        batch = _create(client, "open")
    job_id = batch["jobs"][0]["id"]

    job = session.get(ImportJob, job_id)
    job.decisions = {}
    session.add(job)
    session.commit()
    resp = client.post(f"{IMPORTS}/{batch['id']}/run", json={"job_ids": [job_id]})
    assert resp.status_code == 409
    assert "decide" in resp.json()["detail"]

    bad = client.put(f"{IMPORTS}/jobs/{job_id}/decisions", json={"institutions": {"https://nope": "new"}})
    assert bad.status_code == 422
    missing = client.put(
        f"{IMPORTS}/jobs/{job_id}/decisions", json={"institutions": {u("/api/v1/institutions/7"): 9999}}
    )
    assert missing.status_code == 422

    job.status = "checking"
    session.add(job)
    session.commit()
    assert client.delete(f"{IMPORTS}/jobs/{job_id}").status_code == 409
    assert client.put(f"{IMPORTS}/jobs/{job_id}/decisions", json={}).status_code == 409

    job.status = "ready"
    session.add(job)
    session.commit()
    assert client.delete(f"{IMPORTS}/jobs/{job_id}").status_code == 204
    assert session.get(ImportJob, job_id).status == "discarded"
    assert not (Path(settings.import_staging_dir) / str(job_id)).exists()
    assert client.post(f"{IMPORTS}/{batch['id']}/run", json={"job_ids": [job_id]}).status_code == 409


def test_recheck_needs_the_key_again_and_clears_decisions(client: TestClient) -> None:
    with respx.mock:
        respx.get(u("/api/v1/tournaments/open")).mock(return_value=httpx.Response(401, json={}))
        batch = _create(client, "open", key=None)
    job = batch["jobs"][0]
    assert job["status"] == "failed"
    with respx.mock:
        register_simple("open")
        resp = client.post(f"{IMPORTS}/jobs/{job['id']}/check", json={"api_key": KEY})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ready"


def test_stale_active_job_is_marked_failed(client: TestClient, session: Session) -> None:
    batch = ImportBatch()
    session.add(batch)
    session.flush()
    job = ImportJob(
        batch_id=batch.id,
        base_url=BASE_URL,
        slug="lost",
        status="importing",
        updated_at=datetime.now(timezone.utc) - timedelta(seconds=settings.import_stale_seconds + 5),
    )
    fresh = ImportJob(batch_id=batch.id, position=1, base_url=BASE_URL, slug="busy", status="checking")
    session.add(job)
    session.add(fresh)
    session.commit()

    jobs = client.get(f"{IMPORTS}/{batch.id}").json()["jobs"]
    assert jobs[0]["status"] == "failed"
    assert "restarted" in jobs[0]["error"]
    assert jobs[1]["status"] == "checking"


def test_batch_imports_in_order_and_refreshes_stats_once(
    client: TestClient, session: Session, monkeypatch
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(service, "refresh_profiles", lambda s: calls.append(1))

    with respx.mock:
        register_simple("first")
        register_simple("second", speakers=["Ruwan Jay", "Dilan Raj", "Ishan Dee"])
        batch = _create(client, "first", "second")
    ids = [j["id"] for j in batch["jobs"]]
    assert [j["status"] for j in batch["jobs"]] == ["ready", "ready"]

    with respx.mock:
        run = client.post(f"{IMPORTS}/{batch['id']}/run", json={"job_ids": ids})
    jobs = run.json()["jobs"]
    assert [j["status"] for j in jobs] == ["imported", "imported"], [j["error"] for j in jobs]
    # Both checks created the school; on import the second matched the first's by key.
    assert len(session.exec(select(Institution)).all()) == 1
    assert calls == [1]
    assert run.json()["stats_refreshed_at"] is not None

    listed = client.get(IMPORTS).json()
    assert listed[0]["id"] == batch["id"]
    assert listed[0]["counts"] == {"imported": 2}
    assert listed[0]["tournaments"] == ["Open first", "Open second"]


# --- Suggestion rules (found on real data: earlier imports left many debaters school-less) ---


def test_person_suggestion_prefers_a_single_schoolless_or_same_school_namesake() -> None:
    from types import SimpleNamespace as P

    from app.services.tabbycat import _suggest_person

    no_school, same, other = P(id=1, institution_id=None), P(id=2, institution_id=7), P(id=3, institution_id=9)
    assert _suggest_person([no_school], 7, "debater") == 1
    assert _suggest_person([same], 7, "debater") == 2
    assert _suggest_person([other], 7, "debater") == "new"
    assert _suggest_person([no_school, other], 7, "debater") == 1
    assert _suggest_person([no_school, same], 7, "debater") is None  # duplicates already: a human picks
    assert _suggest_person([other], 7, "judge") == 3  # judges move between schools


def test_school_suggestion_is_left_open_when_two_different_schools_tie() -> None:
    from types import SimpleNamespace as C

    from app.services.tabbycat import _suggest_school

    gampaha = C(id=1, name="Lyceum International School Gampaha", score=0.954)
    wattala = C(id=2, name="Lyceum International School Wattala", score=0.954)
    assert _suggest_school([gampaha, wattala]) is None
    moir, moir_dup = C(id=3, name="Elizabeth Moir School", score=0.933), C(id=4, name="Elizabeth Moir school", score=0.933)
    assert _suggest_school([moir, moir_dup]) == 3  # same name twice is one school
    assert _suggest_school([C(id=5, name="Nalanda College", score=0.915)]) == "new"
