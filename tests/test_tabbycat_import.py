import logging
import sqlite3

import httpx
import respx
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.models import BPDebate, BPDebateTeam, BPSide, Team, TeamMember
from app.services.tabbycat import (
    _normalize_bp_side,
    _TabbycatClient,
    describe_integrity_error,
    import_tournament,
)

BASE_URL = "https://tab.example.com"
SLUG = "sample-tab"
IMPORT_URL = "/api/v1/tournaments/import"


def u(path: str) -> str:
    return f"{BASE_URL}{path}"


TOURNAMENT = {
    "id": 1,
    "url": u(f"/api/v1/tournaments/{SLUG}"),
    "name": "Sample Tab",
    "short_name": "Sample",
    "slug": SLUG,
    "current_rounds": [],
}

PREFERENCES_TWO_TEAM = [
    {"identifier": "debate_rules__teams_in_debate", "value": 2},
    {"identifier": "debate_rules__substantive_speakers", "value": 3},
    {"identifier": "debate_rules__reply_scores_enabled", "value": True},
    {"identifier": "debate_rules__ballots_per_debate_prelim", "value": "per-adj"},
]

PREFERENCES_BP = [
    {"identifier": "debate_rules__teams_in_debate", "value": 4},
    {"identifier": "debate_rules__substantive_speakers", "value": 2},
    {"identifier": "debate_rules__reply_scores_enabled", "value": False},
    {"identifier": "debate_rules__ballots_per_debate_prelim", "value": "per-debate"},
]

INSTITUTIONS = [
    {"id": 1, "url": u("/api/v1/institutions/1"), "name": "University A", "code": "UA"},
    {"id": 2, "url": u("/api/v1/institutions/2"), "name": "University B", "code": "UB"},
]


def _speaker(id_: int, name: str, last_name: str, email: str | None, gender: str | None) -> dict:
    return {
        "id": id_,
        "url": u(f"/api/v1/tournaments/{SLUG}/speakers/{id_}"),
        "name": name,
        "last_name": last_name,
        "email": email,
        "phone": "",
        "gender": gender,
        "categories": [],
    }


TEAM1 = {
    "id": 1,
    "url": u(f"/api/v1/tournaments/{SLUG}/teams/1"),
    "institution": INSTITUTIONS[0]["url"],
    "reference": "1",
    "short_reference": "1",
    "short_name": "University A 1",
    "long_name": "University A 1",
    "speakers": [
        _speaker(1, "Alice Adams", "Adams", "alice@example.com", "F"),
        _speaker(2, "Andrew Ali", "Ali", None, "M"),
        _speaker(3, "Amy Ito", "Ito", None, "F"),
    ],
}
TEAM2 = {
    "id": 2,
    "url": u(f"/api/v1/tournaments/{SLUG}/teams/2"),
    "institution": INSTITUTIONS[1]["url"],
    "reference": "1",
    "short_reference": "1",
    "short_name": "University B 1",
    "long_name": "University B 1",
    "speakers": [
        _speaker(4, "Bob Brooks", "Brooks", "bob@example.com", "M"),
        _speaker(5, "Bella Byrne", "Byrne", None, "F"),
        _speaker(6, "Ben Barnes", "Barnes", None, "M"),
    ],
}
# Team 3/4 exist only to feed a 4-team (BP-style) pairing that gets skipped, and to
# exercise person dedupe: Andrew Ali (no email, same institution) and Bob Brooks (same
# email) both reappear here and should reuse the Debater rows created for team 1/2.
TEAM3 = {
    "id": 3,
    "url": u(f"/api/v1/tournaments/{SLUG}/teams/3"),
    "institution": INSTITUTIONS[0]["url"],
    "reference": "2",
    "short_reference": "2",
    "short_name": "University A 2",
    "long_name": "University A 2",
    "speakers": [
        _speaker(7, "Andrew Ali", "Ali", None, "M"),
        _speaker(8, "Carl Cole", "Cole", None, "M"),
        _speaker(9, "Cathy Cruz", "Cruz", None, "F"),
    ],
}
TEAM4 = {
    "id": 4,
    "url": u(f"/api/v1/tournaments/{SLUG}/teams/4"),
    "institution": INSTITUTIONS[1]["url"],
    "reference": "2",
    "short_reference": "2",
    "short_name": "University B 2",
    "long_name": "University B 2",
    "speakers": [
        _speaker(10, "Bob Brooks", "Brooks", "bob@example.com", "M"),
        _speaker(11, "Dave Dunn", "Dunn", None, "M"),
    ],
}

