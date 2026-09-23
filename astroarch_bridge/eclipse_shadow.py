"""Geometria dell'ombra terrestre per le eclissi di LUNA (contatti + visibilità).

A differenza del Sole (separazione topocentrica Sole-Luna, in eclipse.py), qui i
contatti sono UNIVERSALI: la Luna entra nel cono d'ombra della Terra
(penombra/umbra), evento geocentrico uguale per tutti. Dal luogo dell'osservatore
conta solo se la Luna è sopra l'orizzonte.

Formule standard (Meeus, Astronomical Algorithms cap. 54): raggi angolari
dell'ombra alla distanza della Luna, con allargamento Danjon (+2%) per
l'atmosfera terrestre.
"""
from __future__ import annotations

R_EARTH_KM = 6378.14
R_SUN_KM = 696000.0
R_MOON_KM = 1737.4


def _hhmmss(t) -> str:
    return t.utc.iso[11:19] + " UT"


def compute_lunar_contacts(date: str, lat: float, lon: float) -> dict:
    """Contatti P1/U1/U2/max/U3/U4/P4 (UT) + visibilità della Luna a (lat, lon).
    `date` = YYYY-MM-DD (UT del massimo). Best-effort: verificare sul campo."""
    import numpy as np
    from astropy.time import Time
    from astropy.coordinates import EarthLocation, AltAz, get_body
    import astropy.units as u

    loc = EarthLocation(lat=lat * u.deg, lon=lon * u.deg, height=0 * u.m)

    def shadow(times):
        # Geocentrico: Sole e Luna visti dal centro della Terra.
        sun = get_body("sun", times)
        moon = get_body("moon", times)
        sep = sun.separation(moon).deg          # elongazione geocentrica
        gamma = 180.0 - sep                       # distanza Luna ↔ asse d'ombra
        d_moon = moon.distance.to(u.km).value
        d_sun = sun.distance.to(u.km).value
        pi_m = np.degrees(np.arcsin(R_EARTH_KM / d_moon))  # parallasse Luna
        pi_s = np.degrees(np.arcsin(R_EARTH_KM / d_sun))   # parallasse Sole
        s_s = np.degrees(np.arcsin(R_SUN_KM / d_sun))      # semidiametro Sole
        s_m = np.degrees(np.arcsin(R_MOON_KM / d_moon))    # semidiametro Luna
        rho_u = 1.02 * (pi_m + pi_s - s_s)        # raggio umbra
        rho_p = 1.02 * (pi_m + pi_s + s_s)        # raggio penombra
        return gamma, rho_u, rho_p, s_m

    # Finestra ampia attorno al giorno UT (copre eventi a cavallo di mezzanotte).
    t0 = Time(f"{date}T00:00:00", scale="utc") - 6 * u.hour
    nc = int(36 * 60 / 3)  # 36 h a 3 min
    tc = t0 + np.arange(nc) * (3 * u.min)
    g, ru, rp, sm = shadow(tc)
    if not bool((g <= (rp + sm)).any()):
        return {"visible": False,
                "note": "Nessuna eclissi di Luna trovata per questa data."}
    imax = int(np.argmin(g))

    # Scansione fine ±4 h attorno al massimo, a 6 s.
    fn = int(8 * 3600 / 6) + 1
    tf = tc[imax] + np.linspace(-4 * 3600, 4 * 3600, fn) * u.s
    g, ru, rp, sm = shadow(tf)
    jmax = int(np.argmin(g))

    def edges(mask):
        if not bool(mask.any()):
            return None, None
        a = int(np.argmax(mask))
        b = int(len(mask) - 1 - np.argmax(mask[::-1]))
        return a, b

    p1, p4 = edges(g <= (rp + sm))   # penombra (P1/P4)
    u1, u4 = edges(g <= (ru + sm))   # parziale umbrale (U1/U4)
    u2, u3 = edges(g <= (ru - sm))   # totale (U2/U3)

    # Visibilità: altezza della Luna sull'orizzonte del luogo lungo la finestra.
    moon_altaz = get_body("moon", tf).transform_to(AltAz(obstime=tf, location=loc))
    alt = moon_altaz.alt.deg
    az = moon_altaz.az.deg

    gmin = float(g[jmax]); smj = float(sm[jmax])
    ruj = float(ru[jmax]); rpj = float(rp[jmax])
    umag = (ruj + smj - gmin) / (2 * smj)
    pmag = (rpj + smj - gmin) / (2 * smj)
    etype = "total" if u2 is not None else ("partial" if u1 is not None else "penumbral")

    def at(i):
        return _hhmmss(tf[i]) if i is not None else None

    def up(i):
        return bool(alt[i] > 0) if i is not None else None

    tot_sec = int(round((tf[u3] - tf[u2]).to(u.s).value)) if u2 is not None else None
    par_sec = int(round((tf[u4] - tf[u1]).to(u.s).value)) if u1 is not None else None

    return {
        "visible": bool(alt[jmax] > 0),   # Luna sopra l'orizzonte al massimo
        "type": etype,
        "umbral_magnitude": round(float(umag), 3),
        "penumbral_magnitude": round(float(pmag), 3),
        "p1": at(p1), "u1": at(u1), "u2": at(u2), "max": at(jmax),
        "u3": at(u3), "u4": at(u4), "p4": at(p4),
        "totality_sec": tot_sec, "partial_sec": par_sec,
        "moon": {"alt": round(float(alt[jmax]), 2), "az": round(float(az[jmax]), 2)},
        "moon_up": {"p1": up(p1), "u1": up(u1), "max": up(jmax),
                    "u4": up(u4), "p4": up(p4)},
        "note": "Geometria ombra terrestre (astropy, Danjon +2%). Verifica sul campo.",
    }


def moon_radec_now() -> "tuple[float, float]":
    """RA (ore)/Dec (gradi) apparenti della Luna ADESSO (equinozio di data, JNow)."""
    from astropy.time import Time
    from astropy.coordinates import get_body, FK5
    t = Time.now()
    m = get_body("moon", t).transform_to(FK5(equinox=t))
    return float(m.ra.hour), float(m.dec.deg)
