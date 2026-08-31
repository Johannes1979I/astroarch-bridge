"""UDP listener for notifications produced by other programs on the same
machine (or on the same LAN), without going through an internet relay.

Why: in the field, under a dark sky, there is no connectivity. Tools that
watch the session — astro_monitor, for instance, which reports that KStars
has died — cannot use Telegram or push services. They can send a UDP
datagram, though: the bridge picks it up and republishes it on /ws/state,
so every already-connected client — the Android app, the web interface on
an iPad, a laptop browser — shows the notification with no further
infrastructure.

Accepted formats, tried in this order:
  1. JSON:  {"title": "...", "message": "...", "level": "info|warning|error",
             "source": "astro_monitor"}
     Every field is optional.
  2. Plain text: the whole datagram becomes `message`.

Guarantees:
- Async UDP socket, started and stopped by the app lifespan
- Malformed payloads do not bring the listener down
- Bounded size: datagrams and fields are truncated

Mistakes prevented:
- A sender pushing megabytes cannot grow the bridge's memory
- Invalid JSON or undecodable bytes raise nothing into the loop
- A failed bind (port taken) does not stop the bridge from starting
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable, Optional

log = logging.getLogger(__name__)

# A datagram longer than this is truncated before parsing even starts.
MAX_DATAGRAM = 8192
# Per-field limits, to keep the in-memory history from growing.
MAX_TITLE = 120
MAX_MESSAGE = 1000
MAX_SOURCE = 64

VALID_LEVELS = ("info", "warning", "error")

# Callback invoked for every valid notification. Async.
NotifyCallback = Callable[[dict], Awaitable[None]]


def parse_datagram(data: bytes, peer: str = "") -> Optional[dict]:
    """Turn a datagram into a normalised notification.

    Returns None when the payload is empty or undecipherable: the caller
    drops it quietly, because anything at all can land on an open UDP port
    (network scanners, stray packets).
    """
    if not data:
        return None
    raw = data[:MAX_DATAGRAM]
    try:
        text = raw.decode("utf-8", errors="replace").strip()
    except Exception:
        return None
    if not text:
        return None

    title = ""
    message = text
    level = "info"
    source = ""

    # Try JSON. A payload that is not JSON is legitimate (plain text), so
    # failing here is not an error worth reporting.
    if text.startswith("{"):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                title = str(obj.get("title", "") or "")
                message = str(obj.get("message", "") or "")
                source = str(obj.get("source", "") or "")
                lvl = str(obj.get("level", "") or "").lower()
                if lvl in VALID_LEVELS:
                    level = lvl
                # JSON with neither title nor message says nothing.
                if not message and not title:
                    return None
        except (ValueError, TypeError):
            pass

    return {
        "title": title[:MAX_TITLE],
        "message": message[:MAX_MESSAGE],
        "level": level,
        "source": (source or peer)[:MAX_SOURCE],
    }


class _NotifyProtocol(asyncio.DatagramProtocol):
    def __init__(self, on_notification: NotifyCallback):
        self._on_notification = on_notification
        self._loop = asyncio.get_event_loop()

    def datagram_received(self, data: bytes, addr) -> None:
        peer = addr[0] if addr else ""
        try:
            payload = parse_datagram(data, peer=peer)
        except Exception:
            log.exception("notify: parse crashed")
            return
        if payload is None:
            return
        # datagram_received is synchronous: delivery must be scheduled.
        asyncio.ensure_future(self._deliver(payload), loop=self._loop)

    async def _deliver(self, payload: dict) -> None:
        try:
            await self._on_notification(payload)
        except Exception:
            log.exception("notify: callback crashed")

    def error_received(self, exc: Exception) -> None:
        # On UDP this is where an ICMP port unreachable lands, say: not a
        # fatal condition, the socket stays usable.
        log.debug("notify: udp error: %s", exc)


class UdpNotifyListener:
    """UDP socket delivering received notifications to a callback."""

    def __init__(self, host: str, port: int, on_notification: NotifyCallback):
        self.host = host
        self.port = port
        self._on_notification = on_notification
        self._transport: Optional[asyncio.DatagramTransport] = None

    @property
    def running(self) -> bool:
        return self._transport is not None

    async def start(self) -> None:
        if self._transport is not None:
            return
        loop = asyncio.get_running_loop()
        try:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: _NotifyProtocol(self._on_notification),
                local_addr=(self.host, self.port),
                allow_broadcast=True,
            )
        except OSError as e:
            # Port taken or address not assignable: the bridge must start
            # anyway, notifications are an accessory feature.
            log.warning("notify: UDP listener disabled, bind %s:%s failed: %s",
                        self.host, self.port, e)
            return
        self._transport = transport
        log.info("notify: UDP listener on %s:%s", self.host, self.port)

    async def stop(self) -> None:
        if self._transport is None:
            return
        try:
            self._transport.close()
        finally:
            self._transport = None

    def local_port(self) -> Optional[int]:
        """The port actually assigned. Useful with port=0 in the tests."""
        if self._transport is None:
            return None
        sock = self._transport.get_extra_info("socket")
        return sock.getsockname()[1] if sock else None