ADJ_JORDAN = {
    "id": 1,
    "url": u(f"/api/v1/tournaments/{SLUG}/adjudicators/1"),
    "name": "Jordan Park",
    "last_name": "Park",
    "institution": INSTITUTIONS[1]["url"],
    "email": "jordan@judges.org",
    "phone": "",
    "gender": "M",
}
ADJ_PRIYA = {
    "id": 2,
    "url": u(f"/api/v1/tournaments/{SLUG}/adjudicators/2"),
    "name": "Priya Patel",
    "last_name": "Patel",
    "institution": None,
    "email": "priya@judges.org",
    "phone": "",
    "gender": "F",
}
ADJ_SAM = {
    "id": 3,
    "url": u(f"/api/v1/tournaments/{SLUG}/adjudicators/3"),
    "name": "Sam Sharma",
    "last_name": "Sharma",
    "institution": None,
    "email": None,
    "phone": "",
    "gender": None,
}

VENUE_101 = {
    "id": 1,
    "url": u(f"/api/v1/tournaments/{SLUG}/venues/1"),
    "name": "Room 101",
    "display_name": "Room 101",
    "priority": 0,
    "categories": [],
}

CRITERION_CONTENT = {"id": 1, "url": u(f"/api/v1/tournaments/{SLUG}/score-criteria/1"), "name": "Content"}
CRITERION_STYLE = {"id": 2, "url": u(f"/api/v1/tournaments/{SLUG}/score-criteria/2"), "name": "Style"}
CRITERION_STRATEGY = {"id": 3, "url": u(f"/api/v1/tournaments/{SLUG}/score-criteria/3"), "name": "Strategy"}
CRITERION_ANALYSIS = {"id": 4, "url": u(f"/api/v1/tournaments/{SLUG}/score-criteria/4"), "name": "Analysis"}
CRITERION_DELIVERY = {"id": 5, "url": u(f"/api/v1/tournaments/{SLUG}/score-criteria/5"), "name": "Delivery"}


def _motion(id_: int, round_url: str, text: str) -> dict:
    """Matches the real `/api/v1/tournaments/{slug}/motions` payload shape: motions are a
    separate resource that link back to rounds by URL (the round-list endpoint itself never
    embeds motions, only `motions_released`/`motions_status` flags)."""
    return {
        "id": id_,
        "url": u(f"/api/v1/tournaments/{SLUG}/motions/{id_}"),
        "text": text,
        "reference": text[:20],
        "info_slide": "",
        "info_slide_plain": "",
        "rounds": [{"round": round_url, "seq": 1}],
    }


ROUND1 = {
    "id": 1,
    "url": u(f"/api/v1/tournaments/{SLUG}/rounds/1"),
    "seq": 1,
    "name": "Round 1",
    "abbreviation": "R1",
    "stage": "P",
    "completed": True,
    "starts_at": "2024-06-01T09:00:00Z",
}
ROUND2 = {
    "id": 2,
    "url": u(f"/api/v1/tournaments/{SLUG}/rounds/2"),
    "seq": 2,
    "name": "Round 2",
    "abbreviation": "R2",
    "stage": "P",
    "completed": False,
    "starts_at": None,
}

MOTIONS = [
    _motion(1, ROUND1["url"], "This House Would ban social media for minors."),
    _motion(2, ROUND2["url"], "Motion B"),
    _motion(3, ROUND2["url"], "Motion C"),
    _motion(4, ROUND2["url"], "Motion D"),
]

PAIRING_R1 = {
    "id": 1,
    "url": u(f"/api/v1/tournaments/{SLUG}/rounds/1/pairings/1"),
    "venue": VENUE_101["url"],
    "teams": [
        {"team": TEAM1["url"], "side": "aff", "flags": []},
        {"team": TEAM2["url"], "side": "neg", "flags": []},
    ],
    "adjudicators": {
        "chair": ADJ_JORDAN["url"],
        "panellists": [ADJ_PRIYA["url"]],
        "trainees": [ADJ_SAM["url"]],
    },
    "flags": [],
}
PAIRING_R2_BP = {
    "id": 2,
    "url": u(f"/api/v1/tournaments/{SLUG}/rounds/2/pairings/2"),
    "venue": None,
    "teams": [
        {"team": TEAM3["url"], "side": "og", "flags": []},
        {"team": TEAM4["url"], "side": "oo", "flags": []},
        {"team": TEAM1["url"], "side": "cg", "flags": []},
        {"team": TEAM2["url"], "side": "co", "flags": []},
    ],
    "adjudicators": {"chair": ADJ_JORDAN["url"], "panellists": [], "trainees": []},
    "flags": [],
}


def _bp_speech(speaker_url: str, score: float) -> dict:
    return {"speaker": speaker_url, "score": score, "criteria": []}


