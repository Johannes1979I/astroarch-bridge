"""Client INDI minimale per catturare i frame (BLOB) di una camera.

Perche' serve
-------------
Con il guider INTERNO di Ekos i frame della camera di guida NON sono
raggiungibili via DBus: `org.kde.kstars.INDI.getBLOBFile` torna vuoto perche'
i BLOB non finiscono nella cache del client INDI di KStars (e non esiste un
`setBLOBMode` esposto su DBus).

Il server INDI pero' accetta **piu' client contemporaneamente** e la modalita'
BLOB e' **per-client**: apriamo una nostra connessione TCP su :7624, chiediamo
`enableBLOB` per la sola camera di guida e leggiamo il prossimo frame che il
driver pubblica. Ekos resta un client separato e continua a guidare
indisturbato (e' esattamente il modo in cui PHD2/Ekos ricevono le immagini).

Protocollo INDI = frammenti XML su TCP, quindi nessuna dipendenza esterna.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import zlib
from xml.etree import ElementTree as ET

_logger = logging.getLogger(__name__)

INDI_HOST = "127.0.0.1"
INDI_PORT = 7624

_BLOB_OPEN = b"<setBLOBVector"
_BLOB_CLOSE = b"</setBLOBVector>"
# Oltre questa soglia buttiamo via il buffer: significa che stiamo ricevendo
# messaggi non-BLOB (defVector, setNumber...) e non vogliamo crescere all'infinito.
_MAX_BUFFER = 24 * 1024 * 1024


def _xml_escape(v: str) -> str:
    return (v.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def _parse_blob_fragment(fragment: bytes) -> bytes | None:
    """Estrae i byte del primo <oneBLOB> da un <setBLOBVector> completo.

    INDI codifica il payload in base64; se `format` finisce per `.z` il
    contenuto e' anche compresso con zlib.
    """
    try:
        root = ET.fromstring(fragment.decode("utf-8", "replace"))
    except ET.ParseError as e:
        _logger.debug("setBLOBVector non parsabile: %s", e)
        return None
    for one in root.findall("oneBLOB"):
        text = (one.text or "").strip()
        if not text:
            continue
        try:
            raw = base64.b64decode(text, validate=False)
        except Exception as e:
            _logger.warning("base64 BLOB non valido: %s", e)
            return None
        fmt = (one.get("format") or "").lower()
        if fmt.endswith(".z"):
            try:
                raw = zlib.decompress(raw)
            except zlib.error as e:
                _logger.warning("BLOB compresso non decomprimibile: %s", e)
                return None
        return raw
    return None


async def grab_blob(device: str, prop: str = "CCD1",
                    timeout: float = 20.0,
                    host: str = INDI_HOST, port: int = INDI_PORT) -> bytes:
    """Ritorna i byte del prossimo frame (di norma un FITS) di `device`.

    Richiede che la camera stia esponendo (loop/guiding): restiamo in ascolto
    fino a `timeout` secondi in attesa del prossimo BLOB pubblicato.

    Solleva asyncio.TimeoutError se non arriva nulla, ConnectionError se il
    server INDI chiude o non e' raggiungibile.
    """
    dev = _xml_escape(device)
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port), timeout=5.0)
    try:
        # 1. chiediamo le property del device, 2. abilitiamo i BLOB per noi.
        writer.write(f'<getProperties version="1.7" device="{dev}"/>'.encode())
        await writer.drain()
        await asyncio.sleep(0.2)  # lascia al server il tempo di registrarci
        writer.write(f'<enableBLOB device="{dev}">Also</enableBLOB>'.encode())
        await writer.drain()

        buf = bytearray()
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError(
                    f"nessun frame BLOB da '{device}' entro {timeout:.0f}s")
            chunk = await asyncio.wait_for(reader.read(262144), timeout=remaining)
            if not chunk:
                raise ConnectionError("connessione al server INDI chiusa")
            buf += chunk

            start = buf.find(_BLOB_OPEN)
            if start < 0:
                # nessun BLOB in vista: tieni solo la coda (un tag potrebbe
                # essere spezzato a meta' fra due chunk).
                if len(buf) > _MAX_BUFFER:
                    del buf[:-4096]
                continue
            end = buf.find(_BLOB_CLOSE, start)
            if end < 0:
                if start > 0:
                    del buf[:start]  # scarta cio' che precede il BLOB
                continue

            frag = bytes(buf[start:end + len(_BLOB_CLOSE)])
            del buf[:end + len(_BLOB_CLOSE)]
            data = _parse_blob_fragment(frag)
            if data:
                return data
            # frammento inutilizzabile: continua ad ascoltare
    finally:
        try:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), timeout=2.0)
        except Exception:
            pass
