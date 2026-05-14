# Astroarch Bridge — Python backend

> **Python daemon that bridges the Android app to KStars/Ekos, INDI and PHD2.**
> Runs on the Raspberry Pi 5 inside AstroArch as a systemd user service.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=flat-square)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11%2B-blue?style=flat-square&logo=python)](#)
[![Service](https://img.shields.io/badge/systemd-user--service-purple?style=flat-square)](#)

**Author**: Zarletti-Osservatorio Jupiter
**Companion repo (Android app)**: [Johannes1979I/astroarch-interface-app](https://github.com/Johannes1979I/astroarch-interface-app)

---

## What this repo contains

This is the **backend bridge** of Astroarch Interface — a FastAPI +
WebSocket Python daemon that connects to:

- **Ekos** via DBus (`qdbus6`, `dbus-monitor` for signals)
- **INDI server** via raw TCP XML protocol (port 7624) as a *secondary
  client* that does not disturb Ekos
- **PHD2** via its JSON-RPC server (port 4400)
- **KStars userdb** read-only (`~/.local/share/kstars/userdb.sqlite`)
  to inherit the user's optical-train settings

…and exposes the result over HTTPS REST + two WebSockets (one for
state, one for camera frames) for the Android app.

You need **both** the app and this bridge to use the system:

| Repo | What it is | Where it runs |
|---|---|---|
| **astroarch-bridge** *(this one)* | Python daemon | the Raspberry Pi 5 (AstroArch) |
| [**astroarch-interface-app**](https://github.com/Johannes1979I/astroarch-interface-app) | Flutter / Android app | the phone |

---

## Architecture

```
       Android app                 Tailscale (WireGuard)         Raspberry Pi 5 (AstroArch)
   ┌─────────────────┐                                       ┌──────────────────────────────┐
   │                 │ ─────HTTPS / WSS───────────────────► │  astroarch-bridge :8765      │
   │  Astroarch      │                                       │   ├─ REST   /api/*           │
   │  Interface      │ ◄────── live snapshots ──────────── │   ├─ WS     /ws/state        │
   │  (Flutter)      │                                       │   └─ WS     /ws/frames       │
   │                 │                                       │                              │
   │  14 screens     │                                       │   ┌─ INDI client TCP :7624  │
   │  Provider       │                                       │   │  + enableBLOB (parallel │
   │  WebSocket      │                                       │   │    to Ekos, no impact)  │
   │                 │                                       │   ├─ PHD2 client TCP :4400  │
   │                 │                                       │   ├─ Ekos via DBus (qdbus6)│
   │                 │                                       │   ├─ dbus-monitor for      │
   │                 │                                       │   │  Ekos signals (HFR…)   │
   │                 │                                       │   └─ KStars userdb (RO)    │
   └─────────────────┘                                       │                              │
                                                             │  KStars/Ekos (untouched)     │
                                                             │  PHD2 (untouched)            │
                                                             └──────────────────────────────┘
```

**Key design principle**: the bridge is a *non-invasive secondary
client*. It does NOT modify Ekos's UPLOAD_MODE, target coordinates,
save folders, placeholder formats, or any other user-configured
field. It reads them from the canonical sources (DBus, INDI, KStars
userdb) and forwards to the app.

---

## Quick start (AstroArch users)

> Tested on AstroArch (ArchLinux ARM) + Raspberry Pi 5. Setup time: **~5 minutes**.

Install it directly via pacman with `sudo pacman -S astroarch-bridge`

If you want to test it locally and run it/modify be sure to have uv installed, otherwise install it via `sudo pacman -S python-uv` then:

### 1) Clone + install

```bash
ssh astronaut@RPI_IP
git clone https://github.com/Johannes1979I/astroarch-bridge
cd astroarch-bridge
make run-app (or uv run astroarch_bridge)
```

The script:

- copies the bridge to `/home/astronaut/astroarch-bridge/`
- creates and enables the systemd user service
  `astroarch-bridge.service` (auto-starts at boot)
- generates a **random token** saved in `~/.config/astroarch-bridge/token`
- prints the **Tailscale URL**, **LAN URL** and **token** to put in
  the app

### 2) Install Tailscale (if not already)

```bash
sudo pacman -S tailscale
sudo systemctl enable --now tailscaled
sudo tailscale up
```

### 3) Install the Android app

Grab the latest APK from the companion repo:

→ [**astroarch-interface-app/releases**](https://github.com/Johannes1979I/astroarch-interface-app/releases/latest)

### 4) Pair

Open the app → **SCAN QR** (the bridge ships a small desktop widget
that shows the QR on the AstroArch desktop), or **Enter manually**:

- **Host**: Pi's Tailscale IP (`tailscale ip -4`)
- **Port**: `8765`
- **Token**: printed by `install.sh`

---

## Service management

```bash
# Status
systemctl --user status astroarch-bridge

# Live log
journalctl --user -u astroarch-bridge -f

# Restart after upgrade
systemctl --user restart astroarch-bridge

# Upgrade
cd ~/astroarch-bridge
git pull
systemctl --user restart astroarch-bridge
```

The bridge auto-restarts on crash and on Pi reboot.

---

## What the bridge inherits from your Ekos profile

The bridge is read-only on these settings — it preserves what you
have configured in Ekos:

- ✅ **FITS save folder** (Capture → Cartella) — from
  `opticaltrainsettings.fileDirectoryT`
- ✅ **Placeholder format** (Capture → Formato) — from
  `placeholderFormatT`
- ✅ **Optical train ID** — from `~/.config/kstarsrc → CaptureTrainID`
- ✅ **Camera, focuser, filter wheel** — from `Ekos.Focus.camera/.focuser/...`
- ✅ **Target coordinates** — from `Ekos.Align.getTargetCoords` (and
  re-pushed from the app's active target before every solve)
- ✅ **PHD2 server endpoint** at `127.0.0.1:4400`
- ✅ **INDI server endpoint** at `127.0.0.1:7624`

If Ekos already works on your desktop, the bridge works without any
extra configuration.

---

## API surface

REST endpoints (under `/api/`):

```
system/    snapshot, info, connections, devices, camera_roles,
           simbad, ekos_state, ekos_start, ekos_stop, ekos_toggle,
           qr (pairing QR with Tailscale IP),
           gui_apps_state, launch_kstars, launch_phd2  (v0.2.30+)
mount/     status, goto, park, unpark, abort, track, slew, slew_rate
camera/    status, expose, abort, cooler, gain, offset, binning, ...
focuser/   status, abs, rel, abort, autofocus (iterative bridge),
           ekos_state, ekos_start, ekos_abort, ekos_curve (Ekos native)
filter_wheel/  status, select
guide/     status, start, stop, dither, loop, clear_calibration,
           pause, find_star, calibrate, profile, star_image
align/     status, solve, ekos_full_status, ekos_capture_and_solve,
           ekos_align_set, ekos_align_abort, polar_align/run
capture/   ekos_alive, ekos_run, ekos_status, ekos_abort,
           ekos_clear, ekos_user_settings, preview_esq
observation/  run, status, abort   (full pre-flight orchestrator)
files/     recent, preview, delete, disk_usage
indi/      devices/{dev}/properties, refresh, connect, disconnect
observatory/  status, dome/shutter, dust_cap, flat_panel
scheduler/  weather_safe, sky_state, jobs
setup/     profiles, active_drivers
```

WebSockets:

```
/ws/state    JSON snapshots + incremental updates (INDI props, PHD2 live…)
/ws/frames   binary JPEG frames + meta (BLOB intercept from cameras)
```

Auth: every endpoint (except `/healthz`) requires `Authorization:
Bearer <token>`.

---

## Tech stack

| | |
|---|---|
| HTTP / WS | FastAPI + uvicorn |
| Async runtime | asyncio |
| Image processing | astropy (FITS, SIMBAD, AltAz, WCS), Pillow, numpy |
| INDI client | custom incremental XML protocol parser |
| Ekos integration | `qdbus6` subprocess calls + `dbus-monitor` for signals |
| PHD2 integration | custom JSON-RPC TCP client |
| Plate solving | `solve-field` (astrometry.net) fallback + `Ekos.Align.captureAndSolve` |
| QR generation | `qrcode[pil]` |
| Service | systemd user unit, auto-restart, journal |
| Auth | bearer token (auto-generated at first start) |

No new dependencies beyond what AstroArch already provides + the
Python packages listed in `requirements.txt`.

---

## Project structure

```
astroarch_bridge/
├── __init__.py
├── __main__.py          — entrypoint (`python -m astroarch_bridge`)
├── app.py               — FastAPI app + lifecycle + WS hubs
├── auth.py              — bearer-token middleware
├── config.py            — pydantic-settings (port, token, paths)
├── deps.py              — DI helpers (Bridge container)
├── state.py             — StateManager (INDI mirror, PHD2 live, frames)
├── indi/                — INDI XML parser + client
├── phd2/                — PHD2 JSON-RPC client
├── images/processor.py  — FITS auto-stretch (PixInsight STF), star/HFR
├── routes/              — REST endpoints (one module per area)
├── ws/                  — WebSocket endpoints + hubs
└── …
deploy/
├── install.sh           — one-command installer
├── astroarch-bridge.service  — systemd user unit
└── PKGBUILD             — optional Arch package recipe
desktop_dashboard/       — small Tk widget for the AstroArch desktop
                          that shows the pairing QR with Tailscale IP
```

---

## Documentation

- 📄 [**User Manual PDF**](AstroarchInterface_Manual.pdf) — full printable guide (covers both app and bridge)
- 🔧 [**DEPLOY.md**](DEPLOY.md) — step-by-step deployment guide

---

## License

[MIT](LICENSE) — feel free to use, modify, distribute. Please keep
attribution to Zarletti-Osservatorio Jupiter.

---

## Credits

- **AstroArch** — [github.com/devDucks/astroarch](https://github.com/devDucks/astroarch)
- **KStars / Ekos** — [edu.kde.org/kstars](https://edu.kde.org/kstars/)
- **PHD2** — [openphdguiding.org](https://openphdguiding.org/)
- **astrometry.net** — [astrometry.net](https://astrometry.net/)

🌙 **Clear skies!** — Zarletti-Osservatorio Jupiter