# Consensus BP ballot (adjudicator: null, attributed to the chair) for PAIRING_R2_BP:
# og (TEAM3) 1st/3pts, co (TEAM2) 2nd/2pts, cg (TEAM1) 3rd/1pt, oo (TEAM4) 4th/0pts.
SUBMISSION_BP_CONSENSUS = {
    "id": 20,
    "result": {
        "sheets": [
            {
                "adjudicator": None,
                "teams": [
                    {
                        "team": TEAM3["url"],
                        "side": "og",
                        "points": 3,
                        "rank": 1,
                        "speeches": [
                            _bp_speech(TEAM3["speakers"][0]["url"], 76.0),
                            _bp_speech(TEAM3["speakers"][1]["url"], 75.0),
                        ],
                    },
                    {
                        "team": TEAM4["url"],
                        "side": "oo",
                        "points": 0,
                        "rank": 4,
                        "speeches": [
                            _bp_speech(TEAM4["speakers"][0]["url"], 70.0),
                            _bp_speech(TEAM4["speakers"][1]["url"], 69.5),
                        ],
                    },
                    {
                        "team": TEAM1["url"],
                        "side": "cg",
                        "points": 1,
                        "rank": 3,
                        "speeches": [
                            _bp_speech(TEAM1["speakers"][0]["url"], 72.0),
                            _bp_speech(TEAM1["speakers"][1]["url"], 71.5),
                        ],
                    },
                    {
                        "team": TEAM2["url"],
                        "side": "co",
                        "points": 2,
                        "rank": 2,
                        "speeches": [
                            _bp_speech(TEAM2["speakers"][0]["url"], 74.0),
                            _bp_speech(TEAM2["speakers"][1]["url"], 73.5),
                        ],
                    },
                ],
            }
        ]
    },
    "confirmed": True,
    "discarded": False,
    "forfeit": False,
    "version": 1,
}


def _speech(speaker_url: str, score: float, criteria: list[dict]) -> dict:
    return {"speaker": speaker_url, "score": score, "criteria": criteria}


def _breakdown(content: float, style: float, strategy: float) -> list[dict]:
    return [
        {"criterion": CRITERION_CONTENT["url"], "score": content},
        {"criterion": CRITERION_STYLE["url"], "score": style},
        {"criterion": CRITERION_STRATEGY["url"], "score": strategy},
    ]


def _unmatched(score: float) -> list[dict]:
    return [
        {"criterion": CRITERION_ANALYSIS["url"], "score": score - 5},
        {"criterion": CRITERION_DELIVERY["url"], "score": 5},
    ]


# Individual (non-consensus) ballot from the chair, version 1 - later superseded by the
# consensus ballot below (same judge via chair-attribution), so it should be dropped as
# a duplicate and never land in the DB.
SUBMISSION_JORDAN_V1 = {
    "id": 10,
    "result": {
        "sheets": [
            {
                "adjudicator": ADJ_JORDAN["url"],
                "teams": [
                    {"team": TEAM1["url"], "side": "aff", "win": True, "speeches": []},
                    {"team": TEAM2["url"], "side": "neg", "win": False, "speeches": []},
                ],
            }
        ]
    },
    "confirmed": True,
    "discarded": False,
    "forfeit": False,
    "version": 1,
}

# Consensus ballot (adjudicator: null) attributed to the chair; version 2 wins the
# collision against the submission above. Uses Content/Style/Strategy criteria, so the
# breakdown should populate.
SUBMISSION_CONSENSUS_V2 = {
    "id": 11,
    "result": {
        "sheets": [
            {
                "adjudicator": None,
                "teams": [
                    {
                        "team": TEAM1["url"],
                        "side": "aff",
                        "win": True,
                        "speeches": [
                            _speech(TEAM1["speakers"][0]["url"], 76.0, _breakdown(30.5, 30.5, 15.0)),
                            _speech(TEAM1["speakers"][1]["url"], 74.5, _breakdown(30.0, 29.5, 15.0)),
                            _speech(TEAM1["speakers"][2]["url"], 75.0, _breakdown(30.0, 30.0, 15.0)),
                            _speech(TEAM1["speakers"][0]["url"], 38.0, _breakdown(15.0, 15.0, 8.0)),
                        ],
                    },
                    {
                        "team": TEAM2["url"],
                        "side": "neg",
                        "win": False,
                        "speeches": [
                            _speech(TEAM2["speakers"][0]["url"], 75.5, _breakdown(30.0, 30.5, 15.0)),
                            _speech(TEAM2["speakers"][1]["url"], 75.0, _breakdown(30.0, 30.0, 15.0)),
                            _speech(TEAM2["speakers"][2]["url"], 74.0, _breakdown(29.5, 29.5, 15.0)),
                            _speech(TEAM2["speakers"][0]["url"], 37.5, _breakdown(15.0, 15.0, 7.5)),
                        ],
                    },
                ],
            }
        ]
    },
    "confirmed": True,
    "discarded": False,
    "forfeit": False,
    "version": 2,
}

