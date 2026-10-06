from conftest import payload


def test_resolves_by_uuid(registry):
    assert registry.resolve_room(payload("media.play", title="whatever", uuid="uuid-living")) == "living_room"


def test_resolves_by_title_case_and_whitespace_insensitive(registry):
    assert registry.resolve_room(payload("media.play", title="BEDROOM apple tv")) == "bedroom"
    assert registry.resolve_room(payload("media.play", title="  living room tv ")) == "living_room"


def test_uuid_wins_over_title(registry):
    assert registry.resolve_room(payload("media.play", title="Living Room TV", uuid="uuid-bed-phone")) == "bedroom"


def test_unknown_or_missing_player(registry):
    assert registry.resolve_room(payload("media.play", title="Kitchen")) is None
    assert registry.resolve_room({}) is None
    assert registry.resolve_room(None) is None


def test_lights_for(registry):
    assert registry.lights_for("bedroom")[0]["id"] == "plug1"
    assert registry.lights_for("nope") == []


def test_missing_config_disables_dispatcher(registry, tmp_path):
    registry.config_path = tmp_path / "absent.yaml"
    registry.reload()
    assert registry.rooms == {}
    assert registry.resolve_room(payload("media.play")) is None
