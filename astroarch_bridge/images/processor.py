"""Conversione FITS -> JPEG con auto-stretch.

DoD:
- Apre FITS (mono o RGB) via astropy.io.fits
- Auto-stretch tipo MTF (Midtone Transfer Function) con clipping percentile
- Resize a image_max_dim mantenendo aspect ratio
- Produce JPEG bytes + thumbnail bytes
- Estrae statistiche basilari: median, std, min/max, hfr_approx, star_count
- Mai blocca event loop -> wrapper async che usa to_thread

Errori prevenuti:
- E6: numpy/astropy CPU-heavy -> tutto in thread pool
- FITS multi-extension -> usa primo HDU con dati
- bayer pattern raw color -> debayer base (RGGB) se header BAYERPAT presente
"""
from __future__ import annotations

import asyncio
import io
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
from astropy.io import fits
from PIL import Image

log = logging.getLogger(__name__)


@dataclass
class ProcessResult:
    jpeg: bytes
    thumbnail: bytes
    width: int
    height: int
    median: float
    std: float
    vmin: float
    vmax: float
    p999: float  # 99.9° percentile = luminosità del soggetto (disco), robusto al fondo
    hfr_approx: float
    star_count: int
    is_color: bool
    bayer_pattern: Optional[str]
    exposure: Optional[float]
    filter_name: Optional[str]
    frame_type: Optional[str]
    object_name: Optional[str]


def _read_fits(path: Path) -> tuple[np.ndarray, dict]:
    """Legge dati FITS + header dict del primo HDU con immagine valida."""
    with fits.open(path, memmap=False) as hdul:
        for hdu in hdul:
            data = hdu.data
            if data is None:
                continue
            if data.ndim < 2:
                continue
            return np.asarray(data), dict(hdu.header)
    raise ValueError(f"No image HDU in {path}")


def _read_fits_bytes(data: bytes) -> tuple[np.ndarray, dict]:
    """Legge dati FITS direttamente da bytes (no I/O su disco)."""
    import io as _io
    with fits.open(_io.BytesIO(data), memmap=False) as hdul:
        for hdu in hdul:
            d = hdu.data
            if d is None or d.ndim < 2:
                continue
            return np.asarray(d), dict(hdu.header)
    raise ValueError("No image HDU in FITS bytes")


def _percentile_stretch(data: np.ndarray, low: float = 0.5, high: float = 99.5) -> np.ndarray:
    """Auto-stretch in stile PixInsight Screen Transfer Function (auto-STF) —
    è la stessa famiglia di algoritmi che usa KStars FITS Viewer in auto.

    Differenza chiave dalla versione precedente: il midtone `m` NON è fisso
    (prima 0.05 saturava cieli inquinati o immagini con Vega nel fov), è
    calcolato dalla mediana normalizzata dell'immagine in modo che essa
    mappi al target di background (0.25, default PI). Risultato: immagini
    scure vengono fortemente schiarite (m basso), immagini già brillanti
    quasi non vengono toccate (m → 0.5).

    Algoritmo:
      1. Robust black/white via ZScale-like (median ± k·σ_MAD)
      2. Normalizzazione lineare → [0,1]
      3. Calcolo c = (median - black) / (white - black)
      4. m = c·(1-T) / (c·(1-2T) + T)   con T = 0.25
      5. Applicazione MTF:  out = (m-1)·x / ((2m-1)·x - m)

    Nota: low/high parametri ignorati ma mantenuti per backward compat.
    """
    finite = np.isfinite(data)
    if not finite.any():
        return np.zeros(data.shape, dtype=np.uint8)
    flat = data[finite].astype(np.float64)

    sample = flat
    if sample.size > 200_000:
        idx = np.random.choice(sample.size, 200_000, replace=False)
        sample = sample[idx]
    median = float(np.median(sample))
    mad = float(np.median(np.abs(sample - median)))
    sigma_eq = mad * 1.4826 if mad > 0 else 1.0

    # Shadow clipping a ~2.8σ sotto la mediana; white al 99.9 percentile
    # (più morbido del 99.5 di prima → meno saturazione delle stelle).
    black = max(float(np.min(sample)), median - 2.8 * sigma_eq)
    white = float(np.percentile(sample, 99.9))
    if white <= black:
        white = black + max(1.0, sigma_eq)

    # Mediana NORMALIZZATA dopo clipping → input per il calcolo di m
    span = max(white - black, 1e-9)
    c = float(np.clip((median - black) / span, 0.0, 1.0))

    target_bg = 0.25  # default PixInsight: la mediana finisce a 25/255
    if c <= 0.0:
        m = target_bg
    elif c >= 0.5:
        # L'immagine è già brillante (mediana sopra il 50% dell'intervallo
        # dinamico) → nessuno stretch dei toni medi, m = 0.5 = identità.
        m = 0.5
    else:
        num = c * (1.0 - target_bg)
        den = c * (1.0 - 2.0 * target_bg) + target_bg
        m = num / den if abs(den) > 1e-9 else target_bg
        m = max(0.001, min(0.5, m))

    # Stretch
    norm = np.clip((data - black) / span, 0.0, 1.0)
    denom = ((2.0 * m - 1.0) * norm) - m
    denom = np.where(np.abs(denom) < 1e-9, 1e-9, denom)
    stretched = (m - 1.0) * norm / denom
    stretched = np.clip(stretched, 0.0, 1.0)
    return (stretched * 255.0 + 0.5).astype(np.uint8)


