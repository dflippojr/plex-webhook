"""The light-controller contract: each backend has the shape the dispatcher calls, and the fake honors it."""
import inspect

import pytest

from app import lights

CONTROLLERS = [lights.GoveeController, lights.TuyaController, lights.HassController, lights.NullController]


@pytest.mark.parametrize("cls", CONTROLLERS, ids=lambda cls: cls.__name__)
def test_controller_matches_the_light_controller_protocol(cls):
    assert isinstance(cls(), lights.LightController)
    for name in ("apply", "read_state"):
        assert inspect.signature(getattr(cls, name)) == inspect.signature(getattr(lights.LightController, name))


def test_light_calls_fake_returns_one_light_result_per_light(light_calls):
    room = [{"brand": "govee", "id": "AA:BB"}, {"brand": "tuya", "id": "plug1"}]
    results = lights.apply_action("dim", room, 30)
    assert all(isinstance(result, lights.LightResult) for result in results)
    assert [result.target_id for result in results] == ["AA:BB", "plug1"]
    assert light_calls == [("dim", ["AA:BB", "plug1"])]
