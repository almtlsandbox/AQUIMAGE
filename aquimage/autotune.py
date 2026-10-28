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


_SCENE_CODES = {"contre-jour": "backlit", "pleine eau": "openwater",
                "anti-violet": "antiviolet"}


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
    enc0 = core.to_float(small)
    luma = core.guide_of(enc0)
    bright = float((luma > 0.78).mean())          # part de ciel / surface claire
    texture = float(luma.std())                   # pleine eau : image très plate

    s = base.copy()
    # 1. Nature de la dominante
    if d >= 0.2:
        kind, code = "sous-marine", "underwater"
        s.red_comp, s.color = 1.0, 1.0
    elif d > 0.05:
        kind, code = "dominante légère", "light"
        s.red_comp, s.color = 0.5, 0.75
    elif d > -0.3:
        kind, code = "déjà équilibrée (flash ou correction appareil)", "balanced"
        s.red_comp, s.color = 0.0, 0.3
    else:
        kind, code = "dominante rouge (filtre)", "red"
        s.red_comp, s.color = 0.0, 0.7

    # 2. Bruit : plancher de transmission et dose de retrait du voile
    excess = max(0.0, noise - 0.008)
    s.t_min = _clip(0.2 + 10.0 * excess, 0.2, 0.35)
    s.strength = _clip(0.6 - 12.0 * excess, 0.4, 0.6)
    if kind.startswith("déjà"):
        s.strength = min(s.strength, 0.4)

    # 3. Situations particulières
    scene = []
    if bright > 0.08:
        # Contre-jour vers la surface : préserver le ciel, déboucher le sujet
        scene.append("contre-jour")
        s.strength = min(s.strength, 0.45)
        s.shadows = _clip(0.25 + 1.5 * bright, 0.3, 0.6)
    if texture < 0.07:
        # Pleine eau sans sujet : ne pas étirer le bruit ni le vignetage
        scene.append("pleine eau")
        # Ni étirement, ni vibrance : sinon bleu électrique et bruit doublé
        s.stretch, s.strength, s.color = 1.0, 0.2, min(s.color, 0.5)
        s.vibrance, s.saturation, s.shadows = 0.0, min(s.saturation, 0.95), 0.0

    # 4. Part de teinte gardée dans l'eau lointaine (plan préliminaire)
    pre = core.build_plan(img, s, small=small)
    keep = pre.stats["auto_keep"]
    if kind.startswith("déjà") and keep > 0:
        keep = max(keep, 0.7)
    s.water_keep = keep

    # 5. Garde-fou : ombres violettes (rouge trop poussé dans l'eau bleue)
    for _ in range(3):
        plan = core.build_plan(img, s, small=small)
        out = core.apply_plan(small, plan)
        a_dark, b_dark, a_std = _dark_ab(out)
        # Dominante violette = teinte magenta UNIFORME dans les ombres (des
        # objets réellement violets, eux, ont une teinte très variable).
        if a_dark > 5.0 and b_dark < 2.0 and a_std < 12.0:
            s.color *= 0.75
            s.red_comp *= 0.8
            s.max_gain = max(2.0, s.max_gain * 0.75)
            if "anti-violet" not in scene:
                scene.append("anti-violet")
        else:
            break

    # 6. Clarté selon le contraste obtenu (plus douce si déjà contrasté)
    std = float(core.guide_of(core.to_float(out)).std())
    s.contrast = _clip(0.35 - 1.5 * (std - 0.15), 0.08, 0.35)
    if "pleine eau" in scene:
        s.contrast = 0.08
    s.clamp()

    diag = {
        "type": kind + (" ; " + ", ".join(scene) if scene else ""),
        # Codes indépendants de la langue (traduits par l'interface)
        "kind": code,
        "scene": [_SCENE_CODES[x] for x in scene],
        "red_deficit": round(d, 3),
        "noise": round(noise, 4),
        "bright": round(bright, 3),
        "luma_std": round(std, 3),
        "water_keep": round(keep, 3),
    }
    return s, diag


def _dark_ab(rgb_u8: np.ndarray) -> Tuple[float, float, float]:
    """Teinte moyenne (a*, b* de Lab) des ombres d'une image RGB uint8, et
    écart-type de a* (uniformité de la teinte)."""
    import cv2
    lab = cv2.cvtColor(rgb_u8, cv2.COLOR_RGB2LAB).astype(np.float32)
    L = lab[..., 0] * (100.0 / 255.0)
    m = (L > 8) & (L < 45)
    if m.sum() < 200:
        return 0.0, 0.0, 99.0
    a = lab[..., 1][m] - 128
    return float(a.mean()), float(lab[..., 2][m].mean() - 128), float(a.std())


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
