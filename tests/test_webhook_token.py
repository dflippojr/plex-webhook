"""The optional WEBHOOK_TOKEN: a missing or wrong token is a fixed 404 with nothing stored.

The token never appears in logs, audit rows, metrics or responses.
"""
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from prometheus_client import generate_latest

from conftest import WEBHOOK_TOKEN, drain, payload, rows

import app.main as main
from app import auth

ROOT = Path(__file__).resolve().parent.parent
WRONG = "synthetic-wrong-token-0123456789abcdefgh"
TOKENED = pytest.mark.parametrize("client", ["token"], indirect=True)
# The test's own HTTP client logs the URLs it requests; those records are not the service's.
CLIENT_LOGGERS = ("httpx", "httpcore")


def _service_log(caplog):
    return "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith(CLIENT_LOGGERS))


def _rejected_count():
    return main.WEBHOOK_REJECTED_TOTAL.labels(reason="invalid_token")._value.get()


def _raw_post(client, url, body):
    """POST without the token client's /webhook rewrite."""
    return TestClient.request(client, "POST", url, data={"payload": json.dumps(body)})


def _stored_events(client):
    return client.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]


@TOKENED
@pytest.mark.parametrize("url", [
    "/webhook",                              # no token
    f"/webhook/{WRONG}",                     # wrong token
    f"/webhook/{WEBHOOK_TOKEN[:-1]}",        # one character short
    f"/webhook/{WEBHOOK_TOKEN}x",            # one character long
    f"/webhook?token={WEBHOOK_TOKEN}",       # the query form is not accepted
], ids=["missing", "wrong", "prefix", "suffix", "query"])
def test_missing_or_wrong_token_is_404_and_stores_nothing(client, light_calls, caplog, url):
    caplog.set_level(logging.DEBUG)
    before = _rejected_count()
    response = _raw_post(client, url, payload("media.play"))
    drain()
    assert response.status_code == 404 and response.json() == {"detail": "Not Found"}
    assert _stored_events(client) == 0 and light_calls == []
    receipts = [r for r in rows(client) if r["action"] == "webhook.receipt"]
    assert len(receipts) == 1
    record = receipts[0]
    assert (record["outcome"], record["reason_code"]) == ("denied", "invalid_token")
    assert (record["actor_kind"], record["actor_id"], record["actor_verified"]) == ("anonymous", None, 0)
    assert record["target_kind"] == "webhook" and record["target_id"] is None
    assert _rejected_count() == before + 1
    assert "reason=webhook_rejected code=invalid_token" in caplog.text
    _assert_secrets_absent(client, response.text, _service_log(caplog))


@TOKENED
def test_right_token_is_accepted_and_dispatched(client, light_calls, caplog):
    caplog.set_level(logging.DEBUG)
    before = _rejected_count()
    response = client.post(f"/webhook/{WEBHOOK_TOKEN}", data={"payload": json.dumps(payload("media.play"))})
    drain()
    assert response.status_code == 200 and response.json() == {"status": "received", "event": "media.play"}
    assert _stored_events(client) == 1 and light_calls == [("dim", ["AA:BB"])]
    receipt = [r for r in rows(client) if r["action"] == "webhook.receipt"][0]
    assert (receipt["outcome"], receipt["reason_code"], receipt["actor_id"]) == ("received", "accepted", "plex-server")
    assert _rejected_count() == before
    _assert_secrets_absent(client, response.text, _service_log(caplog))


@TOKENED
def test_errors_after_the_token_check_do_not_echo_it(client, caplog):
    caplog.set_level(logging.DEBUG)
    url = f"/webhook/{WEBHOOK_TOKEN}"
    responses = [
        client.post(url, content=b"\xff", headers={"Content-Type": "multipart/form-data"}),  # 400 invalid form
        client.post(url, content=b"x", headers={"Content-Length": "999999999"}),            # 413
        client.get(url),                                                                     # 405
        client.post(url, data={"payload": "{not json"}),                                     # stored, rejected
    ]
    assert [r.status_code for r in responses] == [400, 413, 405, 200]
    _assert_secrets_absent(client, " ".join(r.text + str(r.headers) for r in responses), _service_log(caplog))


@TOKENED
def test_denials_share_the_capped_audit(client, monkeypatch):
    monkeypatch.setattr(main, "DENIAL_AUDIT_LIMIT", 2)
    before = _rejected_count()
    for _ in range(4):
        assert _raw_post(client, "/webhook", payload("media.play")).status_code == 404
    denied = [r for r in rows(client) if r["outcome"] == "denied"]
    assert len(denied) == 2 and _rejected_count() == before + 4


def test_without_a_token_the_tokened_route_does_not_exist(client, light_calls):
    assert main.WEBHOOK_TOKEN_DIGEST is None
    before = _rejected_count()
    response = client.post(f"/webhook/{WEBHOOK_TOKEN}", data={"payload": json.dumps(payload("media.play"))})
    drain()
    assert response.status_code == 404 and response.json() == {"detail": "Not Found"}
    assert _stored_events(client) == 0 and light_calls == []
    # No audit row was written: the trail's table was never even created.
    assert client.conn.execute("SELECT name FROM sqlite_master WHERE name = 'audit_events'").fetchone() is None
    assert _rejected_count() == before
    # The untokened route keeps today's behaviour.
    assert client.post("/webhook", data={"payload": json.dumps(payload("media.play"))}).status_code == 200


