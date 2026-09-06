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


def _fake_literal(values: dict[str, str]):
    """Replaces align._dbus_call_literal, reading a method -> output table.

    It accepts **kw because the diagnostics pass an explicit short timeout:
    a fake with a narrower signature than the real function turns a passing
    suite into TypeErrors the moment the caller adds a keyword.
    """
    async def fake(path: str, method: str, *args: str, **kw):
        return values.get(method.rsplit(".", 1)[-1], "")
    return fake


def _fresh_log_line(text: str) -> str:
    """A log line stamped now, the way Ekos stamps its own."""
    from datetime import datetime
    return f"{datetime.now().strftime('%Y-%m-%dT%H:%M:%S')} {text}"


@pytest.fixture
def ekos(monkeypatch):
    """A healthy Ekos: complete optical train, camera present, module idle."""
    def configure(*, telescope="[Argument: ad {580, 100, 1}]",
                  camera_info="[Argument: ad {6248, 4176, 3.76, 3.76}]",
                  camera="CCD Simulator", status="0", log=""):
        monkeypatch.setattr(align, "_dbus_call_literal", _fake_literal({
            "telescopeInfo": telescope, "cameraInfo": camera_info,
        }))
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
    ekos(log=_fresh_log_line("Cannot capture") + "\n"
             + _fresh_log_line("WCS enabled"))
    status, detail = await align._capture_and_solve_refusal("false")
    assert status == 422
    assert "Cannot capture" in detail
    assert "WCS enabled" not in detail


async def test_an_old_log_line_is_not_passed_off_as_the_reason(ekos):
    # Ekos never clears this log: its newest line can be from a solve twenty
    # minutes ago. Quoting it would both mislead the user and make the honest
    # "no reason given" answer unreachable, since a running Ekos nearly
    # always has something in the log.
    ekos(log="2026-09-01T18:27:04 Solution coordinates: RA 05h 34m DE +22d 00m")
    status, detail = await align._capture_and_solve_refusal("false")
    assert status == 500
    assert "Solution coordinates" not in detail


async def test_a_camera_without_pixel_size_is_named(ekos):
    # Same stale-optical-train cause as the missing focal length, and Ekos
    # refuses it the same silent way.
    ekos(camera_info="[Argument: ad {0, 0, -1, -1}]")
    status, detail = await align._capture_and_solve_refusal("false")
    assert status == 422
    assert "pixel size" in detail


def test_align_state_table_matches_ekos_h():
    """Ekos::AlignState numbering, as declared in kstars/ekos/ekos.h.

    ALIGN_SUCCESSFUL sits at index 5 and shifts everything after it, so the
    plausible-looking order (syncing, slewing, suspended...) is wrong from
    there on. Pinned so the three copies this file used to carry cannot
    quietly disagree again.
    """
    t = align._ALIGN_STATES
    assert t[4] == "progress"
    assert t[5] == "successful"
    assert t[6] == "syncing"
    assert t[7] == "slewing"
    assert t[8] == "rotating"
    assert t[9] == "suspended"
    assert len(t) == 10
    # The busy subset must be a subset, and must not claim idle/complete.
    assert set(align._ALIGN_BUSY_STATES) <= set(t)
    assert not set(align._ALIGN_BUSY_STATES) & {0, 1, 2, 3}


async def test_a_suspended_module_is_reported_as_suspended(ekos):
    # 9 = ALIGN_SUSPENDED, the one state that genuinely blocks the module.
    # The old table never reached it at all.
    ekos(status="9")
    status, detail = await align._capture_and_solve_refusal("false")
    assert status == 422
    assert "already suspended" in detail


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
