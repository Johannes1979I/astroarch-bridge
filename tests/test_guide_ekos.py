"""Reading the state of the Ekos INTERNAL guider.

Both cases covered here are silent ones: nothing raises, nothing is
logged, and the result is simply missing or wrong data. That is why they
deserve a test rather than a manual check.
"""
from astroarch_bridge.routes import guide


def test_parse_float_list_reads_qdbus_literal_output():
    # The real format qdbus6 --literal prints for an 'ad' property.
    raw = "[Variant: [Argument: ad {0.42, 0.31}]]"
    assert guide._parse_float_list(raw) == [0.42, 0.31]


def test_parse_float_list_ignores_the_qdbus_error_message():
    # Without --literal, qdbus6 prints this and still exits 0: the caller
    # must end up with an empty list, not with invented numbers.
    raw = ("qdbus: I don't know how to display an argument of type 'ad', "
           "run with --literal.")
    assert guide._parse_float_list(raw) == []


def test_parse_float_list_survives_plain_numbers():
    assert guide._parse_float_list("0.5 0.25") == [0.5, 0.25]


def _write_kstarsrc(home, body: str) -> None:
    cfg = home / ".config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "kstarsrc").write_text(body, encoding="utf-8")


def test_guider_type_is_read_when_present(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_kstarsrc(tmp_path, "[Guide]\nGuiderType=1\nDitherEnabled=true\n")
    assert guide._read_guider_type() == 1


def test_missing_guider_type_means_the_kstars_default(tmp_path, monkeypatch):
    # KDE writes the key only if the user changes the setting: its absence
    # is the kstars.kcfg default, that is, the internal guider.
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_kstarsrc(tmp_path, "[Guide]\nDitherEnabled=true\nDitherPixels=3\n")
    assert guide._read_guider_type() == 0


def test_no_kstarsrc_at_all_stays_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert guide._read_guider_type() is None


async def test_ekos_status_asks_for_literal_output_and_parses_rms(monkeypatch):
    calls: list[tuple[str, ...]] = []

    async def fake_qdbus(*args: str, timeout: float = 10.0):
        calls.append(args)
        method = args[-1]
        if method.endswith(".status"):
            return 0, "10"
        if method.endswith(".axisSigma"):
            return 0, "[Variant: [Argument: ad {0.8, 0.6}]]"
        if method.endswith(".axisDelta"):
            return 0, "[Variant: [Argument: ad {0.1, -0.2}]]"
        return 1, ""

    monkeypatch.setattr(guide, "_qdbus_call", fake_qdbus)
    out = await guide.guide_ekos_status()

    assert out["state"] == "GUIDING"
    assert (out["rms_ra"], out["rms_dec"]) == (0.8, 0.6)
    assert out["rms_total"] == 1.0
    assert (out["delta_ra"], out["delta_dec"]) == (0.1, -0.2)

    # The two array properties must be asked for with --literal; status,
    # which is an int, must not: with it, it would come back as
    # '[Variant(int): 10]'.
    for args in calls:
        if args[-1].endswith((".axisSigma", ".axisDelta")):
            assert "--literal" in args
        if args[-1].endswith(".status"):
            assert "--literal" not in args
