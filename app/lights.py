import logging

logger = logging.getLogger("plex-webhook")


class StubController:
    """Placeholder backend: logs the intended action instead of calling a real device.

    Swap this out per-brand (GoveeController using LAN control, TuyaController
    using tinytuya + a local key) once device credentials are gathered - the
    dispatcher only depends on the apply() interface below.
    """

    def apply(self, action: str, light: dict):
        logger.info(
            "STUB %s brand=%s id=%s name=%s (no real device call - controller not implemented yet)",
            action,
            light.get("brand"),
            light.get("id"),
            light.get("name"),
        )


_controllers = {
    "govee": StubController(),
    "tuya": StubController(),
}


def get_controller(brand: str):
    return _controllers.get(brand, StubController())


def apply_action(action: str, lights: list):
    for light in lights:
        controller = get_controller(light.get("brand"))
        controller.apply(action, light)
