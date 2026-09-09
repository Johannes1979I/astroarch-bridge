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
            # 12 = GUIDING in Ekos::GuideState (ekos.h). Not 10, which is
            # CALIBRATION_ERROR: the wrong table used to make the app show
            # DITHERING while the mount was quietly guiding.
            return 0, "12"
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


def test_guide_state_table_matches_ekos_h():
    """The GuideState numbering, as declared in KStars ekos.h.

    Three states (DARK, SUBFRAME, REACQUIRE) were missing from an earlier
    version of this table, shifting everything from index 6 onwards: the app
    reported DITHERING for a mount that was simply guiding. Pinned here so
    the shift cannot come back unnoticed.
    """
    t = guide._EKOS_GUIDE_STATES
    assert t[0] == "IDLE"
    assert t[6] == "DARK"
    assert t[7] == "SUBFRAME"
    assert t[9] == "CALIBRATING"
    assert t[10] == "CALIBRATION_ERROR"
    assert t[12] == "GUIDING"
    assert t[14] == "REACQUIRE"
    assert t[15] == "DITHERING"
    assert t[16] == "MANUAL_DITHERING"
    assert t[19] == "DITHERING_SETTLE"
    assert len(t) == 20


# --- quale guider usa Ekos: la fonte di verita' e' il PROFILO ----------------
#
# Il bug: si leggeva solo `kstarsrc [Guide] GuiderType`. Sull'osservatorio vero
# quella chiave NON esiste (verificato), e la scelta fra guider interno e PHD2
# si fa nel profilo Ekos, nella tabella `profile` di userdb.sqlite. Risultato:
# l'app diceva "guida interna" anche dopo aver selezionato PHD2 nel setup.

def _make_profile_db(home, profiles):
    """userdb.sqlite con la tabella profile, come la crea KStars."""
    import sqlite3
    d = home / ".local/share/kstars"
    d.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(d / "userdb.sqlite")
    con.execute("CREATE TABLE profile (id INTEGER PRIMARY KEY, name TEXT, "
                "guidertype INTEGER, guiderhost TEXT, guiderport INTEGER)")
    for i, (name, gt) in enumerate(profiles, start=1):
        con.execute("INSERT INTO profile (id, name, guidertype, guiderhost, guiderport)"
                    " VALUES (?,?,?,?,?)", (i, name, gt, "localhost", 4400))
    con.commit()
    con.close()


def test_guider_comes_from_the_active_profile(tmp_path, monkeypatch):
    # Ricostruisce esattamente l'osservatorio: kstarsrc senza GuiderType,
    # profilo attivo indicato, e il profilo che dice PHD2.
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_kstarsrc(tmp_path, "[Ekos]\nprofile=Askar\n\n[Guide]\nDitherEnabled=true\n")
    _make_profile_db(tmp_path, [("Simulators", 0), ("Askar", 1)])
    assert guide._active_profile_name() == "Askar"
    assert guide._read_guider_type() == 1


def test_switching_the_profile_switches_the_answer(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_kstarsrc(tmp_path, "[Ekos]\nprofile=Simulators\n")
    _make_profile_db(tmp_path, [("Simulators", 0), ("Askar", 1)])
    assert guide._read_guider_type() == 0


def test_the_profile_wins_over_kstarsrc(tmp_path, monkeypatch):
    """Se i due dicono cose diverse comanda il profilo: e' quello che Ekos usa."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_kstarsrc(tmp_path, "[Ekos]\nprofile=Askar\n\n[Guide]\nGuiderType=0\n")
    _make_profile_db(tmp_path, [("Askar", 1)])
    assert guide._read_guider_type() == 1


def test_without_a_database_it_falls_back_to_kstarsrc(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_kstarsrc(tmp_path, "[Guide]\nGuiderType=2\n")
    assert guide._read_guider_type() == 2


def test_a_single_profile_answers_even_without_an_active_one(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_kstarsrc(tmp_path, "[Guide]\nDitherEnabled=true\n")
    _make_profile_db(tmp_path, [("Askar", 1)])
    assert guide._read_guider_type() == 1


def test_the_database_is_never_written(tmp_path, monkeypatch):
    """KStars puo' averlo aperto nello stesso momento: si legge e basta."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_kstarsrc(tmp_path, "[Ekos]\nprofile=Askar\n")
    _make_profile_db(tmp_path, [("Askar", 1)])
    db = tmp_path / ".local/share/kstars/userdb.sqlite"
    before = db.read_bytes()
    guide._read_guider_type()
    assert db.read_bytes() == before
