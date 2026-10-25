from fastapi.testclient import TestClient

ADMIN = "/api/v1/admin"


def test_people_crud_and_filters(client: TestClient) -> None:
    ann = client.post(f"{ADMIN}/people", json={"name": "Ann", "kind": "board", "email": "ann@example.com"})
    assert ann.status_code == 201, ann.text
    client.post(f"{ADMIN}/people", json={"name": "Bo", "kind": "volunteer", "active": False})
    assert client.post(f"{ADMIN}/people", json={"name": ""}).status_code == 422

    names = lambda **params: [p["name"] for p in client.get(f"{ADMIN}/people", params=params).json()]  # noqa: E731
    assert names() == ["Ann", "Bo"]
    assert names(kind="volunteer") == ["Bo"]
    assert names(active=True) == ["Ann"]

    person_id = ann.json()["id"]
    resp = client.patch(f"{ADMIN}/people/{person_id}", json={"phone": "+94770000000"})
    assert resp.json()["phone"] == "+94770000000"
    assert client.patch(f"{ADMIN}/people/{person_id}", json={"name": None}).status_code == 422
    assert client.delete(f"{ADMIN}/people/{person_id}").status_code == 204


def test_user_can_be_linked_to_a_person(client: TestClient) -> None:
    person = client.post(f"{ADMIN}/people", json={"name": "Ann", "kind": "volunteer"}).json()
    user = client.post(
        f"{ADMIN}/users",
        json={"email": "ann@example.com", "name": "Ann", "role": "volunteer", "password": "long-enough"},
    ).json()
    assert user["person_id"] is None

    assert client.patch(f"{ADMIN}/users/{user['id']}", json={"person_id": 999}).status_code == 404
    linked = client.patch(f"{ADMIN}/users/{user['id']}", json={"person_id": person["id"]})
    assert linked.status_code == 200
    assert linked.json()["person_id"] == person["id"]

    # Deleting the person unlinks the account rather than blocking.
    assert client.delete(f"{ADMIN}/people/{person['id']}").status_code == 204
    assert client.get(f"{ADMIN}/users/{user['id']}").json()["person_id"] is None

    other = client.post(f"{ADMIN}/people", json={"name": "Other"}).json()
    client.patch(f"{ADMIN}/users/{user['id']}", json={"person_id": other["id"]})
    unlinked = client.patch(f"{ADMIN}/users/{user['id']}", json={"person_id": None})
    assert unlinked.json()["person_id"] is None


def test_user_can_be_created_linked_to_a_person(client: TestClient) -> None:
    person = client.post(f"{ADMIN}/people", json={"name": "Bo"}).json()
    body = {"email": "bo@example.com", "name": "Bo", "role": "volunteer", "password": "long-enough"}
    assert client.post(f"{ADMIN}/users", json={**body, "person_id": 999}).status_code == 404
    created = client.post(f"{ADMIN}/users", json={**body, "person_id": person["id"]})
    assert created.status_code == 201
    assert created.json()["person_id"] == person["id"]