# Panellist ballot from Priya, using unmatched criteria names (Analysis/Delivery) so it
# should fall back to final_score only. The first row (76.3) is not a half-point score
# and should be skipped without aborting the rest of the sheet.
SUBMISSION_PRIYA = {
    "id": 12,
    "result": {
        "sheets": [
            {
                "adjudicator": ADJ_PRIYA["url"],
                "teams": [
                    {
                        "team": TEAM1["url"],
                        "side": "aff",
                        "win": True,
                        "speeches": [
                            _speech(TEAM1["speakers"][0]["url"], 76.3, _unmatched(76.3)),
                            _speech(TEAM1["speakers"][1]["url"], 74.5, _unmatched(74.5)),
                            _speech(TEAM1["speakers"][2]["url"], 75.0, _unmatched(75.0)),
                            _speech(TEAM1["speakers"][0]["url"], 38.0, _unmatched(38.0)),
                        ],
                    },
                    {
                        "team": TEAM2["url"],
                        "side": "neg",
                        "win": False,
                        "speeches": [
                            _speech(TEAM2["speakers"][0]["url"], 75.0, _unmatched(75.0)),
                            _speech(TEAM2["speakers"][1]["url"], 75.0, _unmatched(75.0)),
                            _speech(TEAM2["speakers"][2]["url"], 74.0, _unmatched(74.0)),
                            _speech(TEAM2["speakers"][0]["url"], 37.0, _unmatched(37.0)),
                        ],
                    },
                ],
            }
        ]
    },
    "confirmed": True,
    "discarded": False,
    "forfeit": False,
    "version": 1,
}


def register_routes(*, institutions_paginated: bool = True, ballots: bool = True, preferences=PREFERENCES_TWO_TEAM):
    respx.get(u(f"/api/v1/tournaments/{SLUG}")).mock(return_value=httpx.Response(200, json=TOURNAMENT))
    if preferences is None:
        respx.get(u(f"/api/v1/tournaments/{SLUG}/preferences")).mock(return_value=httpx.Response(403))
    else:
        respx.get(u(f"/api/v1/tournaments/{SLUG}/preferences")).mock(return_value=httpx.Response(200, json=preferences))
    if institutions_paginated:
        respx.get(u("/api/v1/institutions")).mock(
            return_value=httpx.Response(
                200, json={"count": len(INSTITUTIONS), "next": None, "previous": None, "results": INSTITUTIONS}
            )
        )
    else:
        respx.get(u("/api/v1/institutions")).mock(return_value=httpx.Response(200, json=INSTITUTIONS))
    respx.get(u(f"/api/v1/tournaments/{SLUG}/teams")).mock(
        return_value=httpx.Response(200, json=[TEAM1, TEAM2, TEAM3, TEAM4])
    )
    respx.get(u(f"/api/v1/tournaments/{SLUG}/adjudicators")).mock(
        return_value=httpx.Response(200, json=[ADJ_JORDAN, ADJ_PRIYA, ADJ_SAM])
    )
    respx.get(u(f"/api/v1/tournaments/{SLUG}/venues")).mock(return_value=httpx.Response(200, json=[VENUE_101]))
    respx.get(u(f"/api/v1/tournaments/{SLUG}/score-criteria")).mock(
        return_value=httpx.Response(
            200,
            json=[CRITERION_CONTENT, CRITERION_STYLE, CRITERION_STRATEGY, CRITERION_ANALYSIS, CRITERION_DELIVERY],
        )
    )
    respx.get(u(f"/api/v1/tournaments/{SLUG}/rounds")).mock(return_value=httpx.Response(200, json=[ROUND1, ROUND2]))
    respx.get(u(f"/api/v1/tournaments/{SLUG}/motions")).mock(return_value=httpx.Response(200, json=MOTIONS))
    respx.get(u(f"/api/v1/tournaments/{SLUG}/rounds/1/pairings")).mock(
        return_value=httpx.Response(200, json=[PAIRING_R1])
    )
    respx.get(u(f"/api/v1/tournaments/{SLUG}/rounds/2/pairings")).mock(
        return_value=httpx.Response(200, json=[PAIRING_R2_BP])
    )
    ballots_route = respx.get(
        u(f"/api/v1/tournaments/{SLUG}/rounds/1/pairings/1/ballots"), params={"confirmed": "true"}
    )
    if ballots:
        ballots_route.mock(
            return_value=httpx.Response(
                200, json=[SUBMISSION_JORDAN_V1, SUBMISSION_CONSENSUS_V2, SUBMISSION_PRIYA]
            )
        )
    return ballots_route


