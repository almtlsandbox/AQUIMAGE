"""
Lecture / écriture des images avec conservation des métadonnées.

- Chemins Unicode sous Windows (« Plongée ») : Pillow et numpy.fromfile,
  jamais cv2.imread/imwrite qui échouent sur ces chemins.
- EXIF conservé (orientation remise à 1 puisque l'image est redressée),
  profil ICC conservé, logiciel = AquImage.
- 8 bits (JPEG, PNG, TIFF, WebP, BMP) et 16 bits (PNG, TIFF) ; le 16 bits
  est traité et enregistré en 16 bits.
- Écriture atomique (fichier temporaire puis renommage).
"""
from __future__ import annotations

import io
import os
import tempfile
from dataclasses import dataclass, field
from typing import Optional, Tuple

import cv2
import numpy as np
from PIL import Image, ImageOps

from . import __version__

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".bmp"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".mts", ".m2ts", ".3gp"}

_TAG_ORIENTATION = 0x0112
_TAG_SOFTWARE = 0x0131


@dataclass
class ImageMeta:
    exif: Optional[bytes] = None
    icc: Optional[bytes] = None
    fmt: str = ""
    bits: int = 8
    extra: dict = field(default_factory=dict)


def is_image(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in IMAGE_EXTS


def is_video(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in VIDEO_EXTS


def _read_16bit(path: str) -> Optional[np.ndarray]:
    data = np.fromfile(path, dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
    if img is None or img.dtype != np.uint16:
        return None
    if img.ndim == 2:
        img = np.stack([img] * 3, axis=2)
    elif img.shape[2] == 4:
        img = img[:, :, :3]
    return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)


def read_image(path: str) -> Tuple[np.ndarray, ImageMeta]:
    """Lit une image en RGB (uint8 ou uint16), redressée selon l'EXIF."""
    with Image.open(path) as im:
        meta = ImageMeta(fmt=(im.format or "").upper(),
                         icc=im.info.get("icc_profile"))
        try:
            exif = im.getexif()
        except Exception:
            exif = None
        mode = im.mode
        if mode in ("I;16", "I;16B", "I;16L", "I") or \
                (meta.fmt in ("TIFF", "PNG") and mode not in ("RGB", "RGBA", "L", "P", "LA", "CMYK", "1")):
            arr = _read_16bit(path)
            if arr is not None:
                meta.bits = 16
                if exif is not None:
                    exif[_TAG_ORIENTATION] = 1
                    meta.exif = exif.tobytes()
                return arr, meta
        im2 = ImageOps.exif_transpose(im)
        if im2.mode != "RGB":
            im2 = im2.convert("RGB")
        arr = np.asarray(im2).copy()
    if meta.fmt in ("TIFF", "PNG") and meta.bits == 8:
        # Un PNG/TIFF 16 bits RGB est lu en 8 bits par Pillow : on vérifie.
        arr16 = _read_16bit(path)
        if arr16 is not None and arr16.shape[:2] == arr.shape[:2]:
            arr, meta.bits = arr16, 16
    if exif is not None and len(exif):
        try:
            exif[_TAG_ORIENTATION] = 1
            exif[_TAG_SOFTWARE] = f"AquImage {__version__}"
            meta.exif = exif.tobytes()
        except Exception:
            meta.exif = None
    return arr, meta


def _atomic_write(path: str, data: bytes) -> None:
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".aqi_", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def encode_image(rgb: np.ndarray, ext: str, meta: Optional[ImageMeta] = None,
                 quality: int = 95) -> bytes:
    """Encode *rgb* au format de l'extension *ext* (avec métadonnées)."""
    ext = ext.lower()
    meta = meta or ImageMeta()
    if rgb.dtype == np.uint16:
        if ext in (".tif", ".tiff", ".png"):
            ok, buf = cv2.imencode(ext, cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
            if not ok:
                raise IOError("échec de l'encodage 16 bits")
            return buf.tobytes()
        rgb = (rgb.astype(np.float32) / 257.0 + 0.5).astype(np.uint8)
    im = Image.fromarray(rgb, "RGB")
    kw = {}
    if meta.icc:
        kw["icc_profile"] = meta.icc
    bio = io.BytesIO()
    if ext in (".jpg", ".jpeg"):
        if meta.exif:
            kw["exif"] = meta.exif
        im.save(bio, "JPEG", quality=int(quality), subsampling=0,
                optimize=True, **kw)
    elif ext == ".png":
        if meta.exif:
            kw["exif"] = meta.exif
        im.save(bio, "PNG", **kw)
    elif ext in (".tif", ".tiff"):
        if meta.exif:
            kw["exif"] = meta.exif
        im.save(bio, "TIFF", compression="tiff_deflate", **kw)
    elif ext == ".webp":
        if meta.exif:
            kw["exif"] = meta.exif
        im.save(bio, "WEBP", quality=int(quality), **kw)
    elif ext == ".bmp":
        im.save(bio, "BMP")
    else:
        raise ValueError(f"format non pris en charge : {ext}")
    return bio.getvalue()


def save_image(path: str, rgb: np.ndarray, meta: Optional[ImageMeta] = None,
               quality: int = 95) -> None:
    ext = os.path.splitext(path)[1]
    _atomic_write(path, encode_image(rgb, ext, meta, quality))


def jpeg_bytes(rgb: np.ndarray, quality: int = 88) -> bytes:
    """JPEG rapide pour l'aperçu (sans métadonnées)."""
    if rgb.dtype == np.uint16:
        rgb = (rgb >> 8).astype(np.uint8)
    ok, buf = cv2.imencode(".jpg", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR),
                           [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
    return buf.tobytes()


def thumbnail(path: str, max_side: int = 320) -> np.ndarray:
    """Vignette RGB uint8 d'une image ou d'une vidéo (image du milieu)."""
    if is_video(path):
        from .video import read_frame_at
        frame = read_frame_at(path, 0.5)
        if frame is None:
            raise IOError("vidéo illisible")
        from .core import resize_max
        return resize_max(frame, max_side)
    with Image.open(path) as im:
        try:
            im.draft("RGB", (max_side * 2, max_side * 2))   # JPEG : décodage réduit
        except Exception:
            pass
        im = ImageOps.exif_transpose(im)
        if im.mode != "RGB":
            if im.mode.startswith("I;16") or im.mode == "I":
                arr = np.asarray(im, dtype=np.float32)
                arr = (arr / max(arr.max(), 1) * 255).astype(np.uint8)
                im = Image.fromarray(arr).convert("RGB")
            else:
                im = im.convert("RGB")
        im.thumbnail((max_side, max_side), Image.LANCZOS)
        return np.asarray(im).copy()
