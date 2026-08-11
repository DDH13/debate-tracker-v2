import httpx
import respx
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.services.tabbycat import _TabbycatClient, import_tournament

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


def _motion(id_: int, seq: int, text: str) -> dict:
    return {
        "id": id_,
        "url": u(f"/api/v1/tournaments/{SLUG}/motions/{id_}"),
        "text": text,
        "reference": text[:20],
        "info_slide": "",
        "info_slide_plain": "",
        "seq": seq,
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
    "motions": [_motion(1, 1, "This House Would ban social media for minors.")],
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
    "motions": [
        _motion(2, 1, "Motion B"),
        _motion(3, 2, "Motion C"),
        _motion(4, 3, "Motion D"),
    ],
}

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
    "adjudicators": None,
    "flags": [],
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


def register_routes(*, institutions_paginated: bool = True, ballots: bool = True):
    respx.get(u(f"/api/v1/tournaments/{SLUG}")).mock(return_value=httpx.Response(200, json=TOURNAMENT))
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

    assert report["institutions"] == 2
    assert report["teams"] == 4
    assert report["debaters"] == 9  # Andrew Ali and Bob Brooks reused, not double-counted
    assert report["judges"] == 3
    assert report["rounds"] == 2
    assert report["motions"] == 2
    assert report["debates"] == 1  # the 4-team round-2 pairing is unmappable
    assert report["ballots"] == 2  # Jordan (consensus, wins collision) + Priya
    assert report["speaker_scores"] == 15  # 8 (Jordan) + 7 valid of 8 (Priya, one invalid)
    assert len(report["skipped"]) == 4

    tournaments = client.get("/api/v1/tournaments").json()
    tournament = next(t for t in tournaments if t["slug"] == SLUG)
    assert tournament["id"] == report["tournament_id"]
    assert tournament["name"] == "Sample Tab"
    assert tournament["abbr"] == "Sample"
    assert tournament["date"] == "2024-06-01"  # earliest Round.starts_at

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