def _import_payload(**overrides) -> dict:
    payload = {"base_url": BASE_URL, "slug": SLUG}
    payload.update(overrides)
    return payload


def test_import_happy_path(client: TestClient, session: Session) -> None:
    with respx.mock:
        register_routes()
        resp = client.post(IMPORT_URL, json=_import_payload())

    assert resp.status_code == 201, resp.text
    report = resp.json()

    assert report["format"] == "two_team"
    assert report["institutions"] == 2
    assert report["teams"] == 4
    assert report["debaters"] == 9  # Andrew Ali and Bob Brooks reused, not double-counted
    assert report["judges"] == 3
    assert report["rounds"] == 2
    assert report["motions"] == 2
    assert report["debates"] == 1  # the 4-team round-2 pairing is unmappable
    assert report["ballots"] == 2  # Jordan (consensus, wins collision) + Priya
    assert report["speaker_scores"] == 15  # 8 (Jordan) + 7 valid of 8 (Priya, one invalid)
    skipped = report["skipped"]
    assert len(skipped) == 5
    assert any("kept first of 3 motions" in s for s in skipped)          # ROUND2
    assert any("expected 2 teams, got 4" in s for s in skipped)          # PAIRING_R2_BP
    assert any("duplicate ballot for judge" in s for s in skipped)       # Jordan v1 vs consensus v2
    assert any("invalid speaker score" in s for s in skipped)            # Priya's 76.3
    assert any("adjudicators had no institution" in s for s in skipped)  # new

    tournaments = client.get("/api/v1/tournaments").json()
    tournament = next(t for t in tournaments if t["slug"] == SLUG)
    assert tournament["id"] == report["tournament_id"]
    assert tournament["name"] == "Sample Tab"
    assert tournament["abbr"] == "Sample"
    assert tournament["date"] == "2024-06-01"  # earliest Round.starts_at
    assert tournament["format"] == "two_team"

    rounds = client.get(f"/api/v1/tournaments/{tournament['id']}/rounds").json()
    assert len(rounds) == 2
    round1 = next(r for r in rounds if r["seq"] == 1)

    debates = client.get(f"/api/v1/rounds/{round1['id']}/debates").json()
    assert len(debates) == 1
    debate = debates[0]
    assert debate["room"] == "Room 101"

    result = client.get(f"/api/v1/debates/{debate['id']}/result").json()
    assert result["winner"] == "prop"
    assert sorted(b["winner"] for b in result["ballots"]) == ["prop", "prop"]
    assert len(result["speakers"]) == 8

    alice_first = next(
        s for s in result["speakers"] if s["position"] == 1 and s["side"] == "prop"
    )
    # Priya's 76.3 row for this debater/position was an invalid (non-half-point) score
    # and was skipped, so only the consensus ballot's 76.0 landed.
    assert alice_first["average_score"] == 76.0


def test_import_no_api_key_sends_no_auth_header(client: TestClient, session: Session) -> None:
    with respx.mock:
        ballots_route = register_routes()
        resp = client.post(IMPORT_URL, json=_import_payload())
        assert resp.status_code == 201, resp.text
        tournament_call = respx.calls[0]
        assert "authorization" not in {k.lower() for k in tournament_call.request.headers.keys()}
        assert ballots_route.called


def test_import_with_api_key_sends_token_header() -> None:
    with respx.mock:
        register_routes()
        client_headers_seen = []

        def _capture(request):
            client_headers_seen.append(request.headers.get("authorization"))
            return httpx.Response(200, json=TOURNAMENT)

        respx.get(u(f"/api/v1/tournaments/{SLUG}")).mock(side_effect=_capture)

        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(engine)
        with Session(engine) as db_session:
            import_tournament(BASE_URL, SLUG, "abc", session=db_session, include_ballots=True)

        assert client_headers_seen == ["Token abc"]


def test_duplicate_slug_is_409(client: TestClient, session: Session) -> None:
    created = client.post("/api/v1/tournaments", json={"name": "Existing", "slug": SLUG})
    assert created.status_code == 201

    with respx.mock:
        # No routes registered: the pre-flight check must fail before any HTTP call.
        resp = client.post(IMPORT_URL, json=_import_payload())

    assert resp.status_code == 409


def test_include_ballots_false_skips_ballot_requests(client: TestClient, session: Session) -> None:
    with respx.mock:
        ballots_route = register_routes(ballots=False)
        resp = client.post(IMPORT_URL, json=_import_payload(include_ballots=False))
        assert resp.status_code == 201, resp.text
        assert ballots_route.call_count == 0

    report = resp.json()
    assert report["ballots"] == 0
    assert report["speaker_scores"] == 0


