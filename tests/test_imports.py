"""Console imports: check (dry run + staging), decisions, import from staging, guards."""

import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.core.config import settings
from app.models import (
    Debate,
    DebateJudge,
    Debater,
    ImportBatch,
    ImportJob,
    Institution,
    Judge,
    Round,
    Team,
    TeamMember,
    Tournament,
)
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


def _adjudicator(slug: str, id_: int, judge: str | dict, institution_url: str | None) -> dict:
    """`judge` is a name, or a dict with `name` and any fields to override (email, gender)."""
    fields = judge if isinstance(judge, dict) else {"name": judge}
    return {
        "id": id_,
        "url": u(f"/api/v1/tournaments/{slug}/adjudicators/{id_}"),
        "last_name": fields["name"].split()[-1],
        "institution": institution_url,
        "email": None,
        "phone": "",
        "gender": None,
        **fields,
    }


def register_simple(
    slug: str,
    *,
    school: str = "Royal Colege Colombo",
    speakers: list[str] | None = None,
    judges: list[str | dict] | None = None,
    starts_at: str | None = "2025-01-04T09:00:00Z",
) -> None:
    """One school, two teams, one round with one two-team debate, no ballots. `judges` are
    adjudicators from that school (not on any panel)."""
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
        "starts_at": starts_at,
    }
    adjudicators = [_adjudicator(slug, i + 1, n, institution["url"]) for i, n in enumerate(judges or [])]
    pairing = {
        "id": 1,
        "teams": [{"team": teams[0]["url"], "side": "aff"}, {"team": teams[1]["url"], "side": "neg"}],
        "adjudicators": None,
    }
    tournament = {"name": f"Open {slug}", "short_name": slug.upper(), "slug": slug}
    respx.get(u(f"/api/v1/tournaments/{slug}")).mock(return_value=httpx.Response(200, json=tournament))
    respx.get(u("/api/v1/institutions")).mock(return_value=httpx.Response(200, json=[institution]))
    respx.get(u(f"/api/v1/tournaments/{slug}/teams")).mock(return_value=httpx.Response(200, json=teams))
    respx.get(u(f"/api/v1/tournaments/{slug}/adjudicators")).mock(
        return_value=httpx.Response(200, json=adjudicators)
    )
    for path in ("venues", "score-criteria", "motions"):
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
    # A namesake elsewhere who debated recently may have switched schools: a human picks.
    assert _suggest_person([other], 7, "debater", recent={3}) is None
    assert _suggest_person([no_school, other], 7, "debater", recent={3}) is None
    # Several judge copies: the busiest, ties to the oldest row.
    assert _suggest_person([same, other], 7, "judge", activity={3: 5, 2: 1}) == 3
    assert _suggest_person([other, same], 7, "judge", activity={}) == 2


def _judge_with_panels(session: Session, name: str, school: Institution, panels: int) -> Judge:
    judge = Judge(full_name=name, institution_id=school.id)
    session.add(judge)
    session.flush()
    if panels:
        tournament = Tournament(name=f"Old {name} {school.id}", slug=f"old-{judge.id}")
        session.add(tournament)
        session.flush()
        round_ = Round(tournament_id=tournament.id, seq=1, name="R1")
        prop, opp = Team(tournament_id=tournament.id, name="P"), Team(tournament_id=tournament.id, name="O")
        session.add_all([round_, prop, opp])
        session.flush()
        for _ in range(panels):
            debate = Debate(round_id=round_.id, prop_team_id=prop.id, opp_team_id=opp.id)
            session.add(debate)
            session.flush()
            session.add(DebateJudge(debate_id=debate.id, judge_id=judge.id))
    return judge


