from conftest import payload
import pytest

from app.rooms import EMPTY_STATE, RoomConfigError, RoomConfigUnavailable, RoomRegistry


def test_resolves_by_uuid(registry):
    assert registry.resolve_room(payload("media.play", title="whatever", uuid="uuid-living")) == "living_room"


def test_resolves_by_title_case_and_whitespace_insensitive(registry):
    assert registry.resolve_room(payload("media.play", title="BEDROOM apple tv")) == "bedroom"
    assert registry.resolve_room(payload("media.play", title="  living room tv ")) == "living_room"


def test_uuid_wins_over_title(registry):
    assert registry.resolve_room(payload("media.play", title="Living Room TV", uuid="uuid-bed-tv")) == "bedroom"


def test_unknown_or_missing_player(registry):
    assert registry.resolve_room(payload("media.play", title="Kitchen")) is None
    assert registry.resolve_room({}) is None
    assert registry.resolve_room(None) is None


def test_lights_for(registry):
    assert registry.lights_for("bedroom")[0]["id"] == "plug1"
    assert registry.lights_for("nope") == []


def test_missing_startup_config_disables_dispatcher(tmp_path):
    from app.rooms import RoomRegistry

    reg = RoomRegistry(tmp_path / "absent.yaml")
    assert reg.rooms == {}
    assert reg.resolve_room(payload("media.play")) is None


INVALID_CANDIDATES = [
    ("rooms: [private-value", "$", "invalid_yaml"),
    ("[]", "$", "expected_mapping"),
    ("false", "$", "expected_mapping"),
    ("rooms: null", "rooms", "expected_mapping"),
    ("rooms: []", "rooms", "expected_mapping"),
    ("rooms: {den: null}", "rooms.den", "expected_mapping"),
    ("rooms: {den: {plex_clients: null}}", "rooms.den.plex_clients", "expected_list"),
    ("rooms: {den: {lights: {}}}", "rooms.den.lights", "expected_list"),
    ("rooms: {den: {plex_clients: [42]}}", "rooms.den.plex_clients[0]", "expected_mapping"),
    ("rooms: {den: {lights: [private-value]}}", "rooms.den.lights[0]", "expected_mapping"),
    ("rooms: {den: {plex_clients: [{uuid: 42}]}}", "rooms.den.plex_clients[0].uuid", "expected_string_or_null"),
    ("rooms: {den: {plex_clients: [{title: false}]}}", "rooms.den.plex_clients[0].title", "expected_string_or_null"),
    ("rooms: {den: {lights: [{id: private-value}]}}", "rooms.den.lights[0].brand", "expected_nonempty_string"),
    ("rooms: {den: {lights: [{brand: other, id: ' '}]}}", "rooms.den.lights[0].id", "expected_nonempty_string"),
    ("rooms: {den: {lights: [{brand: 1, id: private-value}]}}", "rooms.den.lights[0].brand", "expected_nonempty_string"),
    ("rooms: {den: {lights: [{brand: other, id: x, name: []}]}}", "rooms.den.lights[0].name", "expected_string_or_null"),
    ("rooms: {den: {lights: [{brand: other, id: x, model: 1}]}}", "rooms.den.lights[0].model", "expected_string_or_null"),
    ("rooms: {}\nrooms: {}", "rooms", "duplicate_key"),
    ("rooms: {den: {}, den: {}}", "rooms.den", "duplicate_key"),
    ("rooms: {den: {plex_clients: [{uuid: private-value, uuid: other}]}}", "rooms.den.plex_clients[0].uuid", "duplicate_key"),
    ("rooms: {den: {lights: [{brand: other, id: x, id: y}]}}", "rooms.den.lights[0].id", "duplicate_key"),
    ("rooms: {den: {private-value: 1, private-value: 2}}", "rooms.den.[key]", "duplicate_key"),
    ("rooms: &loop {den: *loop}", "rooms.den", "recursive_yaml"),
    ("rooms: {1: {}}", "rooms.[key]", "expected_string"),
    ("rooms: {}\n---\nrooms: {}", "$", "invalid_yaml"),
    ("? [a, b]\n: private-value", "$", "invalid_yaml"),
    ("rooms: \x00private-value", "$", "invalid_yaml"),
    ("rooms: {den: {plex_clients: [{uuid: !!int private-value}]}}", "$", "invalid_yaml"),
]