def test_upstream_401_maps_to_502(client: TestClient, session: Session) -> None:
    with respx.mock:
        respx.get(u(f"/api/v1/tournaments/{SLUG}")).mock(return_value=httpx.Response(401))
        resp = client.post(IMPORT_URL, json=_import_payload())
    assert resp.status_code == 502


def test_upstream_404_maps_to_404(client: TestClient, session: Session) -> None:
    with respx.mock:
        respx.get(u(f"/api/v1/tournaments/{SLUG}")).mock(return_value=httpx.Response(404))
        resp = client.post(IMPORT_URL, json=_import_payload())
    assert resp.status_code == 404


def test_get_list_handles_both_pagination_shapes() -> None:
    with respx.mock:
        respx.get(u("/bare-array")).mock(return_value=httpx.Response(200, json=[{"id": 1}, {"id": 2}]))
        respx.get(u("/paginated"), params={"offset": "0"}).mock(
            return_value=httpx.Response(
                200,
                json={"count": 3, "next": u("/paginated?offset=2"), "previous": None, "results": [{"id": 1}, {"id": 2}]},
            )
        )
        respx.get(u("/paginated"), params={"offset": "2"}).mock(
            return_value=httpx.Response(200, json={"count": 3, "next": None, "previous": None, "results": [{"id": 3}]})
        )

        tabby_client = _TabbycatClient(BASE_URL, SLUG)
        bare = tabby_client.get_list(u("/bare-array"))
        paginated = tabby_client.get_list(u("/paginated?offset=0"))
        tabby_client.close()

    assert [item["id"] for item in bare] == [1, 2]
    assert [item["id"] for item in paginated] == [1, 2, 3]


def _register_minimal(
    *, teams=None, institutions=None, adjudicators=None, rounds=None, motions=None, preferences=PREFERENCES_TWO_TEAM
):
    """Registers only the routes exercised by `_run_import` for a slug with no debates,
    so individual diagnostics can be tested without the full happy-path fixture set."""
    respx.get(u(f"/api/v1/tournaments/{SLUG}")).mock(return_value=httpx.Response(200, json=TOURNAMENT))
    if preferences is None:
        respx.get(u(f"/api/v1/tournaments/{SLUG}/preferences")).mock(return_value=httpx.Response(403))
    else:
        respx.get(u(f"/api/v1/tournaments/{SLUG}/preferences")).mock(return_value=httpx.Response(200, json=preferences))
    respx.get(u("/api/v1/institutions")).mock(return_value=httpx.Response(200, json=institutions or []))
    respx.get(u(f"/api/v1/tournaments/{SLUG}/teams")).mock(return_value=httpx.Response(200, json=teams or []))
    respx.get(u(f"/api/v1/tournaments/{SLUG}/adjudicators")).mock(
        return_value=httpx.Response(200, json=adjudicators or [])
    )
    respx.get(u(f"/api/v1/tournaments/{SLUG}/venues")).mock(return_value=httpx.Response(200, json=[]))
    respx.get(u(f"/api/v1/tournaments/{SLUG}/score-criteria")).mock(return_value=httpx.Response(200, json=[]))
    respx.get(u(f"/api/v1/tournaments/{SLUG}/rounds")).mock(return_value=httpx.Response(200, json=rounds or []))
    respx.get(u(f"/api/v1/tournaments/{SLUG}/motions")).mock(return_value=httpx.Response(200, json=motions or []))
    for round_data in rounds or []:
        respx.get(u(f"/api/v1/tournaments/{SLUG}/rounds/{round_data['seq']}/pairings")).mock(
            return_value=httpx.Response(200, json=[])
        )


def test_duplicate_speaker_on_same_team_is_skipped(client: TestClient, session: Session) -> None:
    team = {
        "id": 501,
        "url": u(f"/api/v1/tournaments/{SLUG}/teams/501"),
        "institution": None,
        "reference": "1",
        "short_reference": "1",
        "short_name": "Team Placeholder",
        "long_name": "Team Placeholder",
        "speakers": [
            _speaker(201, "Chageesha", "", None, None),
            _speaker(202, "Speaker", "", None, None),
            _speaker(203, "Speaker", "", None, None),
        ],
    }
    with respx.mock:
        _register_minimal(teams=[team])
        resp = client.post(IMPORT_URL, json=_import_payload())

    assert resp.status_code == 201, resp.text
    report = resp.json()
    assert report["teams"] == 1
    assert report["debaters"] == 2  # Chageesha + one "Speaker"; the second "Speaker" dedupes onto it
    assert any("duplicate" in s.lower() and "Team Placeholder" in s for s in report["skipped"])

    members = session.exec(select(TeamMember)).all()
    assert len(members) == 2