def test_judge_at_another_school_is_linked_without_a_decision(client: TestClient, session: Session) -> None:
    home = Institution(name="Royal College Colombo")
    elsewhere = Institution(name="Visakha Vidyalaya")
    session.add_all([home, elsewhere])
    session.flush()
    judge = _judge_with_panels(session, "Thithira Jayakody", elsewhere, 0)
    session.commit()

    with respx.mock:
        register_simple("open", school="Royal College Colombo", judges=["Thithira Jayakody"])
        batch = _create(client, "open")
    job = batch["jobs"][0]
    assert job["status"] == "ready", job["error"]
    assert not _issues(job, "person_namesake")
    [info] = _issues(job, "judge_other_school")
    assert info["severity"] == "info" and "Thithira Jayakody" in info["message"]
    [res] = [r for r in job["report"]["resolutions"] if r["kind"] == "judge"]
    assert res["local_id"] == judge.id and res["how"] == "name"

    with respx.mock:
        run = client.post(f"{IMPORTS}/{batch['id']}/run", json={"job_ids": [job["id"]]})
    assert run.json()["jobs"][0]["status"] == "imported"
    session.expire_all()
    judges = session.exec(select(Judge)).all()
    assert [j.id for j in judges] == [judge.id]
    assert judges[0].institution_id == elsewhere.id  # matching only: the school is left alone


def test_judge_duplicates_suggest_the_busiest_and_a_same_school_copy_wins(
    client: TestClient, session: Session
) -> None:
    home = Institution(name="Royal College Colombo")
    a, b = Institution(name="Gateway College Colombo"), Institution(name="Visakha Vidyalaya")
    session.add_all([home, a, b])
    session.flush()
    _judge_with_panels(session, "Thithira Jayakody", a, 1)
    busy = _judge_with_panels(session, "Thithira Jayakody", b, 3)
    session.commit()

    with respx.mock:
        register_simple("open", school="Royal College Colombo", judges=["Thithira Jayakody"])
        job = _create(client, "open")["jobs"][0]
    [namesake] = _issues(job, "person_namesake")
    assert namesake["kind"] == "judge" and namesake["suggested"] == busy.id

    same = _judge_with_panels(session, "Thithira Jayakody", home, 0)
    session.commit()
    with respx.mock:
        register_simple("again", school="Royal College Colombo", judges=["Thithira Jayakody"])
        job = _create(client, "again")["jobs"][0]
    assert not _issues(job, "person_namesake")
    [res] = [r for r in job["report"]["resolutions"] if r["kind"] == "judge"]
    assert res["local_id"] == same.id and res["how"] == "name+institution"


def test_debater_namesake_elsewhere_who_debated_recently_is_left_to_a_human(
    client: TestClient, session: Session
) -> None:
    home, other = Institution(name="Royal College Colombo"), Institution(name="Somewhere Else")
    session.add_all([home, other])
    session.flush()
    debater = Debater(full_name="Kamal Perera", institution_id=other.id)
    recent = Tournament(name="Recent", slug="recent", date=date.today() - timedelta(days=60))
    session.add_all([debater, recent])
    session.flush()
    team = Team(tournament_id=recent.id, name="Else A", institution_id=other.id)
    session.add(team)
    session.flush()
    session.add(TeamMember(team_id=team.id, debater_id=debater.id))
    session.commit()

    with respx.mock:
        register_simple("open", school="Royal College Colombo")
        job = _create(client, "open")["jobs"][0]
    [namesake] = _issues(job, "person_namesake")
    assert namesake["suggested"] is None
    assert job["undecided"] == 1

    recent.date = date.today() - timedelta(days=800)
    session.add(recent)
    session.commit()
    with respx.mock:
        register_simple("later", school="Royal College Colombo")
        job = _create(client, "later")["jobs"][0]
    [namesake] = _issues(job, "person_namesake")
    assert namesake["suggested"] == "new"


def test_tournament_date_entered_by_hand_when_rounds_have_no_start(client: TestClient, session: Session) -> None:
    session.add(Institution(name="Royal College Colombo"))
    session.commit()
    with respx.mock:
        register_simple("open", school="Royal College Colombo", starts_at=None)
        batch = _create(client, "open")
    job = batch["jobs"][0]
    assert _issues(job, "missing_start")

    url = f"{IMPORTS}/jobs/{job['id']}/decisions"
    saved = client.put(url, json={"tournament_date": "2025-03-01"})
    assert saved.status_code == 200, saved.text
    assert saved.json()["decisions"]["tournament_date"] == "2025-03-01"
    # Saving other decisions keeps the date; an explicit null clears it.
    assert client.put(url, json={}).json()["decisions"]["tournament_date"] == "2025-03-01"
    assert client.put(url, json={"tournament_date": None}).json()["decisions"]["tournament_date"] is None
    client.put(url, json={"tournament_date": "2025-03-02"})

    with respx.mock:
        run = client.post(f"{IMPORTS}/{batch['id']}/run", json={"job_ids": [job["id"]]})
    done = run.json()["jobs"][0]
    assert done["status"] == "imported", done["error"]
    assert session.get(Tournament, done["tournament_id"]).date == date(2025, 3, 2)


