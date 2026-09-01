"""The error message when Ekos refuses capture and solve.

All Ekos returns is `false`, and for several of its preconditions it
leaves no trace in the log: these tests pin down that the bridge, before
giving up, asks Ekos enough to tell the user what to fix.
"""
import pytest

from astroarch_bridge.routes import align, capture_ekos


def _fake_dbus(values: dict[str, str]):
    """Replaces capture_ekos._dbus_call, reading a method -> output table."""
    async def fake(service: str, path: str, method: str, *args: str, **kw):
        key = method.rsplit(".", 1)[-1]
        if key in values:
            return 0, values[key]
        return 1, ""
    return fake


def _fake_literal(value: str):
    async def fake(path: str, method: str, *args: str):
        return value
    return fake


@pytest.fixture
def ekos(monkeypatch):
    """A healthy Ekos: complete optical train, camera present, module idle."""
    def configure(*, telescope="[Argument: ad {580, 100, 1}]",
                  camera="CCD Simulator", status="0", log=""):
        monkeypatch.setattr(align, "_dbus_call_literal", _fake_literal(telescope))
        monkeypatch.setattr(capture_ekos, "_dbus_call", _fake_dbus({
            "camera": camera, "status": status, "logText": log,
        }))
    return configure


async def test_missing_optical_train_is_named(ekos):
    # The real case: the active train points at optics no longer in the
    # database, Ekos reports -1 and refuses without logging anything.
    ekos(telescope="[Argument: ad {-1, -1, 1}]")
    status, detail = await align._capture_and_solve_refusal("false")
    assert status == 422
    assert "optical train" in detail
    assert "focal=-1" in detail and "aperture=-1" in detail


async def test_missing_camera_is_named(ekos):
    ekos(camera="")
    status, detail = await align._capture_and_solve_refusal("false")
    assert status == 422
    assert "camera" in detail


async def test_busy_module_is_named(ekos):
    # 4 = in progress. Verified on KStars 3.8.3 by sampling `status` during
    # a real capture-and-solve: 4 throughout, 1 when the solution lands.
    ekos(status="4")
    status, detail = await align._capture_and_solve_refusal("false")
    assert status == 422
    assert "already capturing or solving" in detail


async def test_a_finished_alignment_is_not_reported_as_busy(ekos):
    # 1 = complete, not "busy": it must not end up in the message.
    ekos(status="1")
    status, detail = await align._capture_and_solve_refusal("false")
    assert status == 500
    assert "already" not in detail


async def test_the_last_ekos_log_line_is_quoted(ekos):
    # Ekos returns the log newest first.
    ekos(log="2026-09-01T18:27 Cannot capture\n2026-09-01T17:34 WCS enabled")
    status, detail = await align._capture_and_solve_refusal("false")
    assert status == 422
    assert "Cannot capture" in detail
    assert "WCS enabled" not in detail


async def test_ekos_not_started_is_told_apart_from_a_refusal(ekos):
    ekos()
    raw = ("Error: org.freedesktop.DBus.Error.UnknownObject\n"
           "No such object path '/KStars/Ekos/Align'")
    status, detail = await align._capture_and_solve_refusal(raw)
    assert status == 503
    assert "Ekos is not started" in detail


async def test_kstars_not_running_is_told_apart_too(ekos):
    ekos()
    status, detail = await align._capture_and_solve_refusal(
        "Service 'org.kde.kstars' does not exist.")
    assert status == 503
    assert "KStars is not running" in detail


async def test_a_healthy_ekos_still_gets_an_honest_message(ekos):
    # No clue at all: better to say so than to invent a diagnosis.
    ekos()
    status, detail = await align._capture_and_solve_refusal("false")
    assert status == 500
    assert "without giving a reason" in detail
    assert "false" in detail


async def test_several_causes_are_all_reported(ekos):
    ekos(telescope="[Argument: ad {-1, -1, 1}]", camera="", status="4")
    status, detail = await align._capture_and_solve_refusal("false")
    assert status == 422
    assert "optical train" in detail
    assert "camera" in detail
    assert "capturing or solving" in detail