def test_describe_integrity_error_names_constraint() -> None:
    orig = sqlite3.IntegrityError("UNIQUE constraint failed: teammember.team_id, teammember.debater_id")
    exc = IntegrityError("INSERT INTO teammember ...", (152, 531), orig)

    detail = describe_integrity_error(exc)

    assert "teammember" in detail
    assert "team_id" in detail
    assert "debater_id" in detail
    assert "(152, 531)" in detail


def test_unresolved_institution_is_reported(client: TestClient, session: Session) -> None:
    team = {
        "id": 502,
        "url": u(f"/api/v1/tournaments/{SLUG}/teams/502"),
        "institution": u("/api/v1/institutions/99"),
        "reference": "1",
        "short_reference": "1",
        "short_name": "Orphan Team",
        "long_name": "Orphan Team",
        "speakers": [],
    }
    with respx.mock:
        _register_minimal(teams=[team])
        resp = client.post(IMPORT_URL, json=_import_payload())

    assert resp.status_code == 201, resp.text
    report = resp.json()
    assert any(u("/api/v1/institutions/99") in s and "institution_id" in s for s in report["skipped"])

    team_row = session.exec(select(Team)).first()
    assert team_row.institution_id is None


def test_round_without_motion_is_reported(client: TestClient, session: Session) -> None:
    round_with_motion = {
        "id": 601,
        "url": u(f"/api/v1/tournaments/{SLUG}/rounds/1"),
        "seq": 1,
        "name": "Round 1",
        "abbreviation": "R1",
        "stage": "P",
        "completed": True,
        "starts_at": None,
    }
    round_without_motion = {
        "id": 602,
        "url": u(f"/api/v1/tournaments/{SLUG}/rounds/2"),
        "seq": 2,
        "name": "Round 2",
        "abbreviation": "R2",
        "stage": "P",
        "completed": False,
        "starts_at": None,
    }
    motion = _motion(701, round_with_motion["url"], "Motion A")
    with respx.mock:
        _register_minimal(rounds=[round_with_motion, round_without_motion], motions=[motion])
        resp = client.post(IMPORT_URL, json=_import_payload())

    assert resp.status_code == 201, resp.text
    report = resp.json()
    assert report["motions"] == 1
    assert any("rounds had no motion" in s for s in report["skipped"])


def _clashing_teams() -> tuple[dict, dict]:
    team_a = {
        "id": 701,
        "url": u(f"/api/v1/tournaments/{SLUG}/teams/701"),
        "institution": u("/api/v1/institutions/99"),  # unresolved -> a skip is recorded before the crash
        "reference": "1",
        "short_reference": "1",
        "short_name": "Clashing Team",
        "long_name": "Clashing Team",
        "speakers": [],
    }
    team_b = {
        "id": 702,
        "url": u(f"/api/v1/tournaments/{SLUG}/teams/702"),
        "institution": None,
        "reference": "2",
        "short_reference": "2",
        "short_name": "Clashing Team",
        "long_name": "Clashing Team",  # same name as team_a -> IntegrityError on flush
        "speakers": [],
    }
    return team_a, team_b


def test_partial_skips_are_logged_before_rollback(client: TestClient, session: Session, caplog) -> None:
    team_a, team_b = _clashing_teams()

    with caplog.at_level(logging.DEBUG, logger="app.services.tabbycat"):
        with respx.mock:
            _register_minimal(teams=[team_a, team_b])
            resp = client.post(IMPORT_URL, json=_import_payload())

    assert resp.status_code == 409
    assert any("skipped (discarded by rollback)" in record.getMessage() for record in caplog.records)


def test_integrity_error_detail_names_the_constraint(client: TestClient, session: Session) -> None:
    team_a, team_b = _clashing_teams()

    with respx.mock:
        _register_minimal(teams=[team_a, team_b])
        resp = client.post(IMPORT_URL, json=_import_payload())

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "UNIQUE" in detail
    assert "team" in detail


def test_normalize_bp_side_accepts_live_tabbycat_values() -> None:
    # Confirmed against a real BP tournament's /rounds/{seq}/pairings response: Tabbycat's
    # live API returns literal "og"/"oo"/"cg"/"co" strings, not "aff"/"neg" for the first
    # two positions. Both "aff"/"neg" and the 0/1 ordinals are kept as accepted aliases.
    assert _normalize_bp_side("og") == BPSide.OG
    assert _normalize_bp_side("oo") == BPSide.OO
    assert _normalize_bp_side("cg") == BPSide.CG
    assert _normalize_bp_side("co") == BPSide.CO
    assert _normalize_bp_side("aff") == BPSide.OG
    assert _normalize_bp_side("neg") == BPSide.OO
    assert _normalize_bp_side(0) == BPSide.OG
    assert _normalize_bp_side(1) == BPSide.OO
    assert _normalize_bp_side("bye") is None


