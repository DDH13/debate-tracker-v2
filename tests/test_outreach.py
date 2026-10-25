from datetime import date

from fastapi.testclient import TestClient

API = "/api/v1"
ADMIN = f"{API}/admin"
THIS_YEAR = date.today().year


def _lead(client: TestClient, **fields) -> dict:
    resp = client.post(f"{ADMIN}/outreach", json=fields)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _contacts(client: TestClient, institution_id: int) -> list[dict]:
    return client.get(f"{ADMIN}/school-contacts", params={"institution_id": institution_id}).json()


def test_lead_needs_a_school_name_or_link(client: TestClient) -> None:
    assert client.post(f"{ADMIN}/outreach", json={"contact_name": "Ms Silva"}).status_code == 422
    assert client.post(f"{ADMIN}/outreach", json={"school_name": "   "}).status_code == 422
    lead = _lead(client, school_name="Lyceum", channel="email")
    assert lead["status"] == "new"
    # Clearing the name on a lead with no link is refused too.
    assert client.patch(f"{ADMIN}/outreach/{lead['id']}", json={"school_name": None}).status_code == 422


def test_outreach_status_filter(client: TestClient) -> None:
    _lead(client, school_name="A")
    _lead(client, school_name="B", status="declined")
    open_leads = client.get(f"{ADMIN}/outreach", params={"status": "new"}).json()
    assert [lead["school_name"] for lead in open_leads] == ["A"]


def test_convert_creates_school_membership_and_contact(client: TestClient) -> None:
    lead = _lead(
        client,
        school_name="st. john's college",
        contact_name="Ms Silva",
        contact_email="silva@example.com",
    )
    resp = client.post(
        f"{ADMIN}/outreach/{lead['id']}/convert", json={"fee_lkr": 5000, "reason": "Workshop"}
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["institution_created"] and result["membership_created"] and result["contact_created"]
    assert result["institution"]["name"] == "St. John's College"  # tidied case
    assert result["institution"]["kind"] == "school"
    assert result["membership"]["year"] == THIS_YEAR
    assert result["membership"]["fee_lkr"] == "5000.00"
    assert result["membership"]["registered_on"] == date.today().isoformat()
    assert result["lead"]["status"] == "joined"
    assert result["lead"]["institution_id"] == result["institution"]["id"]

    [contact] = _contacts(client, result["institution"]["id"])
    assert (contact["name"], contact["role"], contact["is_primary"]) == ("Ms Silva", "point_of_contact", True)
    assert contact["email"] == "silva@example.com"

    summary = client.get(f"{ADMIN}/schools/{result['institution']['id']}").json()["summary"]
    assert summary["standing"]["status"] == "member"

    again = client.post(f"{ADMIN}/outreach/{lead['id']}/convert")
    assert again.status_code == 409


def test_convert_matches_existing_institution_by_key_and_reuses_membership(
    client: TestClient,
) -> None:
    existing = client.post(f"{API}/institutions", json={"name": "St Johns College"}).json()
    client.post(f"{ADMIN}/memberships", json={"institution_id": existing["id"], "year": 2025})
    client.post(
        f"{ADMIN}/school-contacts",
        json={"institution_id": existing["id"], "name": "MS SILVA", "is_primary": True},
    )
    lead = _lead(client, school_name="St. John's College", contact_name="Ms Silva")

    result = client.post(f"{ADMIN}/outreach/{lead['id']}/convert", json={"year": 2025}).json()
    assert result["institution"]["id"] == existing["id"]
    assert not result["institution_created"]
    assert not result["membership_created"]
    assert not result["contact_created"]  # same person by name key
    # An existing institution with no kind is marked as a school.
    assert result["institution"]["kind"] == "school"
    assert len(_contacts(client, existing["id"])) == 1


def test_convert_uses_explicit_or_linked_institution(client: TestClient) -> None:
    linked = client.post(f"{API}/institutions", json={"name": "Linked School", "kind": "other"}).json()
    chosen = client.post(f"{API}/institutions", json={"name": "Chosen School"}).json()

    lead = _lead(client, institution_id=linked["id"], contact_name="Mr Fernando")
    result = client.post(f"{ADMIN}/outreach/{lead['id']}/convert").json()
    assert result["institution"]["id"] == linked["id"]
    assert result["institution"]["kind"] == "other"  # an explicit kind is kept
    assert result["contact_created"]

    lead = _lead(client, school_name="Something Else")
    result = client.post(
        f"{ADMIN}/outreach/{lead['id']}/convert", json={"institution_id": chosen["id"]}
    ).json()
    assert result["institution"]["id"] == chosen["id"]
    assert not result["institution_created"]

    lead = _lead(client, school_name="Whatever")
    missing = client.post(f"{ADMIN}/outreach/{lead['id']}/convert", json={"institution_id": 9999})
    assert missing.status_code == 404
    assert client.post(f"{ADMIN}/outreach/9999/convert").status_code == 404
