import json

import pytest
from prometheus_client import generate_latest

from conftest import ADMIN_TOKEN, drain, payload
from test_audit import records
from test_audit_http import rows

ROUTES = [("get", "/rooms", "rooms.read"), ("get", "/clients", "clients.read"),
          ("post", "/rooms/validate", "rooms.validate"), ("post", "/rooms/reload", "rooms.reload")]
SENTINEL = "SENTINEL-attempted-credential"


def call(client, method, path, headers=None):
    return client.request(method, path, headers=headers or {}) if not headers else \
        client.request(method, path, headers=headers)


@pytest.fixture
def anon(client):
    client.headers.pop("Authorization")
    return client


@pytest.mark.parametrize("method,path,action", ROUTES)
@pytest.mark.parametrize("headers,status,code", [
    ({}, 401, "missing_credential"),
    ({"Authorization": f"Bearer {SENTINEL}"}, 401, "invalid_credential"),
    ({"Authorization": f"Basic {ADMIN_TOKEN}"}, 401, "missing_credential"),
    ({"Authorization": "Bearer "}, 401, "missing_credential"),
    ({"X-Actor": "owner-admin", "X-Forwarded-For": "127.0.0.1", "X-Admin-Token": ADMIN_TOKEN}, 401, "missing_credential"),
])
def test_denied(anon, rooms_file, light_calls, method, path, action, headers, status, code):
    before = dict(anon.get("/health").json())  # health stays public
    rooms_file.write_text("rooms: {den: {}}", encoding="utf-8")
    response = anon.request(method, path, headers=headers)
    assert before == {"status": "ok"} and response.status_code == status
    assert response.json() == {"detail": "Admin credential required"}
    assert SENTINEL not in response.text and ADMIN_TOKEN not in response.text
    assert set(anon.get("/rooms", headers=anon.admin).json()["rooms"]) == {"living_room", "bedroom"}
    record = [r for r in rows(anon) if r["action"] == action][0]
    assert (record["outcome"], record["reason_code"]) == ("denied", code)
    assert record["actor_kind"] == "anonymous" and record["actor_id"] is None and record["actor_verified"] == 0
    assert SENTINEL not in json.dumps(rows(anon)) and light_calls == []


def test_unset_or_weak_token_fails_closed(anon, monkeypatch):
    for value in (None, "", "short"):
        if value is None:
            monkeypatch.delenv("ADMIN_API_TOKEN")
        else:
            monkeypatch.setenv("ADMIN_API_TOKEN", value)
        response = anon.post("/rooms/reload", headers={"Authorization": f"Bearer {value or 'x'}"})
        assert response.status_code == 503 and response.json() == {"detail": "Admin authentication unavailable"}
    assert {r["reason_code"] for r in rows(anon)} == {"auth_unconfigured"}
    assert anon.get("/health").status_code == 200 and anon.get("/metrics").status_code == 200


@pytest.mark.parametrize("method,path,action", ROUTES[2:])
def test_valid_token_attributes_verified_admin(client, method, path, action):
    response = client.request(method, path, headers={"X-Actor": "mallory", "X-Forwarded-For": "8.8.8.8"})
    assert response.status_code == 200
    record = [r for r in rows(client) if r["action"] == action][0]
    assert record["actor_kind"] == "admin_token" and record["actor_id"] == "owner-admin"
    assert record["actor_verified"] == 1 and "mallory" not in json.dumps(record)


def test_valid_reads(client):
    assert client.get("/rooms").status_code == 200 and client.get("/clients").status_code == 200


def test_rejected_admin_operation_is_attributed(client, rooms_file):
    rooms_file.write_text("rooms: [", encoding="utf-8")
    assert client.post("/rooms/reload").status_code == 422
    record = rows(client)[0]
    assert (record["outcome"], record["actor_id"], record["actor_verified"]) == ("rejected", "owner-admin", 1)


def test_rotation_requires_new_token(client, monkeypatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "rotated-synthetic-token-9876543210")
    assert client.get("/rooms").status_code == 401
    assert client.get("/rooms", headers={"Authorization": "Bearer rotated-synthetic-token-9876543210"}).status_code == 200


def test_webhook_stays_unauthenticated_and_plex_server_actor(anon, light_calls):
    body = payload("media.play", account="owner-admin")
    response = anon.post("/webhook", data={"payload": json.dumps(body)},
                         headers={"X-Actor": "owner-admin", "Authorization": f"Bearer {SENTINEL}"})
    drain()
    assert response.status_code == 200 and light_calls
    record = rows(anon)[0]
    assert (record["actor_kind"], record["actor_id"], record["actor_verified"]) == ("plex_server", "plex-server", 0)


def test_denial_audit_is_bounded_and_counted(anon, monkeypatch):
    import app.main as main
    monkeypatch.setattr(main, "DENIAL_AUDIT_LIMIT", 3)
    monkeypatch.setitem(main._denial_window, "start", 0.0)
    monkeypatch.setitem(main._denial_window, "count", 0)
    for _ in range(10):
        anon.get("/rooms", headers={"Authorization": f"Bearer {SENTINEL}"})
    assert len([r for r in rows(anon) if r["outcome"] == "denied"]) == 3
    metrics = generate_latest().decode()
    assert 'plex_webhook_admin_denials_total{reason="invalid_credential"}' in metrics
    assert SENTINEL not in metrics and ADMIN_TOKEN not in metrics


def test_credentials_never_logged(anon, caplog):
    anon.get("/rooms", headers={"Authorization": f"Bearer {SENTINEL}"})
    anon.get("/rooms", headers=anon.admin)
    assert SENTINEL not in caplog.text and ADMIN_TOKEN not in caplog.text
    assert "reason=admin_denied code=invalid_credential" in caplog.text