def test_bp_format_imports_four_team_pairing(client: TestClient, session: Session) -> None:
    with respx.mock:
        register_routes(preferences=PREFERENCES_BP)
        respx.get(
            u(f"/api/v1/tournaments/{SLUG}/rounds/2/pairings/2/ballots"), params={"confirmed": "true"}
        ).mock(return_value=httpx.Response(200, json=[SUBMISSION_BP_CONSENSUS]))
        resp = client.post(IMPORT_URL, json=_import_payload())

    assert resp.status_code == 201, resp.text
    report = resp.json()
    assert report["format"] == "bp"
    # PAIRING_R1 (2 teams) is unmappable under BP and skipped; only PAIRING_R2_BP imports.
    assert report["debates"] == 1
    assert report["ballots"] == 1
    assert report["speaker_scores"] == 8  # 4 teams x 2 speakers
    assert any("expected 4 distinct BP sides" in s for s in report["skipped"])

    tournaments = client.get("/api/v1/tournaments").json()
    tournament = next(t for t in tournaments if t["slug"] == SLUG)
    assert tournament["format"] == "bp"

    bp_debate = session.exec(select(BPDebate)).first()
    assert bp_debate is not None
    team_rows = {
        row.side.value: (row.rank, row.points)
        for row in session.exec(
            select(BPDebateTeam).where(BPDebateTeam.bp_debate_id == bp_debate.id)
        ).all()
    }
    assert team_rows["og"] == (1, 3)
    assert team_rows["co"] == (2, 2)
    assert team_rows["cg"] == (3, 1)
    assert team_rows["oo"] == (4, 0)


SUBMISSION_BP_ADVANCE_ONLY = {
    "id": 21,
    "result": {
        "sheets": [
            {
                "adjudicator": None,
                "teams": [
                    {"team": TEAM3["url"], "side": "og", "points": None, "rank": None, "win": True},
                    {"team": TEAM4["url"], "side": "oo", "points": None, "rank": None, "win": False},
                    {"team": TEAM1["url"], "side": "cg", "points": None, "rank": None, "win": True},
                    {"team": TEAM2["url"], "side": "co", "points": None, "rank": None, "win": False},
                ],
            }
        ]
    },
    "confirmed": True,
    "discarded": False,
    "forfeit": False,
    "version": 1,
}


def test_bp_ballot_advance_eliminate_fallback(client: TestClient, session: Session) -> None:
    # Confirmed against a real BP tournament's elimination rounds: Tabbycat sometimes
    # reports only a win/advance flag per team (no points/rank at all), e.g. 2 advance +
    # 2 eliminated in a quarter/semi. The importer should record that instead of
    # skipping the ballot outright.
    with respx.mock:
        register_routes(preferences=PREFERENCES_BP)
        respx.get(
            u(f"/api/v1/tournaments/{SLUG}/rounds/2/pairings/2/ballots"), params={"confirmed": "true"}
        ).mock(return_value=httpx.Response(200, json=[SUBMISSION_BP_ADVANCE_ONLY]))
        resp = client.post(IMPORT_URL, json=_import_payload())

    assert resp.status_code == 201, resp.text
    report = resp.json()
    assert report["format"] == "bp"
    assert report["debates"] == 1
    assert report["ballots"] == 1
    assert report["speaker_scores"] == 0  # no speeches recorded for advance-only ballots
    assert not any("no valid ranking" in s for s in report["skipped"])

    bp_debate = session.exec(select(BPDebate)).first()
    team_rows = {
        row.side.value: (row.rank, row.points, row.advanced)
        for row in session.exec(
            select(BPDebateTeam).where(BPDebateTeam.bp_debate_id == bp_debate.id)
        ).all()
    }
    assert team_rows["og"] == (None, None, True)
    assert team_rows["cg"] == (None, None, True)
    assert team_rows["oo"] == (None, None, False)
    assert team_rows["co"] == (None, None, False)


def test_preferences_403_falls_back_to_pairing_inference(client: TestClient, session: Session) -> None:
    with respx.mock:
        register_routes(preferences=None, ballots=False)
        resp = client.post(IMPORT_URL, json=_import_payload(include_ballots=False))

    assert resp.status_code == 201, resp.text
    report = resp.json()
    # First round's pairing (PAIRING_R1) has 2 teams, so the fallback infers two-team.
    assert report["format"] == "two_team"
    assert any("could not read tournament preferences" in s for s in report["skipped"])
