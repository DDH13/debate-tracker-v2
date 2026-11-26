"""Personal fields (email, phone, birthdate, gender) must never reach an open endpoint."""

from fastapi.testclient import TestClient

from app.main import app

PRIVATE_FIELDS = {"email", "phone", "birthdate", "gender"}
# Signed-in users' own account data; not part of the open API.
SIGNED_IN_PREFIXES = ("/api/v1/admin", "/api/v1/auth", "/api/v1/me")


def _schema_properties(schema: dict, components: dict, seen: set[str]) -> set[str]:
    """Every property name reachable from `schema`, following $refs, unions and arrays."""
    found: set[str] = set()
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        if name in seen:
            return found
        seen.add(name)
        return _schema_properties(components[name], components, seen)
    found |= set(schema.get("properties", {}))
    for prop in schema.get("properties", {}).values():
        found |= _schema_properties(prop, components, seen)
    for key in ("items", "additionalProperties"):
        if isinstance(schema.get(key), dict):
            found |= _schema_properties(schema[key], components, seen)
    for key in ("anyOf", "oneOf", "allOf"):
        for sub in schema.get(key, []):
            found |= _schema_properties(sub, components, seen)
    return found


def test_no_open_get_response_schema_has_a_personal_field() -> None:
    openapi = app.openapi()
    components = openapi["components"]["schemas"]
    offenders: list[str] = []
    checked = 0
    for path, methods in openapi["paths"].items():
        if path.startswith(SIGNED_IN_PREFIXES) or "get" not in methods:
            continue
        for response in methods["get"].get("responses", {}).values():
            schema = response.get("content", {}).get("application/json", {}).get("schema")
            if not schema:
                continue
            checked += 1
            leaked = _schema_properties(schema, components, set()) & PRIVATE_FIELDS
            if leaked:
                offenders.append(f"GET {path}: {sorted(leaked)}")
    assert checked > 20
    assert not offenders, "\n".join(offenders)


def test_planted_personal_values_never_appear_in_public_responses(
    client: TestClient, anon_client: TestClient
) -> None:
    debater = client.post(
        "/api/v1/debaters",
        json={
            "full_name": "Sentinel Debater",
            "email": "sentinel-debater@secret.example",
            "phone": "0770000111",
            "birthdate": "2001-02-03",
            "gender": "sentinel-gender",
        },
    ).json()
    judge = client.post(
        "/api/v1/judges",
        json={
            "full_name": "Sentinel Judge",
            "email": "sentinel-judge@secret.example",
            "phone": "0770000222",
            "birthdate": "1980-04-05",
            "gender": "sentinel-gender",
        },
    ).json()
    # Board-only records still carry them.
    assert client.get(f"/api/v1/admin/debaters/{debater['id']}").json()["email"]
    assert client.get(f"/api/v1/admin/judges/{judge['id']}").json()["phone"]

    needles = ("secret.example", "0770000111", "0770000222", "2001-02-03", "1980-04-05", "sentinel-gender")
    paths = [
        "/api/v1/debaters",
        f"/api/v1/debaters/{debater['id']}",
        "/api/v1/judges",
        f"/api/v1/judges/{judge['id']}",
        "/api/v1/debaters?q=Sentinel",
    ]
    for path in paths:
        resp = anon_client.get(path)
        assert resp.status_code == 200, path
        assert not any(n in resp.text for n in needles), path
        for field in PRIVATE_FIELDS:
            assert f'"{field}"' not in resp.text, f"{path} exposes {field}"


def test_moved_routes_are_board_only(anon_client: TestClient, volunteer_client: TestClient) -> None:
    for path in (
        "/api/v1/debaters/merge-info",
        "/api/v1/debaters/duplicates",
        "/api/v1/judges/merge-info",
        "/api/v1/judges/duplicates",
        "/api/v1/institutions/merge-info",
    ):
        # No longer public routes: the int path parameter claims or nothing matches.
        assert anon_client.get(path).status_code in (404, 422), path
    for path in (
        "/api/v1/admin/debaters/duplicates",
        "/api/v1/admin/debaters/merge-info",
        "/api/v1/admin/judges/duplicates",
        "/api/v1/admin/judges/merge-info",
        "/api/v1/admin/institutions/merge-info",
        "/api/v1/admin/debaters",
        "/api/v1/admin/judges",
    ):
        assert anon_client.get(path).status_code == 401, path
        assert volunteer_client.get(path).status_code == 403, path
