"""Scènes de test avec vérité terrain : photos réelles « immergées » par le
modèle physique (en lumière linéaire), avec des types d'eau et des cartes de
profondeur variés.  Sert à valider l'algorithme et à calibrer l'auto-réglage.
"""
from __future__ import annotations

import os

import cv2
import numpy as np

from aquimage.core import _lin_to_srgb_f, decode_linear

WATERS = {
    #            beta R,G,B (1/m)        lumière de l'eau B (R,G,B linéaire)
    "ocean":   ((0.50, 0.07, 0.04), (0.010, 0.18, 0.40)),
    "coastal": ((0.45, 0.12, 0.10), (0.020, 0.25, 0.30)),
    "green":   ((0.50, 0.15, 0.25), (0.020, 0.25, 0.14)),
    "turbid":  ((0.60, 0.30, 0.30), (0.050, 0.30, 0.30)),
}


def _sources():
    out = {}
    try:
        import skimage.data as d
        for n in ("coffee", "astronaut", "chelsea", "rocket"):
            out[n] = getattr(d, n)()
    except Exception:
        pass
    try:
        from sklearn.datasets import load_sample_images
        for n, im in zip(("china", "flower"), load_sample_images().images):
            out[n] = im
    except Exception:
        pass
    return out


def depth_map(h, w, kind, rng):
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    y, x = yy / (h - 1), xx / (w - 1)
    if kind == "open":      # eau libre en haut, récif proche en bas
        d = 1.5 + 9.0 * (1 - y) ** 1.5
        top = y < 0.18 + 0.05 * np.sin(x * 6 + rng.uniform(0, 6))
        d[top] = 60.0
    elif kind == "slope":   # gauche proche, droite lointaine
        d = 1.0 + 10.0 * x
    else:                   # « close » : tout entre 1,5 et 4 m
        d = 1.5 + 2.5 * (0.5 + 0.5 * np.sin(3 * x + 2 * y))
    return d.astype(np.float32)


def make_case(img_rgb_u8, water, depth_kind, seed=0, max_side=900):
    rng = np.random.default_rng(seed)
    h, w = img_rgb_u8.shape[:2]
    f = min(1.0, max_side / max(h, w))
    img = cv2.resize(img_rgb_u8, (int(w * f), int(h * f)),
                     interpolation=cv2.INTER_AREA)
    J = decode_linear(img)
    beta, B = (np.array(v, np.float32) for v in WATERS[water])
    d = depth_map(J.shape[0], J.shape[1], depth_kind, rng)
    t = np.exp(-beta[None, None, :] * d[..., None])
    I = J * t + B * (1 - t)
    I = I * 1.0 + rng.normal(0, 0.002, I.shape).astype(np.float32)
    enc = _lin_to_srgb_f(np.clip(I, 0, 1))
    I8 = (enc * 255 + 0.5).astype(np.uint8)
    return {"J": img, "I": I8, "t": t, "B": B, "beta": beta, "d": d,
            "name": f"{water}-{depth_kind}"}


def cases(max_side=900):
    srcs = _sources()
    kinds = ["open", "slope", "close"]
    out = []
    i = 0
    for n, im in srcs.items():
        for water in WATERS:
            k = kinds[i % 3]
            c = make_case(im, water, k, seed=i, max_side=max_side)
            c["name"] = f"{n}/{c['name']}"
            out.append(c)
            i += 1
    return out


def delta_e(a_u8, b_u8):
    from skimage.color import deltaE_ciede2000, rgb2lab
    return float(deltaE_ciede2000(rgb2lab(a_u8), rgb2lab(b_u8)).mean())


if __name__ == "__main__":
    os.makedirs("bench/out", exist_ok=True)
    for c in cases()[:6]:
        cv2.imwrite(f"bench/out/{c['name'].replace('/', '_')}.jpg",
                    cv2.cvtColor(np.hstack([c["J"], c["I"]]), cv2.COLOR_RGB2BGR))