def test_round_start_times_win_over_a_hand_entered_date(session: Session) -> None:
    session.add(Institution(name="Royal College Colombo"))
    session.commit()
    with respx.mock:
        register_simple("open", school="Royal College Colombo")
        report = import_tournament(
            BASE_URL, "open", session=session, overrides=ImportOverrides(tournament_date=date(2030, 1, 1))
        )
    assert session.get(Tournament, report.tournament_id).date == date(2025, 1, 4)
    assert not [i for i in report.issues if i.code == "date_by_hand"]


def test_school_suggestion_needs_the_same_distinguishing_words() -> None:
    from types import SimpleNamespace as C

    from app.services.tabbycat import _suggest_school

    def school(id_: int, name: str) -> C:
        return C(id=id_, name=name, score=0.95)

    # Branches of one chain are different schools, whatever the similarity score.
    colombo, dehiwala = school(1, "Gateway College Colombo"), school(2, "Gateway College Dehiwala")
    assert _suggest_school("Gateway College Negombo", [colombo, dehiwala]) == "new"
    # Nalanda / Ananda score 0.95 but are different schools.
    assert _suggest_school("Ananda College", [school(3, "Nalanda College")]) == "new"
    # Spelling and filler words only: suggested.
    assert _suggest_school("Royal Colege Colombo", [school(4, "Royal College Colombo")]) == 4
    assert _suggest_school("British School of Colombo", [school(5, "The British School in Colombo")]) == 5
    assert _suggest_school("S. Thomas' College", [school(6, "St Thomas' College")]) == 6
    assert _suggest_school("Vishaka Vidyalaya", [school(7, "Visakha Vidyalaya")]) == 7
    # The upstream name may leave out the branch: a human picks.
    assert _suggest_school("Hillwood College", [school(8, "Hillwood College Kandy")]) is None
    # Extra words in front name another school.
    assert _suggest_school("Ladies' College", [school(14, "Muslim Ladies' College")]) == "new"
    gampaha, wattala = school(9, "Lyceum International School Gampaha"), school(10, "Lyceum International School Wattala")
    assert _suggest_school("Lyceum International School", [gampaha, wattala]) is None
    # Two equivalent schools here already (duplicates): a human picks; the same name twice is one.
    assert _suggest_school("British School Colombo", [school(5, "The British School in Colombo"), school(11, "British School of Colombo")]) is None
    assert _suggest_school("Elizabeth Moir", [school(12, "Elizabeth Moir School"), school(13, "Elizabeth Moir school")]) == 12
    assert _suggest_school("Anything", []) == "new"


def test_school_candidates_include_same_words_and_rank_them_first(client: TestClient, session: Session) -> None:
    near = Institution(name="British School Kandy")  # higher score, a different branch
    same = Institution(name="The British School in Colombo")  # low score, same school
    session.add_all([near, same])
    session.commit()
    with respx.mock:
        register_simple("open", school="British School of Colombo")
        job = _create(client, "open")["jobs"][0]
    [issue] = _issues(job, "school_similar")
    assert issue["candidates"][0]["id"] == same.id
    assert issue["suggested"] == same.id


def test_swing_teams_get_no_school(session: Session) -> None:
    with respx.mock:
        register_simple("open", school="Swing")
        report = import_tournament(BASE_URL, "open", session=session)
    assert session.exec(select(Institution)).all() == []
    assert all(t.institution_id is None for t in session.exec(select(Team)).all())
    assert all(d.institution_id is None for d in session.exec(select(Debater)).all())
    assert [i for i in report.issues if i.code == "composite_school"]
    assert not [i for i in report.issues if i.code == "unresolved_institution"]


def _school(session: Session, name: str = "Royal College Colombo") -> Institution:
    school = Institution(name=name)
    session.add(school)
    session.commit()
    return school


def _judge_resolution(report) -> tuple[str, int]:
    [res] = [r for r in report.resolutions if r.kind == "judge"]
    return res.how, res.local_id


