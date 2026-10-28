"""
Estimation automatique des réglages, image par image (ou vidéo par vidéo).

Les règles ont été calibrées sur un jeu de 35 photos de plongée réelles
(eau verte, eau bleue, épaves sombres, photos au flash, photos déjà
corrigées par l'appareil) et sur des scènes simulées à vérité terrain.

Signaux utilisés :
- déficit de rouge  d = 1 − R̄/Ḡ  : vraie dominante sous-marine (d > 0,2),
  légère (0..0,2), ou photo déjà équilibrée / au flash (d ≤ 0) ;
- bruit estimé : relève t_min et modère le retrait du voile ;
- couleur de l'eau lointaine : part de teinte gardée pour un fond bleu ;
- contraste obtenu : dose de clarté.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import numpy as np

from . import core
from .core import Settings


def _clip(x, lo, hi):
    return float(min(hi, max(lo, x)))


def auto_settings(img: np.ndarray, base: Optional[Settings] = None,
                  stats: Optional[dict] = None) -> Tuple[Settings, dict]:
    """Réglages adaptés à *img* (RGB encodé).  Retourne (réglages, diagnostic).

    *base* : réglages de départ (les réglages créatifs — température,
    teinte, luminosité, saturation — sont conservés).
    """
    base = base or Settings()
    small = core.resize_max(img, core.ANALYSIS_MAX_SIDE)
    st = stats or core.image_stats(img)
    d = st["red_deficit"]
    noise = st["noise"]

    s = base.copy()
    # 1. Nature de la dominante
    if d >= 0.2:
        kind = "sous-marine"
        s.red_comp, s.color = 1.0, 1.0
    elif d > 0.05:
        kind = "dominante légère"
        s.red_comp, s.color = 0.5, 0.75
    elif d > -0.3:
        kind = "déjà équilibrée (flash ou correction appareil)"
        s.red_comp, s.color = 0.0, 0.3
    else:
        kind = "dominante rouge (filtre)"
        s.red_comp, s.color = 0.0, 0.7

    # 2. Bruit : plancher de transmission et dose de retrait du voile
    excess = max(0.0, noise - 0.008)
    s.t_min = _clip(0.2 + 10.0 * excess, 0.2, 0.35)
    s.strength = _clip(0.8 - 15.0 * excess, 0.5, 0.8)
    if kind.startswith("déjà"):
        s.strength = min(s.strength, 0.5)

    # 3. Part de teinte gardée dans l'eau lointaine (plan préliminaire)
    pre = core.build_plan(img, s, small=small)
    keep = pre.stats["auto_keep"]
    if kind.startswith("déjà") and keep > 0:
        keep = max(keep, 0.7)
    s.water_keep = keep

    # 4. Clarté selon le contraste obtenu
    plan = core.build_plan(img, s, small=small)
    out = core.apply_plan(small, plan)
    std = float(core.guide_of(core.to_float(out)).std())
    s.contrast = _clip(0.5 - 2.0 * (std - 0.15), 0.15, 0.5)
    s.clamp()

    diag = {
        "type": kind,
        "red_deficit": round(d, 3),
        "noise": round(noise, 4),
        "luma_std": round(std, 3),
        "water_keep": round(keep, 3),
    }
    return s, diag


def auto_settings_multi(frames: List[np.ndarray],
                        base: Optional[Settings] = None) -> Tuple[Settings, dict]:
    """Réglages communs à plusieurs images (images clés d'une vidéo) :
    médiane des réglages automatiques de chaque image."""
    if not frames:
        return (base or Settings()), {}
    all_s = [auto_settings(f, base)[0].to_dict() for f in frames]
    keys = all_s[0].keys()
    med = {k: float(np.median([d[k] for d in all_s])) for k in keys}
    s = Settings.from_dict(med)
    _, diag = auto_settings(frames[len(frames) // 2], s)
    diag["frames"] = len(frames)
    return s, diag
