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
notify/    push, recent   (offline alerts from external tools)
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

## Offline notifications

Under a dark sky there is usually no connectivity, so alerting services
that relay through the internet are of no use. Any program running
alongside the bridge can instead send it a plain UDP datagram, which the
bridge republishes on `/ws/state` as a `notification` event — every
client already connected (Android app, browser, tablet) shows it, with
no extra infrastructure and no internet.

```bash
# plain text
echo -n "KStars has stopped." | nc -u -w0 127.0.0.1 5005

# or JSON, for a level and a title
echo -n '{"title":"Weather","message":"clouds incoming","level":"warning"}' \
  | nc -u -w0 127.0.0.1 5005
```

The listener binds to **loopback only** by default, because the normal
sender runs on the same machine as KStars. To receive from another host
on the observing LAN, set `ASTROARCH_NOTIFY_UDP_HOST=0.0.0.0`; bear in
mind that anyone on that network can then post a notification. Set
`ASTROARCH_NOTIFY_UDP_ENABLED=false` to close the socket entirely.

| Env var | Default |
|---|---|
| `ASTROARCH_NOTIFY_UDP_ENABLED` | `true` |
| `ASTROARCH_NOTIFY_UDP_HOST` | `127.0.0.1` |
| `ASTROARCH_NOTIFY_UDP_PORT` | `5005` |

Senders that already speak HTTP can use `POST /api/notify` instead,
which takes the same fields and requires the bearer token. The last 50
notifications are kept in memory: they ride along in the WebSocket
snapshot, so a tablet that was asleep still sees what it missed on
reconnect, and `GET /api/notify/recent` returns them on demand.
## Serving a web UI (optional)

The bridge can serve a pre-built web interface from the same origin as
its own API. Point `ASTROARCH_WEB_DIR` at a folder containing an
`index.html` and it is mounted at `/`; if the folder is absent nothing
is mounted and the bridge behaves exactly as before.

```bash
ASTROARCH_WEB_DIR=/usr/share/astroarch-bridge/web astroarch-bridge
# then, from any device on the same network:
#   http://astroarch.local:8765/
```

Same-origin is the point, not a convenience. A page served over HTTPS
from anywhere else cannot call `http://astroarch.local:8765` at all —
browsers block mixed content with no workaround available to the page.
Serving the UI from the bridge sidesteps both that and CORS, and needs
no certificate, which matters in the field where there is no internet
to obtain or renew one.

The mount is registered last, so `/api`, `/ws` and `/healthz` keep
precedence over it. Unknown paths fall back to `index.html` so that
client-side routes survive a reload, except under those reserved
prefixes and for paths that look like files — a missing `main.dart.js`
stays an honest 404 rather than becoming HTML that the browser would
report as a baffling syntax error.

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

## Team & contributors

This project is built and improved by a small community of astrophotographers.

| | Role | Contributions |
|---|---|---|
| **Gianpaolo Zarletti** (Zarletti-Osservatorio Jupiter) | Creator, lead developer & maintainer | Concept, architecture, app + bridge development |
| **Mattia Procopio** ([MattBlack85](https://github.com/MattBlack85)) | Co-maintainer · packaging & distribution | `uv` migration, Makefile, pacman packaging, PKGBUILD & CI |
| **Stéphane Carlin** ([sc74](https://github.com/sc74)) | Contributor | Multi-user PKGBUILD, broadcast-stream fix, desktop dashboard i18n (FR/ES/DE) |
| **Benoit "Tucniak"** ([BenoitBotton](https://github.com/BenoitBotton)) | Contributor | French app localization, QA & testing on multiple hardware |

Want to help? PRs and ideas are very welcome.

## Credits (third-party)

- **AstroArch** — [github.com/devDucks/astroarch](https://github.com/devDucks/astroarch)
- **KStars / Ekos** — [edu.kde.org/kstars](https://edu.kde.org/kstars/)
- **PHD2** — [openphdguiding.org](https://openphdguiding.org/)
- **astrometry.net** — [astrometry.net](https://astrometry.net/)

🌙 **Clear skies!** — Zarletti-Osservatorio Jupiter & contributors
