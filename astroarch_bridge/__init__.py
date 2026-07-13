"""astroarch-bridge - Backend daemon for Astroarch Interface Android app.

Author: Zarletti-Osservatorio Jupiter
"""

# v0.3.7: la versione esposta da /api/system/info viene letta in modo
# robusto. Ordine di tentativi:
#   1) importlib.metadata.version("astroarch-bridge") — funziona se il
#      pacchetto è installato (pip install / pacman / wheel install)
#   2) pyproject.toml letto dal filesystem — funziona quando giriamo da
#      sorgente con `python -m astroarch_bridge` (cwd su repo clonato)
#   3) fallback hardcoded — ultimo resort
#
# In passato __version__ era hardcoded a "0.1.0" e dimenticato ad ogni
# release, esponendo erroneamente quel numero su /api/system/info anche
# quando il codice in esecuzione era ben più recente.


def _resolve_version() -> str:
    # Priorità 1: pyproject.toml ACCANTO al modulo importato (running from source).
    # Più affidabile in development perché `astroarch_bridge.egg-info` legacy
    # nei cloni vecchi può rispondere con versioni stale via importlib.metadata.
    try:
        from pathlib import Path
        proj = Path(__file__).resolve().parent.parent / "pyproject.toml"
        if proj.exists():
            in_project = False
            for line in proj.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if s.startswith("[") and s.endswith("]"):
                    in_project = (s == "[project]")
                    continue
                if in_project and s.startswith("version") and "=" in s:
                    v = s.split("=", 1)[1].strip().strip('"').strip("'")
                    if v:
                        return v
    except Exception:
        pass
    # Priorità 2: importlib.metadata (funziona per install pacman/pip wheel).
    try:
        from importlib.metadata import version, PackageNotFoundError
        try:
            return version("astroarch-bridge")
        except PackageNotFoundError:
            pass
    except Exception:
        pass
    return "0.3.16"  # hardcoded fallback, KEEP IN SYNC con pyproject.toml


__version__ = _resolve_version()
__author__ = "Zarletti-Osservatorio Jupiter"
