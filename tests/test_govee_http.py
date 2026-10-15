"""Real webhook-to-Govee journeys with only device network boundaries faked."""
import json
import socket
from types import SimpleNamespace

import requests

from app import db, lights
from conftest import drain, payload, post

# Capture the real backend before the client fixture installs its light recorders.
_prepare, _read_state, _apply_action = lights.prepare, lights.read_state, lights.apply_action


def test_webhook_batches_discovery_and_restores_three_lights(client, rooms_file, registry, monkeypatch, tmp_path):
    from app import main

    rooms_file.write_text("""
rooms:
  cinema:
    dim_brightness_percent: 20
    restore_brightness_percent: 100
    plex_clients:
      - {title: Synthetic TV, uuid: synthetic-player}
    lights:
      - {brand: govee, id: G0, model: H6159}
      - {brand: govee, id: G1, model: H6159}
      - {brand: govee, id: G2, model: H6159}
""", encoding="utf-8")
    registry.reload()
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "restore.db")
    monkeypatch.setattr(lights, "_secrets_cache", {})
    monkeypatch.setenv("GOVEE_API_KEY", "synthetic-cloud-key")
    # Explicitly undo all three light backend recorders from the client fixture.
    monkeypatch.setattr(lights, "prepare", _prepare)
    monkeypatch.setattr(lights, "read_state", _read_state)
    monkeypatch.setattr(lights, "apply_action", _apply_action)
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(lights, "_clock", lambda: clock.now)
    state = {device: {"powerState": "on", "brightness": 70} for device in ("G0", "G1", "G2")}
    datagrams, sockets, commands = [], [], []

    class DiscoverySocket:
        def __init__(self, *args):
            self.closed = False
            sockets.append(self)

        def setsockopt(self, *args):
            pass

        def bind(self, *args):
            pass

        def settimeout(self, *args):
            pass

        def sendto(self, data, address):
            datagrams.append((json.loads(data), address))

        def recvfrom(self, size):
            raise socket.timeout()

        def close(self):
            self.closed = True

    # Keep asyncio/TestClient's real sockets and the global non-loopback guard intact.
    monkeypatch.setattr(lights, "socket", SimpleNamespace(
        socket=DiscoverySocket, AF_INET=socket.AF_INET, SOCK_DGRAM=socket.SOCK_DGRAM,
        SOL_SOCKET=socket.SOL_SOCKET, SO_REUSEADDR=socket.SO_REUSEADDR, timeout=socket.timeout,
    ))

    def cloud_get(url, *, headers, params, timeout):
        assert url == lights.GOVEE_CLOUD_STATE_URL
        assert headers["Govee-API-Key"] == "synthetic-cloud-key"
        assert params["model"] == "H6159"
        body = {"data": {"properties": [dict(state[params["device"]])]}}
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: body)

    def cloud_put(url, *, headers, json, timeout):
        assert url == lights.GOVEE_CLOUD_API_URL
        assert headers["Govee-API-Key"] == "synthetic-cloud-key"
        assert json["model"] == "H6159"
        device, command = json["device"], json["cmd"]
        commands.append((device, command["name"], command["value"]))
        field = "powerState" if command["name"] == "turn" else command["name"]
        state[device][field] = command["value"]
        return SimpleNamespace(raise_for_status=lambda: None)

    monkeypatch.setattr(requests, "get", cloud_get)
    monkeypatch.setattr(requests, "put", cloud_put)
    try:
        response = post(client, payload("media.play", "Synthetic TV", "synthetic-player"))
        assert response.status_code == 200
        drain()
        dim_commands = [(device, name, value) for device in state
                        for name, value in (("turn", "on"), ("brightness", 20))]
        assert commands == dim_commands
        assert all(value == {"powerState": "on", "brightness": 20} for value in state.values())
        assert [db.get_restore_record("cinema", device) for device in state] == [
            {"was_off": False, "restore_percent": 70, "dim_percent": 20}
        ] * 3

        clock.now += 59  # stop inside the 60-second discovery-miss TTL
        response = post(client, payload("media.stop", "Synthetic TV", "synthetic-player"))
        assert response.status_code == 200
        drain()
        restore_commands = [(device, name, value) for device in state
                            for name, value in (("turn", "on"), ("brightness", 70))]
        assert commands == dim_commands + restore_commands
        assert all(value == {"powerState": "on", "brightness": 70} for value in state.values())
        assert db.rooms_with_restore_records() == set()
        assert datagrams == [(
            {"msg": {"cmd": "scan", "data": {"account_topic": "reserve"}}},
            (lights.GOVEE_LAN_SCAN_MULTICAST_ADDR, lights.GOVEE_LAN_SCAN_PORT),
        )]
        assert all(sock.closed for sock in sockets)

        receipts = client.conn.execute(
            "SELECT correlation_id, target_id, reason_code FROM audit_events "
            "WHERE action = 'webhook.receipt' ORDER BY id"
        ).fetchall()
        assert len(receipts) == 2
        assert len({row[0] for row in receipts}) == 2
        for (correlation, event_id, reason), action in zip(receipts, ("dim", "restore")):
            assert reason == "accepted"
            results = client.conn.execute(
                "SELECT target_id, outcome, reason_code, detail FROM audit_events "
                "WHERE action = 'light.result' AND correlation_id = ? ORDER BY id", (correlation,)
            ).fetchall()
            assert [(target, outcome, reason) for target, outcome, reason, _ in results] == [
                (device, "request_accepted_by_transport", "cloud_accepted") for device in state
            ]
            for _, _, _, raw_detail in results:
                detail = json.loads(raw_detail)
                assert detail["transport"] == "cloud"
                assert detail["request"] == action
                assert str(detail["event_id"]) == event_id
    finally:
        # Settle queued work before monkeypatch restores network/backend boundaries.
        drain()
        for event in ("media.play", "media.stop"):
            main.EVENTS_TOTAL.remove(event, "Synthetic TV", "known")