def _debayer_rggb(raw: np.ndarray, pattern: str) -> np.ndarray:
    """Debayer molto semplice (nearest) - per preview, non per scientific use."""
    pattern = pattern.upper()
    if pattern not in {"RGGB", "BGGR", "GRBG", "GBRG"}:
        # fallback: tratta come mono
        return raw
    h, w = raw.shape
    h2, w2 = h - h % 2, w - w % 2
    raw = raw[:h2, :w2]
    r = np.zeros((h2 // 2, w2 // 2), dtype=raw.dtype)
    g = np.zeros((h2 // 2, w2 // 2), dtype=raw.dtype)
    b = np.zeros((h2 // 2, w2 // 2), dtype=raw.dtype)
    if pattern == "RGGB":
        r[:] = raw[0::2, 0::2]
        g[:] = (raw[0::2, 1::2].astype(np.int32) + raw[1::2, 0::2].astype(np.int32)) // 2
        b[:] = raw[1::2, 1::2]
    elif pattern == "BGGR":
        b[:] = raw[0::2, 0::2]
        g[:] = (raw[0::2, 1::2].astype(np.int32) + raw[1::2, 0::2].astype(np.int32)) // 2
        r[:] = raw[1::2, 1::2]
    elif pattern == "GRBG":
        g[:] = (raw[0::2, 0::2].astype(np.int32) + raw[1::2, 1::2].astype(np.int32)) // 2
        r[:] = raw[0::2, 1::2]
        b[:] = raw[1::2, 0::2]
    elif pattern == "GBRG":
        g[:] = (raw[0::2, 0::2].astype(np.int32) + raw[1::2, 1::2].astype(np.int32)) // 2
        b[:] = raw[0::2, 1::2]
        r[:] = raw[1::2, 0::2]
    rgb = np.dstack([r, g, b]).astype(raw.dtype)
    return rgb


def _estimate_stars(gray: np.ndarray, sigma_threshold: float = 5.0) -> tuple[float, int]:
    """Stima rapida HFR e numero stelle.
    Approccio: threshold a median+sigma*std, label connected components, conta blob 3-300 px.
    HFR = sqrt(area/pi) media.
    """
    finite = np.isfinite(gray)
    med = float(np.median(gray[finite])) if finite.any() else 0.0
    std = float(np.std(gray[finite])) if finite.any() else 1.0
    if std <= 0:
        return 0.0, 0
    thresh = med + sigma_threshold * std
    mask = gray > thresh
    if not mask.any():
        return 0.0, 0
    # CC labeling iterativo a stack (no scipy) - O(N) con flood fill
    visited = np.zeros_like(mask, dtype=np.bool_)
    H, W = mask.shape
    star_areas: list[int] = []
    # Limita scan a downsample 4x per velocità su immagini grandi
    step = 1 if max(H, W) <= 1500 else 2
    for y in range(0, H, step):
        for x in range(0, W, step):
            if not mask[y, x] or visited[y, x]:
                continue
            # BFS
            stack = [(y, x)]
            area = 0
            while stack and area < 1000:
                cy, cx = stack.pop()
                if cy < 0 or cy >= H or cx < 0 or cx >= W:
                    continue
                if visited[cy, cx] or not mask[cy, cx]:
                    continue
                visited[cy, cx] = True
                area += 1
                stack.append((cy + 1, cx))
                stack.append((cy - 1, cx))
                stack.append((cy, cx + 1))
                stack.append((cy, cx - 1))
            if 3 <= area <= 300:
                star_areas.append(area)
    if not star_areas:
        return 0.0, 0
    avg_area = float(np.mean(star_areas))
    hfr = float(np.sqrt(avg_area / np.pi))
    return hfr, len(star_areas)


def _to_pil_image(arr_u8: np.ndarray) -> Image.Image:
    if arr_u8.ndim == 2:
        return Image.fromarray(arr_u8, mode="L").convert("RGB")
    if arr_u8.ndim == 3 and arr_u8.shape[2] == 3:
        return Image.fromarray(arr_u8, mode="RGB")
    raise ValueError(f"unsupported array shape: {arr_u8.shape}")


def _resize_keep_aspect(img: Image.Image, max_dim: int) -> Image.Image:
    w, h = img.size
    m = max(w, h)
    if m <= max_dim:
        return img
    scale = max_dim / float(m)
    return img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)


def process_fits_sync(
    path: Path,
    max_dim: int = 1600,
    thumbnail_dim: int = 512,
    jpeg_quality: int = 85,
) -> ProcessResult:
    """Processo bloccante da path - chiamare via asyncio.to_thread."""
    raw, header = _read_fits(path)
    return _process_array(raw, header, max_dim, thumbnail_dim, jpeg_quality)


async def process_fits_async(
    path: Path,
    max_dim: int = 1600,
    thumbnail_dim: int = 512,
    jpeg_quality: int = 85,
) -> ProcessResult:
    return await asyncio.to_thread(
        process_fits_sync, path, max_dim, thumbnail_dim, jpeg_quality
    )


def process_fits_bytes_sync(
    data: bytes,
    max_dim: int = 1600,
    thumbnail_dim: int = 512,
    jpeg_quality: int = 85,
) -> ProcessResult:
    """Processa bytes FITS direttamente (no scrittura su disco). Usato per i
    BLOB ricevuti via INDI quando il bridge è secondo client (non scrive file).
    """
    raw, header = _read_fits_bytes(data)
    return _process_array(raw, header, max_dim, thumbnail_dim, jpeg_quality)


async def process_fits_bytes_async(
    data: bytes,
    max_dim: int = 1600,
    thumbnail_dim: int = 512,
    jpeg_quality: int = 85,
) -> ProcessResult:
    return await asyncio.to_thread(
        process_fits_bytes_sync, data, max_dim, thumbnail_dim, jpeg_quality
    )


def _process_array(raw: np.ndarray, header: dict, max_dim: int,
                   thumbnail_dim: int, jpeg_quality: int) -> ProcessResult:
    """Logica condivisa di stretching + JPEG."""
    bayer_pattern = header.get("BAYERPAT") or header.get("BAYRPAT")
    is_color = False
    if raw.ndim == 3 and raw.shape[2] == 3:
        is_color = True
        chans = [_percentile_stretch(raw[..., c]) for c in range(3)]
        rgb_u8 = np.dstack(chans)
    elif raw.ndim == 2 and bayer_pattern:
        debayered = _debayer_rggb(raw, str(bayer_pattern))
        if debayered.ndim == 3:
            is_color = True
            chans = [_percentile_stretch(debayered[..., c]) for c in range(3)]
            rgb_u8 = np.dstack(chans)
        else:
            rgb_u8 = _percentile_stretch(debayered)
    elif raw.ndim == 2:
        rgb_u8 = _percentile_stretch(raw)
    else:
        rgb_u8 = _percentile_stretch(raw[0])
    if rgb_u8.ndim == 3:
        gray = (0.299 * rgb_u8[..., 0] + 0.587 * rgb_u8[..., 1] + 0.114 * rgb_u8[..., 2]).astype(np.uint8)
    else:
        gray = rgb_u8
    finite = np.isfinite(raw)
    median = float(np.median(raw[finite])) if finite.any() else 0.0
    std = float(np.std(raw[finite])) if finite.any() else 0.0
    vmin = float(np.min(raw[finite])) if finite.any() else 0.0
    vmax = float(np.max(raw[finite])) if finite.any() else 0.0
    # p99.9 = livello del soggetto luminoso (disco Sole/Luna): robusto al fondo
    # scuro, a differenza della mediana di tutto il frame. Usato per calibrare
    # l'esposizione nell'eclissi (conduttore).
    p999 = float(np.percentile(raw[finite], 99.9)) if finite.any() else 0.0
    hfr, n_stars = _estimate_stars(gray)
    img = _to_pil_image(rgb_u8)
    big = _resize_keep_aspect(img, max_dim)
    thumb = _resize_keep_aspect(img, thumbnail_dim)
    big_buf = io.BytesIO()
    big.save(big_buf, format="JPEG", quality=jpeg_quality, optimize=True)
    thumb_buf = io.BytesIO()
    thumb.save(thumb_buf, format="JPEG", quality=max(60, jpeg_quality - 10), optimize=True)
    return ProcessResult(
        jpeg=big_buf.getvalue(), thumbnail=thumb_buf.getvalue(),
        width=big.width, height=big.height,
        median=median, std=std, vmin=vmin, vmax=vmax, p999=p999,
        hfr_approx=hfr, star_count=n_stars,
        is_color=is_color, bayer_pattern=str(bayer_pattern) if bayer_pattern else None,
        exposure=_safe_float(header.get("EXPTIME") or header.get("EXPOSURE")),
        filter_name=_safe_str(header.get("FILTER")),
        frame_type=_safe_str(header.get("FRAME") or header.get("IMAGETYP")),
        object_name=_safe_str(header.get("OBJECT")),
    )


def _safe_float(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _safe_str(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None