def test_unset_token_logs_a_startup_warning(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    for value in (None, ""):
        if value is None:
            monkeypatch.delenv("WEBHOOK_TOKEN", raising=False)
        else:
            monkeypatch.setenv("WEBHOOK_TOKEN", value)
        caplog.clear()
        assert main._webhook_token_digest() is None
        assert "reason=webhook_token_unset" in caplog.text and "set WEBHOOK_TOKEN" in caplog.text


def test_valid_token_is_kept_only_as_a_digest(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("WEBHOOK_TOKEN", WEBHOOK_TOKEN)
    digest = main._webhook_token_digest()
    assert isinstance(digest, bytes) and WEBHOOK_TOKEN.encode() not in digest and caplog.text == ""
    assert auth.webhook_token_matches(WEBHOOK_TOKEN, digest)
    assert not auth.webhook_token_matches(WRONG, digest) and not auth.webhook_token_matches("", digest)


@pytest.mark.parametrize("value", [
    "a" * 31,                       # one short of the minimum
    "short",
    "synthetic token with spaces 0123456789",
    "synthetic/token/with/slashes/0123456789",
    "synthetic-token-%2F-0123456789abcdef",
    " " * 40,
], ids=["31-chars", "short", "spaces", "slashes", "percent", "blank"])
def test_unusable_token_is_refused(monkeypatch, value):
    monkeypatch.setenv("WEBHOOK_TOKEN", value)
    with pytest.raises(auth.WebhookTokenError) as raised:
        main._webhook_token_digest()
    assert value not in str(raised.value) and "at least 32 characters" in str(raised.value)


def test_minimum_length_token_is_accepted(monkeypatch):
    monkeypatch.setenv("WEBHOOK_TOKEN", "a-Z_0.9~" * 4)
    assert auth.webhook_token_digest() is not None


def test_short_token_stops_the_server_at_startup(tmp_path):
    """The real entry point: uvicorn exits before binding, and its output does not show the value."""
    value = "synthetic-31-character-token-00"
    assert len(value) == 31
    env = {**os.environ, "WEBHOOK_TOKEN": value, "DB_PATH": str(tmp_path / "plex_events.db")}
    result = subprocess.run(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "0"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "WebhookTokenError" in output and "at least 32 characters" in output
    assert value not in output


@TOKENED
def test_access_log_never_shows_the_token(client, caplog):
    """Serve the app with uvicorn on loopback and read its access log."""
    import uvicorn

    caplog.set_level(logging.INFO)
    server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=0, log_config=None,
                                           access_log=True, lifespan="off"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 20
        while not server.started:
            assert time.monotonic() < deadline and thread.is_alive(), "uvicorn did not start"
            time.sleep(0.05)
        port = server.servers[0].sockets[0].getsockname()[1]
        statuses = []
        for path in (f"/webhook/{WEBHOOK_TOKEN}", f"/webhook/{WRONG}", f"/webhook?token={WEBHOOK_TOKEN}"):
            statuses.append(_raw_http_post(port, path))
    finally:
        server.should_exit = True
        thread.join(timeout=20)
    assert statuses == [200, 404, 404]  # an empty form is stored as missing_payload
    access = [r.getMessage() for r in caplog.records if r.name == "uvicorn.access"]
    assert len(access) == 3
    assert ['"POST /webhook/[redacted] HTTP/1.1" 200', '"POST /webhook/[redacted] HTTP/1.1" 404',
            '"POST /webhook?[redacted] HTTP/1.1" 404'] == [line.split(" - ", 1)[1] for line in access]
    _assert_secrets_absent(client, "", caplog.text)


def _raw_http_post(port, path):
    """One empty form POST over a loopback socket (http.client would resolve the name); returns the status."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(10)
        sock.connect(("127.0.0.1", port))
        sock.sendall(f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/x-www-form-urlencoded"
                     "\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
        reply = b""
        while chunk := sock.recv(4096):
            reply += chunk
    return int(reply.split(b" ", 2)[1])


@pytest.mark.parametrize("args,expected", [
    (("127.0.0.1:1", "POST", "/webhook/abc", "1.1", 200), ("127.0.0.1:1", "POST", "/webhook/[redacted]", "1.1", 200)),
    (({"path": "/webhook?token=abc", "status": 404},), {"path": "/webhook?[redacted]", "status": 404}),
    ((), ()),
    (None, None),
    (("GET", "/webhook", 200), ("GET", "/webhook", 200)),  # nothing after /webhook: unchanged
], ids=["tuple", "mapping", "empty-args", "none-args", "bare-route"])
def test_redaction_filter_handles_every_record_shape(args, expected):
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "%s POST /webhook/abc", args, None)
    assert main._redact_webhook_paths(record) is True
    assert record.msg == "%s POST /webhook/[redacted]" and record.args == expected


def _assert_secrets_absent(client, response_text, log_text):
    """Neither the configured token nor a presented wrong one reaches responses, logs, audit rows or metrics."""
    trail = json.dumps(rows(client))
    events = json.dumps(client.conn.execute("SELECT * FROM events").fetchall())
    metrics = generate_latest().decode()
    for secret in (WEBHOOK_TOKEN, WEBHOOK_TOKEN[:-1], WRONG):
        for where, text in (("response", response_text), ("log", log_text), ("audit", trail),
                            ("events", events), ("metrics", metrics)):
            assert secret not in text, where
