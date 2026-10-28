"""Évalue AquImage contre la vérité terrain (et AquaCol v1 si disponible).

    python -m bench.evaluate      (nécessite scikit-image et scikit-learn)

Note : la vérité terrain n'a pas d'eau ; « garder le bleu au loin » est donc
pénalisé par cette mesure alors qu'il donne un rendu plus naturel.
"""
from __future__ import annotations
import os
import sys
import cv2
import numpy as np
from aquimage import core, autotune
from bench.groundtruth import cases, delta_e

AQUALIB = os.path.join(os.path.dirname(__file__), "..", "..", "AQUALIB")


def v1(I_rgb):
    """AquaCol 1.0 (dossier voisin AQUALIB), pour comparaison."""
    sys.path.insert(0, AQUALIB)
    import underwater_enhancement as ue
    r = ue.enhance_underwater_image(cv2.cvtColor(I_rgb, cv2.COLOR_RGB2BGR))
    return cv2.cvtColor(r["enhanced"], cv2.COLOR_BGR2RGB)

def v3_auto(I):
    s, _ = autotune.auto_settings(I)
    return core.enhance(I, s)

if __name__ == "__main__":
    M = {"entree": lambda I: I, "aquimage_auto": v3_auto,
         "aquimage_sans_bleu_garde": lambda I: core.enhance(I, autotune.auto_settings(I)[0].copy(water_keep=0))}
    if os.path.exists(os.path.join(AQUALIB, "underwater_enhancement.py")):
        M["aquacol_v1"] = v1
    R = {k: [] for k in M}
    for c in cases():
        for k, f in M.items():
            R[k].append(delta_e(f(c["I"]), c["J"]))
    print("ΔE2000 moyen (24 scènes) :", {k: round(float(np.mean(v)), 2) for k, v in R.items()})