def test_first_name_only_is_never_matched_or_suggested(session: Session) -> None:
    home = _school(session)
    debater = Debater(full_name="Kamal", institution_id=home.id)
    judge = Judge(full_name="Akindu", institution_id=None)
    session.add_all([debater, judge])
    session.commit()
    with respx.mock:
        register_simple("open", school=home.name, speakers=["Kamal", "Nimal Silva", "Sunil Fernando"], judges=["Akindu"])
        report = import_tournament(BASE_URL, "open", session=session, dry_run=True)
    namesakes = {i.upstream_name: i for i in report.issues if i.code == "person_namesake"}
    assert namesakes["Kamal"].suggested == "new" and namesakes["Kamal"].candidates[0].id == debater.id
    assert namesakes["Akindu"].suggested == "new"
    assert _judge_resolution(report)[0] == "created"


def test_judge_link_needs_no_conflicting_email_or_gender(session: Session) -> None:
    _school(session)
    elsewhere = _school(session, "Visakha Vidyalaya")
    session.add_all(
        [
            Judge(full_name="Thithira Jayakody", institution_id=elsewhere.id, email="t@one.lk"),
            Judge(full_name="Visura Padeniya", institution_id=elsewhere.id, gender="male"),
            Judge(full_name="Dinuka Perera", institution_id=elsewhere.id),
        ]
    )
    session.commit()
    judges = [
        {"name": "Thithira Jayakody", "email": "t@two.lk"},
        {"name": "Visura Padeniya", "gender": "F"},
        {"name": "Dinuka Perera", "gender": "M"},
    ]
    with respx.mock:
        register_simple("open", school="Royal College Colombo", judges=judges)
        report = import_tournament(BASE_URL, "open", session=session, dry_run=True)
    hows = {r.upstream_name: r.how for r in report.resolutions if r.kind == "judge"}
    assert hows == {"Thithira Jayakody": "created", "Visura Padeniya": "created", "Dinuka Perera": "name"}
    suggested = {i.upstream_name: i.suggested for i in report.issues if i.code == "person_namesake"}
    assert suggested == {"Thithira Jayakody": "new", "Visura Padeniya": "new"}


def test_email_needs_a_compatible_name(session: Session) -> None:
    home = _school(session)
    holder = Judge(full_name="Coach Fernando", institution_id=home.id, email="debate@royal.lk")
    initial = Judge(full_name="K. Perera", institution_id=None, email="kp@mail.lk")
    session.add_all([holder, initial])
    session.commit()
    judges = [{"name": "Nimal Silva", "email": "debate@royal.lk"}, {"name": "Kamal Perera", "email": "KP@mail.lk"}]
    with respx.mock:
        register_simple("open", school=home.name, judges=judges)
        report = import_tournament(BASE_URL, "open", session=session, dry_run=True)
    resolved = {r.upstream_name: (r.how, r.local_id) for r in report.resolutions if r.kind == "judge"}
    assert resolved["Kamal Perera"] == ("email", initial.id)
    assert resolved["Nimal Silva"][0] == "created"
    [issue] = [i for i in report.issues if i.code == "email_other_name"]
    assert issue.upstream_name == "Nimal Silva" and issue.suggested == "new"
    assert [c.id for c in issue.candidates] == [holder.id]


def test_unknown_email_still_matches_by_name_and_school(session: Session) -> None:
    home = _school(session)
    judge = Judge(full_name="Kaveesha Mallawaarachchi", institution_id=home.id)
    session.add(judge)
    session.commit()
    with respx.mock:
        register_simple("open", school=home.name, judges=[{"name": "Kaveesha Mallawaarachchi", "email": "k@x.lk"}])
        report = import_tournament(BASE_URL, "open", session=session, dry_run=True)
    assert _judge_resolution(report) == ("name+institution", judge.id)


def test_two_adjudicators_of_one_name_stay_apart(session: Session) -> None:
    _school(session)
    with respx.mock:
        register_simple("open", school="Royal College Colombo", judges=["Dinuka Perera", "Dinuka Perera"])
        report = import_tournament(BASE_URL, "open", session=session)
    assert len(session.exec(select(Judge)).all()) == 2
    assert [i for i in report.issues if i.code == "same_name_twice"]