@pytest.mark.parametrize("source,path,code", INVALID_CANDIDATES)
def test_invalid_candidate_preserves_mapping(registry, rooms_file, source, path, code):
    before = registry._state
    rooms_file.write_text(source, encoding="utf-8")
    for operation in (registry.validate, registry.reload):
        with pytest.raises(RoomConfigError) as error:
            operation()
        assert error.value.detail == [{"path": path, "code": code}]
        assert "private-value" not in str(error.value.detail)
        assert registry._state is before
        assert registry.resolve_room(payload("media.play", uuid="uuid-living")) == "living_room"
        assert registry.resolve_room(payload("media.play", title=" Bedroom Apple TV ")) == "bedroom"


@pytest.mark.parametrize("field,first,second", [("uuid", "private-value", "private-value"), ("title", "Private-Value", " private-value ")])
def test_cross_room_conflicts_report_both_paths(registry, rooms_file, field, first, second):
    import yaml

    before = registry._state
    rooms_file.write_text(yaml.safe_dump({"rooms": {
        "a": {"plex_clients": [{field: first}]}, "b": {"plex_clients": [{field: second}]},
    }}), encoding="utf-8")
    with pytest.raises(RoomConfigError) as error:
        registry.reload()
    assert error.value.detail == [
        {"path": f"rooms.a.plex_clients[0].{field}", "code": "conflicting_client"},
        {"path": f"rooms.b.plex_clients[0].{field}", "code": "conflicting_client"},
    ]
    assert registry._state is before


@pytest.mark.parametrize("source", ["", "# empty", "{}", "extension: private-value", "rooms: {}"])
def test_valid_empty_config(rooms_file, source):
    rooms_file.write_text(source, encoding="utf-8")
    assert RoomRegistry(rooms_file).rooms == {}


def test_optional_fields_extensions_and_repetition(rooms_file):
    rooms_file.write_text('''rooms:
  den:
    extension: preserved
    plex_clients:
      - {uuid: fake-tv, title: " TV ", extra: preserved}
    lights:
      - {brand: unsupported, id: fake-light, name: null, model: null, extra: preserved}
''', encoding="utf-8")
    reg = RoomRegistry(rooms_file)
    assert reg.resolve_room(payload("media.play", title="tv")) == "den"
    assert reg.rooms["den"]["extension"] == "preserved"
    assert reg.lights_for("den")[0]["extra"] == "preserved"


def test_example_config():
    from pathlib import Path

    reg = RoomRegistry(Path(__file__).parents[1] / "config" / "rooms.yaml.example")
    assert set(reg.rooms) == {"living_room", "bedroom"}


def test_yaml_aliases_and_merges(rooms_file):
    rooms_file.write_text('''defaults: &defaults
  lights: [{brand: unsupported, id: fake-light}]
rooms:
  den:
    <<: *defaults
    plex_clients: [{title: TV}]
''', encoding="utf-8")
    reg = RoomRegistry(rooms_file)
    assert reg.resolve_room(payload("media.play", title="tv")) == "den"
    assert reg.lights_for("den")[0]["id"] == "fake-light"


def test_invalid_startup_is_sanitized(rooms_file, caplog):
    rooms_file.write_text("rooms: [private-value", encoding="utf-8")
    reg = RoomRegistry(rooms_file)
    assert reg._state == EMPTY_STATE
    assert "invalid_yaml" in caplog.text
    assert "private-value" not in caplog.text


@pytest.mark.parametrize("failure", [FileNotFoundError, PermissionError])
def test_unavailable_file_preserves_mapping(registry, monkeypatch, failure):
    from pathlib import Path

    before = registry._state
    def fail(*args, **kwargs):
        raise failure("private-value")
    monkeypatch.setattr(Path, "read_text", fail)
    for operation in (registry.validate, registry.reload):
        with pytest.raises(RoomConfigUnavailable) as error:
            operation()
        assert "private-value" not in str(error.value)
        assert registry._state is before


def test_invalid_encoding_preserves_mapping(registry, rooms_file):
    before = registry._state
    rooms_file.write_bytes(b"\xff")
    with pytest.raises(RoomConfigError) as error:
        registry.reload()
    assert error.value.detail == [{"path": "$", "code": "invalid_yaml"}]
    assert registry._state is before
