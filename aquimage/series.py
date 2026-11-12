"""
Cohérence des séries : des photos prises à quelques secondes d'intervalle, de
la même scène, doivent recevoir des réglages voisins.  Réglée seule, chaque
photo peut tomber d'un côté ou de l'autre d'une décision (un poisson passe,
un peu plus d'eau dans le cadre) et la série paraît inégale.

On regroupe les photos consécutives proches dans le temps ET d'aspect
semblable, puis on rapproche les réglages automatiques de chacune de ceux de
sa série.
"""
from __future__ import annotations

import os
import threading
from collections import OrderedDict
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image

from . import autotune, fileio
from .core import Settings, resize_max, to_float

SERIES_GAP = 8.0          # secondes au plus entre deux photos d'une même série
SERIES_RED = 0.10         # écart maximal du déficit de rouge
SERIES_RGB = 0.07         # écart maximal de la couleur moyenne (valeurs 0..1)
SERIES_PULL = 0.7         # part du chemin vers les réglages de la série
SERIES_SPAN = 2           # voisines prises en compte de chaque côté
# Réglages décidés par l'analyse automatique (les réglages créatifs —
# température, teinte, luminosité — ne sont pas touchés)
SMOOTHED = ("red_comp", "color", "max_gain", "water_keep", "strength", "t_min",
            "contrast", "saturation", "vibrance", "shadows", "black_level",
            "stretch", "auto_tone", "denoise")

_TAG_EXIF_IFD, _TAG_DATETIME = 0x8769, 0x0132
_TAG_ORIGINAL, _TAG_SUBSEC = 0x9003, 0x9291


def capture_time(path: str) -> Optional[float]:
    """Heure de prise de vue (EXIF, en secondes), ou None si inconnue."""
    try:
        with Image.open(path) as im:
            exif = im.getexif()
            sub = exif.get_ifd(_TAG_EXIF_IFD)
            txt = sub.get(_TAG_ORIGINAL) or exif.get(_TAG_DATETIME)
            if not txt:
                return None
            t = datetime.strptime(str(txt).strip()[:19], "%Y:%m:%d %H:%M:%S").timestamp()
            frac = str(sub.get(_TAG_SUBSEC) or "").strip()
            if frac.isdigit():
                t += float("0." + frac)
            return t
    except Exception:
        return None


def group(infos: Sequence[dict]) -> List[List[int]]:
    """Séries parmi *infos* (un dict par photo : ``folder``, ``time``,
    ``red`` = déficit de rouge, ``rgb`` = couleur moyenne).  Retourne les
    listes d'indices des séries d'au moins deux photos."""
    order = sorted((i for i, f in enumerate(infos) if f.get("time") is not None),
                   key=lambda i: (infos[i].get("folder", ""), infos[i]["time"]))
    groups: List[List[int]] = []
    cur: List[int] = []
    for i in order:
        if cur:
            same = _similar(infos[cur[-1]], infos[i])
            if not same:
                if len(cur) > 1:
                    groups.append(cur)
                cur = []
        cur.append(i)
    if len(cur) > 1:
        groups.append(cur)
    return groups


def window(order: Sequence[int], pos: int) -> List[int]:
    """Voisines de la photo en position *pos* de sa série (elle comprise) :
    SERIES_SPAN de chaque côté au plus."""
    return list(order[max(0, pos - SERIES_SPAN):pos + SERIES_SPAN + 1])


def pull(own: Settings, others: Sequence[Settings],
         amount: float = SERIES_PULL) -> Settings:
    """Rapproche *own* de la médiane de *others* (qui le contient)."""
    d = own.to_dict()
    dicts = [o.to_dict() for o in others]
    for k in SMOOTHED:
        d[k] = d[k] + amount * (float(np.median([x[k] for x in dicts])) - d[k])
    return Settings.from_dict(d)


def smooth(settings: Sequence[Settings], groups: Sequence[Sequence[int]]
           ) -> Dict[int, Settings]:
    """{indice: réglages rapprochés de ceux de ses voisines de série}."""
    out: Dict[int, Settings] = {}
    for g in groups:
        for pos, i in enumerate(g):
            out[i] = pull(settings[i], [settings[j] for j in window(g, pos)])
    return out


# ---------------------------------------------------------------------------
# Réglages automatiques d'un fichier, série comprise (lot et atelier)
# ---------------------------------------------------------------------------

_CACHE: "OrderedDict[tuple, tuple]" = OrderedDict()
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 4000


def _key(path: str) -> tuple:
    st = os.stat(path)
    return (os.path.abspath(path), st.st_mtime_ns, st.st_size)


def photo_auto(path: str) -> Tuple[Settings, dict, dict]:
    """(réglages automatiques, diagnostic, infos de série) d'une photo, à
    partir de sa copie d'analyse.  Résultat gardé en mémoire."""
    key = _key(path)
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is not None:
            _CACHE.move_to_end(key)
            return hit[0].copy(), dict(hit[1]), hit[2]
    img = fileio.read_analysis(path)
    s, diag = autotune.auto_settings(img)
    m = to_float(resize_max(img, 640)).reshape(-1, 3).mean(axis=0)
    info = {"folder": os.path.dirname(key[0]), "time": capture_time(path),
            "red": float(diag.get("red_deficit", 0.0)),
            "rgb": [float(v) for v in m]}
    with _CACHE_LOCK:
        _CACHE[key] = (s, diag, info)
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)
    return s.copy(), dict(diag), info


def _similar(a: dict, b: dict) -> bool:
    return (a.get("folder", "") == b.get("folder", "")
            and a.get("time") is not None and b.get("time") is not None
            and abs(b["time"] - a["time"]) <= SERIES_GAP
            and abs(a["red"] - b["red"]) <= SERIES_RED
            and float(np.abs(np.asarray(a["rgb"]) - np.asarray(b["rgb"])).max())
            <= SERIES_RGB)


def auto_for(path: str, siblings: Optional[Sequence[str]] = None
             ) -> Tuple[Settings, dict]:
    """Réglages automatiques d'une photo, rapprochés de ceux de ses voisines
    de série.  *siblings* : photos du même dossier, dans l'ordre (par défaut,
    celles du dossier, triées par nom)."""
    s, diag, info = photo_auto(path)
    if info["time"] is None:
        return s, diag
    if siblings is None:
        folder = os.path.dirname(os.path.abspath(path))
        try:
            siblings = sorted((e.path for e in os.scandir(folder)
                               if e.is_file() and fileio.is_image(e.name)
                               and not e.name.startswith(".")),
                              key=lambda q: os.path.basename(q).lower())
        except OSError:
            siblings = [path]
    names = [os.path.abspath(q) for q in siblings]
    try:
        pos = names.index(os.path.abspath(path))
    except ValueError:
        return s, diag
    near = [s]
    for step in (-1, 1):
        prev = info
        for k in range(1, SERIES_SPAN + 1):
            j = pos + step * k
            if not 0 <= j < len(names):
                break
            # L'heure d'abord (lecture de l'EXIF, immédiate) : la plupart des
            # voisines ne sont pas de la série et ne sont pas analysées.
            t = capture_time(names[j])
            if t is None or abs(t - prev["time"]) > SERIES_GAP:
                break
            try:
                sj, _, ij = photo_auto(names[j])
            except Exception:
                break
            if not _similar(prev, ij):
                break
            near.append(sj)
            prev = ij
    if len(near) > 1:
        s = pull(s, near)
        diag["series"] = len(near)
    return s, diag
