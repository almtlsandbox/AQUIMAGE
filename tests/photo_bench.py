"""Banc de non-régression sur de vraies photos de plongée (réduites).

    python tests/photo_bench.py            compare à la référence
    python tests/photo_bench.py --update   enregistre la référence

La référence (tests/photos/baseline.json) contient, pour chaque photo, des
mesures de la correction automatique : ombres pourpres ou rougies, blancs
écrêtés, luminosité, contraste, couleur, bruit.  ``pytest`` vérifie qu'elles
ne dérivent pas ; après un changement voulu de l'algorithme, on regarde les
écarts puis on met la référence à jour.
"""
from __future__ import annotations

import glob
import json
import os
import sys

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PHOTOS = os.path.join(HERE, "photos")
BASELINE = os.path.join(PHOTOS, "baseline.json")

# Écart toléré par mesure (absolu) avant de signaler une dérive
TOL = {"purple": 1.5, "red_shadows": 2.5, "clipped": 0.3, "L50": 4.0, "dL50": 4.0,
       "contrast": 2.5, "chroma": 3.0, "a": 2.5, "b": 3.0, "noise_x": 0.35}


def _lab(rgb):
    lab = cv2.cvtColor(rgb.astype(np.float32) / 255.0, cv2.COLOR_RGB2LAB)
    return lab[..., 0], lab[..., 1], lab[..., 2]


def _noise(L):
    k = np.array([[1, -2, 1], [-2, 4, -2], [1, -2, 1]], np.float32)
    return float(np.abs(cv2.filter2D(L, -1, k)[1:-1, 1:-1]).mean())


def measure(orig: np.ndarray, out: np.ndarray) -> dict:
    """Mesures (en % ou en unités Lab) d'une correction."""
    Lo, ao, bo = _lab(orig)
    L, a, b = _lab(out)
    C = np.hypot(a, b)
    hue = (np.degrees(np.arctan2(b, a)) + 360.0) % 360.0
    purple = ((hue >= 318) | (hue < 10)) & (C > 8) & (L > 5) & (L < 60)
    redsh = (L > 3) & (L < 30) & (C > 5) & ((hue < 70) | (hue > 345))
    m = {
        "purple": 100.0 * purple.mean(),            # ombres pourpres
        "red_shadows": 100.0 * redsh.mean(),        # ombres rougies / brique
        "clipped": 100.0 * (out.max(axis=-1) >= 254).mean(),
        "L50": float(np.median(L)),
        "dL50": float(np.median(L) - np.median(Lo)),
        "contrast": float(L.std()),
        "chroma": float(C.mean()),
        "a": float(a.mean()), "b": float(b.mean()),
        "noise_x": _noise(L) / max(_noise(Lo), 1e-3),   # bruit multiplié par…
    }
    return {k: round(float(v), 2) for k, v in m.items()}


def load_photos() -> dict:
    out = {}
    for p in sorted(glob.glob(os.path.join(PHOTOS, "*.jpg"))):
        bgr = cv2.imread(p, cv2.IMREAD_COLOR)
        out[os.path.splitext(os.path.basename(p))[0]] = np.ascontiguousarray(bgr[..., ::-1])
    return out


def run() -> dict:
    """Corrige chaque photo en automatique et la mesure."""
    sys.path.insert(0, os.path.dirname(HERE))
    from aquimage import autotune, core
    res = {}
    for name, img in load_photos().items():
        s, diag = autotune.auto_settings(img)
        out = core.apply_plan(img, core.build_plan(img, s))
        res[name] = dict(measure(img, out), kind=diag.get("kind", ""))
    return res


def compare(res: dict, base: dict) -> list:
    """Liste des dérives (photo, mesure, référence, valeur)."""
    bad = []
    for name, ref in base.items():
        cur = res.get(name)
        if cur is None:
            bad.append((name, "absente", 0, 0))
            continue
        for k, tol in TOL.items():
            if abs(cur[k] - ref[k]) > tol:
                bad.append((name, k, ref[k], cur[k]))
    return bad


def main(argv):
    res = run()
    if "--update" in argv or not os.path.exists(BASELINE):
        with open(BASELINE, "w", encoding="utf-8") as f:
            json.dump(res, f, indent=1, sort_keys=True)
        print("Référence enregistrée : %d photos" % len(res))
        return 0
    with open(BASELINE, encoding="utf-8") as f:
        base = json.load(f)
    bad = compare(res, base)
    for name, k, ref, cur in bad:
        print("%-10s %-12s %8.2f -> %8.2f" % (name, k, ref, cur))
    keys = list(TOL)
    print("%-12s %9s %9s" % ("moyenne", "référence", "actuel"))
    for k in keys:
        print("%-12s %9.2f %9.2f" % (k, np.mean([base[n][k] for n in base]),
                                     np.mean([res[n][k] for n in base if n in res])))
    print("%d dérive(s) sur %d photos" % (len(bad), len(base)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
