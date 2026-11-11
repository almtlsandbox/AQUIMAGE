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

import cv2
import numpy as np

from . import core
from .core import Settings


def _clip(x, lo, hi):
    return float(min(hi, max(lo, x)))


OPEN_WATER_FINE = 0.006      # texture fine en dessous de laquelle c'est de la pleine eau

_SCENE_CODES = {"contre-jour": "backlit", "pleine eau": "openwater",
                "anti-violet": "antiviolet", "hors de l'eau": "air"}

# Réglages selon le déficit de rouge d, par interpolation : pas de saut d'un
# rendu à l'autre entre deux photos presque identiques.
#            d :   -0.36  -0.24   0.02   0.09   0.16   0.24
_D_KNOTS = [-0.36, -0.24, 0.02, 0.09, 0.16, 0.24]
_COLOR = [0.70, 0.30, 0.30, 0.75, 0.75, 1.00]
_RED_COMP = [0.00, 0.00, 0.00, 0.50, 0.50, 1.00]
_BALANCED = [0.00, 1.00, 1.00, 0.00, 0.00, 0.00]     # « déjà équilibrée »


def _lerp(a: float, b: float, w: float) -> float:
    return a + (b - a) * w


def air_evidence(ev: dict, d: float) -> Tuple[float, float]:
    """(rééquilibrée, intacte), chacun de 0 à 1.

    *rééquilibrée* : le rouge est présent et de grandes zones bleues en
    gardent — ciel, mer vue du pont, ou eau déjà corrigée par l'appareil :
    la photo est traitée comme « déjà équilibrée », pas comme sous-marine.
    *intacte* : photo chaude dont les hautes lumières sont neutres (intérieur)
    ou coucher de soleil : rien à corriger.
    """
    shift = core.ramp(ev["red_p90"], 0.85, 1.0) * core.ramp(ev["sky"], 0.10, 0.20)
    lr, lb = abs(np.log(max(ev["hl_rg"], 1e-3))), abs(np.log(max(ev["hl_bg"], 1e-3)))
    neutral_hl = (1.0 - core.ramp(lr, 0.12, 0.20)) * (1.0 - core.ramp(lb, 0.15, 0.25))
    sunset = core.ramp(ev["hl_rg"], 2.2, 2.6) * (1.0 - core.ramp(ev["hl_bg"], 0.40, 0.50))
    intact = max(neutral_hl, sunset) * (1.0 - core.ramp(d, -0.36, -0.24))
    return float(shift), float(intact)


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
    # Texture fine (sans le dégradé de lumière de la surface vers le fond) :
    # une pleine eau en dégradé a un écart-type élevé mais aucun détail.
    fine = float((luma - cv2.GaussianBlur(luma, (0, 0), 0.05 * min(luma.shape))).std())
    ev = core.scene_evidence(enc0)
    shift, intact = air_evidence(ev, d)

    s = base.copy()
    # 1. Nature de la dominante.  Du rouge présent et du ciel : le déficit de
    # rouge moyen vient du bleu du ciel ou de la mer, pas d'une dominante.
    de = d * (1.0 - shift) if d > 0 else d
    s.color = float(np.interp(de, _D_KNOTS, _COLOR))
    s.red_comp = float(np.interp(de, _D_KNOTS, _RED_COMP))
    balanced = float(np.interp(de, _D_KNOTS, _BALANCED))
    if de >= 0.2:
        kind, code = "sous-marine", "underwater"
    elif de > 0.05:
        kind, code = "dominante légère", "light"
    elif de > -0.3:
        kind, code = "déjà équilibrée (flash ou correction appareil)", "balanced"
    else:
        kind, code = "dominante rouge (filtre)", "red"

    # 2. Bruit : plancher de transmission et dose de retrait du voile
    excess = max(0.0, noise - 0.008)
    s.t_min = _clip(0.2 + 10.0 * excess, 0.2, 0.35)
    s.strength = _clip(0.6 - 12.0 * excess, 0.4, 0.6)
    s.strength = min(s.strength, _lerp(0.6, 0.4, balanced))
    if shift > 0:
        s.strength = min(s.strength, _lerp(0.6, 0.25, shift))

    # 3. Situations particulières (dosées, sans seuil net)
    scene = []
    backlit = core.ramp(bright, 0.07, 0.09)
    if backlit > 0.5:
        scene.append("contre-jour")
    # Contre-jour vers la surface : préserver le ciel, déboucher le sujet ;
    # sinon, ombres débouchées seulement si l'original en a besoin.
    s.strength = min(s.strength, _lerp(0.6, 0.45, backlit))
    s.shadows = _lerp(s.shadows * (1.0 - core.tonal_fullness(enc0)),
                      _clip(0.25 + 1.5 * bright, 0.3, 0.6), backlit)
    openw = max(1.0 - core.ramp(texture, 0.07, 0.078),
                1.0 - core.ramp(fine, OPEN_WATER_FINE, OPEN_WATER_FINE + 0.0008))
    if openw > 0:
        # Pleine eau sans sujet : ne pas étirer le bruit ni le vignetage.
        # Ni étirement, ni vibrance : sinon bleu électrique et bruit doublé
        if openw > 0.5:
            scene.append("pleine eau")
        s.stretch = _lerp(s.stretch, 1.0, openw)
        s.strength = _lerp(s.strength, 0.2, openw)
        s.color = _lerp(s.color, min(s.color, 0.5), openw)
        s.vibrance = _lerp(s.vibrance, 0.0, openw)
        s.saturation = _lerp(s.saturation, min(s.saturation, 0.95), openw)
        s.shadows = _lerp(s.shadows, 0.0, openw)
    if intact > 0:
        # Hors de l'eau, lumière chaude voulue : on ne touche presque à rien
        if intact > 0.5:
            scene.append("hors de l'eau")
        for k, v in (("color", 0.0), ("red_comp", 0.0), ("strength", 0.0),
                     ("shadows", 0.0), ("vibrance", 0.0), ("stretch", 1.0),
                     ("black_level", 0.0), ("auto_tone", 0.0)):
            setattr(s, k, _lerp(getattr(s, k), v, intact))
    elif shift > 0.5:
        scene.append("hors de l'eau")

    # 4. Part de teinte gardée dans l'eau lointaine (plan préliminaire)
    pre = core.build_plan(img, s, small=small)
    keep = pre.stats["auto_keep"]
    if keep > 0:
        keep = max(keep, 0.7 * balanced)
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

    # 5 bis. Réduction du bruit, selon ce que la correction l'a amplifié
    noise_gain = float(plan.stats.get("noise_gain", 1.0))
    s.denoise = _clip((noise_gain - 1.1) / 0.8, 0.0, 1.0)

    # 6. Clarté selon le contraste obtenu (plus douce si déjà contrasté)
    std = float(core.guide_of(core.to_float(out)).std())
    s.contrast = _clip(0.35 - 1.5 * (std - 0.15), 0.08, 0.35)
    s.contrast = _lerp(s.contrast, 0.08, openw)
    s.contrast = _lerp(s.contrast, 0.0, intact)
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
        "red_p90": round(ev["red_p90"], 3),
        "sky": round(ev["sky"], 3),
        "air": round(max(shift, intact), 3),
        "noise_gain": round(noise_gain, 2),
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
