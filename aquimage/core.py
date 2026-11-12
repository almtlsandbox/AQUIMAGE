"""
AquImage — moteur de correction des images sous-marines.

Principe : *estimer petit, appliquer en grand*.  Tous les paramètres sont
estimés sur une copie réduite (~0,3 Mpx) puis appliqués à n'importe quelle
résolution, par bandes de lignes.  L'aperçu et l'export utilisent exactement
le même plan : ce que l'on voit est ce que l'on obtient.

Chaîne de traitement (validée sur photos réelles et scènes simulées) :

1. Compensation des canaux affaiblis (Ancuti et al. 2018) :
   R' = R + α (Ḡ − R̄)(1 − R) G   — le rouge est reconstruit là où le vert
   porte du signal, sans amplifier le bruit des zones sombres.  Même chose
   pour le bleu en eau verte.
2. Balance des blancs « gray-world » robuste en lumière linéaire.  Une fois
   la dominante retirée, le voile de l'eau devient une brume presque neutre.
3. Retrait du voile par le prior du canal sombre (He et al. 2011) sur cette
   brume neutre : lumière de l'eau trouvée par quadtree (zone voilée ET
   uniforme), transmission affinée par filtre guidé.
4. Gain de couleur spatialement variable : correction pleine sur les sujets
   proches, partielle sur l'eau lointaine (``water_keep``) pour garder un
   fond bleu naturel au lieu d'un gris délavé ou magenta.
5. Tonalité : niveaux de luminance, gamma automatique des tons moyens,
   hautes lumières écrêtées ramenées vers le blanc (leur couleur est perdue),
   température/teinte, saturation, clarté (contraste local guidé, sans halo).

Toutes les tailles de fenêtre sont des fractions de la taille de l'image.
"""
from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, fields
from typing import Dict, Optional, Tuple

import cv2
import numpy as np

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

ANALYSIS_MAX_SIDE = 640          # côté max de la copie d'analyse
STRIP_PIXELS = 100_000           # pixels par bande : petites, elles tiennent dans le cache
MAX_WORKERS = max(1, min(8, os.cpu_count() or 1))   # bandes traitées en parallèle
LUMA = np.array([0.2126, 0.7152, 0.0722], np.float32)    # linéaire Rec.709
LUMA_E = np.array([0.299, 0.587, 0.114], np.float32)     # encodé (Rec.601)
PRE_PCT = 99.5                   # centile des hautes lumières tenu sous l'épaule
HAZE_FLOOR0, HAZE_FLOOR_SPAN, HAZE_BLACK = 0.08, 0.12, 0.85   # plancher laiteux
MID_DROP = 0.08                  # assombrissement toléré des tons moyens
MID_TARGET = 0.42                # luminance médiane visée par l'auto-gamma


# ---------------------------------------------------------------------------
# Réglages (tous indépendants de la résolution)
# ---------------------------------------------------------------------------

@dataclass
class Settings:
    """Réglages d'une correction.  Les valeurs par défaut conviennent à une
    photo sous-marine typique ; ``autotune.auto_settings`` les adapte à
    chaque image."""

    red_comp: float = 1.0      # compensation du rouge / bleu (Ancuti), 0..2
    color: float = 1.0         # balance des blancs automatique, 0..1
    max_gain: float = 4.0      # gain max par canal de la balance des blancs
    water_keep: float = 0.4    # part de la teinte de l'eau gardée au loin
    strength: float = 0.6      # retrait du voile (0 = aucun, 1 = total)
    omega: float = 0.85        # part du voile prise en compte dans t
    t_min: float = 0.2         # transmission minimale (limite le bruit)
    temperature: float = 0.0   # -1 (froid) .. +1 (chaud)
    tint: float = 0.0          # -1 (vert) .. +1 (magenta)
    exposure: float = 0.0      # luminosité, en IL approximatifs
    contrast: float = 0.25     # clarté / contraste local, 0..1
    saturation: float = 1.0    # 0..2
    vibrance: float = 0.3      # renforce surtout les couleurs ternes, 0..1
    shadows: float = 0.15      # éclaircit les ombres, 0..1
    black_clip: float = 0.05   # % de pixels au noir
    black_level: float = 0.5   # part du voile de noir retirée (1 = noir pur)
    white_clip: float = 0.2    # % de pixels au blanc (avant l'épaule)
    stretch: float = 2.5       # étirement maximal des niveaux
    auto_tone: float = 1.0     # part de la luminosité automatique (0 = aucune)
    denoise: float = 0.0       # réduction du bruit (eau, zones lisses, ombres), 0..1

    LIMITS = {
        "red_comp": (0.0, 2.0), "color": (0.0, 1.0), "max_gain": (1.0, 12.0),
        "water_keep": (0.0, 1.0), "strength": (0.0, 1.0),
        "omega": (0.5, 0.98), "t_min": (0.05, 0.6),
        "temperature": (-1.0, 1.0), "tint": (-1.0, 1.0),
        "exposure": (-2.0, 2.0), "contrast": (0.0, 1.0),
        "saturation": (0.0, 2.0), "black_clip": (0.0, 5.0),
        "white_clip": (0.0, 5.0), "stretch": (1.0, 4.0),
        "vibrance": (0.0, 1.0), "shadows": (0.0, 1.0), "black_level": (0.0, 1.0),
        "auto_tone": (0.0, 1.0), "denoise": (0.0, 1.0),
    }

    def clamp(self) -> "Settings":
        for k, (lo, hi) in self.LIMITS.items():
            setattr(self, k, float(min(hi, max(lo, float(getattr(self, k))))))
        return self

    def to_dict(self) -> Dict[str, float]:
        return {k: round(float(v), 4) for k, v in asdict(self).items()}

    def copy(self, **changes) -> "Settings":
        d = self.to_dict()
        d.update(changes)
        return Settings.from_dict(d)

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "Settings":
        s = cls()
        if d:
            names = {f.name for f in fields(cls)}
            for k, v in d.items():
                if k in names and v is not None:
                    setattr(s, k, float(v))
        return s.clamp()


# ---------------------------------------------------------------------------
# Conversions sRGB <-> linéaire (tables de correspondance)
# ---------------------------------------------------------------------------

def _srgb_to_lin_f(x):
    x = np.asarray(x, np.float64)
    return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)


def _lin_to_srgb_f(x):
    x = np.clip(np.asarray(x, np.float64), 0.0, 1.0)
    return np.where(x <= 0.0031308, x * 12.92, 1.055 * x ** (1 / 2.4) - 0.055)


_N = 65536
_DEC8 = _srgb_to_lin_f(np.arange(256) / 255.0).astype(np.float32)
_DEC16 = _srgb_to_lin_f(np.arange(_N) / (_N - 1.0)).astype(np.float32)
_ENC = _lin_to_srgb_f(np.arange(_N) / (_N - 1.0)).astype(np.float32)


def to_float(img: np.ndarray) -> np.ndarray:
    """uint8/uint16/float -> float32 [0,1] (valeurs encodées sRGB)."""
    if img.dtype == np.uint8:
        return img.astype(np.float32) * (1.0 / 255.0)
    if img.dtype == np.uint16:
        return img.astype(np.float32) * (1.0 / 65535.0)
    return np.clip(img.astype(np.float32), 0.0, 1.0)


def decode_linear(img: np.ndarray) -> np.ndarray:
    """Valeurs encodées (uint8/uint16/float 0..1) -> lumière linéaire."""
    if img.dtype == np.uint8:
        return _DEC8[img]
    if img.dtype == np.uint16:
        return _DEC16[img]
    idx = np.clip(img, 0.0, 1.0) * (_N - 1) + 0.5
    return _DEC16[idx.astype(np.uint16)]


def encode_srgb(lin: np.ndarray) -> np.ndarray:
    """Lumière linéaire -> valeurs encodées sRGB float32 [0,1]."""
    idx = np.clip(lin, 0.0, 1.0) * (_N - 1) + 0.5
    return _ENC[idx.astype(np.uint16)]


def quantize(enc: np.ndarray, dtype) -> np.ndarray:
    """Float [0,1] -> uint8/uint16 avec arrondi."""
    if dtype == np.uint16:
        return (np.clip(enc, 0, 1) * 65535.0 + 0.5).astype(np.uint16)
    return (np.clip(enc, 0, 1) * 255.0 + 0.5).astype(np.uint8)


# ---------------------------------------------------------------------------
# Outils
# ---------------------------------------------------------------------------

def _max3(x: np.ndarray) -> np.ndarray:
    """Maximum des 3 canaux (bien plus rapide que x.max(axis=2))."""
    return np.maximum(np.maximum(x[..., 0], x[..., 1]), x[..., 2])


def _min3(x: np.ndarray) -> np.ndarray:
    return np.minimum(np.minimum(x[..., 0], x[..., 1]), x[..., 2])


def _odd(n: float, lo: int = 3) -> int:
    n = max(lo, int(round(n)))
    return n if n % 2 else n + 1


def rotate90(img: np.ndarray, quarter: int) -> np.ndarray:
    """Rotation de *quarter* quarts de tour dans le sens horaire (0 à 3)."""
    q = int(quarter) % 4
    return img if q == 0 else np.ascontiguousarray(np.rot90(img, -q))


def resize_max(img: np.ndarray, max_side: int) -> np.ndarray:
    """Réduit l'image pour que son plus grand côté soit <= max_side."""
    h, w = img.shape[:2]
    s = max(h, w)
    if s <= max_side:
        return img
    f = max_side / float(s)
    return cv2.resize(img, (max(1, int(round(w * f))), max(1, int(round(h * f)))),
                      interpolation=cv2.INTER_AREA)


def guide_of(enc: np.ndarray) -> np.ndarray:
    """Luminance des valeurs encodées (image guide), float32 2D."""
    return (enc[..., 0] * 0.299 + enc[..., 1] * 0.587
            + enc[..., 2] * 0.114).astype(np.float32)


def _box(x: np.ndarray, r: int) -> np.ndarray:
    return cv2.boxFilter(x, -1, (2 * r + 1, 2 * r + 1),
                         borderType=cv2.BORDER_REFLECT)


def guided_coeffs(I: np.ndarray, p: np.ndarray, r: int, eps: float
                  ) -> Tuple[np.ndarray, np.ndarray]:
    """Coefficients (a, b) moyennés du filtre guidé (He et al. 2013).

    q = a·I + b.  Les coefficients sont lisses : on peut les agrandir et les
    appliquer au guide pleine résolution (fast guided filter, He & Sun 2015).
    """
    I = I.astype(np.float32)
    p = p.astype(np.float32)
    mI, mp = _box(I, r), _box(p, r)
    cov = _box(I * p, r) - mI * mp
    var = _box(I * I, r) - mI * mI
    a = cov / (var + eps)
    b = mp - a * mI
    return _box(a, r), _box(b, r)


def upsample_rows(small: np.ndarray, full_hw: Tuple[int, int],
                  y0: int, y1: int) -> np.ndarray:
    """Agrandissement bilinéaire de *small* vers *full_hw*, lignes y0..y1
    seulement (même convention que cv2.resize)."""
    H, W = full_hw
    sh, sw = small.shape[:2]
    xs = (np.arange(W, dtype=np.float32) + 0.5) * (sw / W) - 0.5
    ys = (np.arange(y0, y1, dtype=np.float32) + 0.5) * (sh / H) - 0.5
    mapx = np.ascontiguousarray(np.broadcast_to(xs[None, :], (y1 - y0, W)))
    mapy = np.ascontiguousarray(np.broadcast_to(ys[:, None], (y1 - y0, W)))
    return cv2.remap(small, mapx, mapy, cv2.INTER_LINEAR,
                     borderMode=cv2.BORDER_REPLICATE)


def estimate_noise(gray: np.ndarray) -> float:
    """Écart-type du bruit (Immerkær 1996), image float 0..1."""
    k = np.array([[1, -2, 1], [-2, 4, -2], [1, -2, 1]], np.float32)
    conv = cv2.filter2D(gray.astype(np.float32), -1, k,
                        borderType=cv2.BORDER_REFLECT)
    h, w = gray.shape
    return float(np.sqrt(np.pi / 2.0) / (6.0 * (w - 2) * (h - 2))
                 * np.abs(conv[1:-1, 1:-1]).sum())


def water_quadtree(img: np.ndarray, prior: np.ndarray, gray: np.ndarray,
                   k: float = 2.0, min_frac: float = 1 / 256
                   ) -> Tuple[np.ndarray, float]:
    """Lumière de l'eau par subdivision en quadrants (Kim et al. 2013).

    À chaque niveau on garde le quadrant qui maximise
    moyenne(prior) − k · écart-type(luminance) : une zone voilée ET
    uniforme, donc de l'eau et non un objet clair.  Retourne (couleur,
    écart-type de la zone).
    """
    h, w = prior.shape
    y0, y1, x0, x1 = 0, h, 0, w
    while ((y1 - y0) * (x1 - x0) > 4 * min_frac * h * w
           and (y1 - y0) > 8 and (x1 - x0) > 8):
        ym, xm = (y0 + y1) // 2, (x0 + x1) // 2
        best_s, best = -1e9, (y0, y1, x0, x1)
        for q in ((y0, ym, x0, xm), (y0, ym, xm, x1),
                  (ym, y1, x0, xm), (ym, y1, xm, x1)):
            s = float(prior[q[0]:q[1], q[2]:q[3]].mean()
                      - k * gray[q[0]:q[1], q[2]:q[3]].std())
            if s > best_s:
                best_s, best = s, q
        y0, y1, x0, x1 = best
    reg = img[y0:y1, x0:x1].reshape(-1, 3)
    pr = prior[y0:y1, x0:x1].ravel()
    sel = reg[pr >= np.percentile(pr, 50)]
    col = np.clip(np.median(sel, axis=0), 1e-3, 1.0).astype(np.float32)
    return col, float(gray[y0:y1, x0:x1].std())


# ---------------------------------------------------------------------------
# Étapes élémentaires (identiques sur la copie réduite et à pleine taille)
# ---------------------------------------------------------------------------

SIG_Y0, SIG_Y1 = 0.002, 0.03   # luminance linéaire : aucun signal -> signal suffisant


def _smooth01(x: np.ndarray) -> np.ndarray:
    """Transition douce 3x² − 2x³ de x borné à [0, 1]."""
    x = np.clip(x, 0.0, 1.0)
    return (x * x * (3.0 - 2.0 * x)).astype(np.float32)


def signal_weight(y_lin: np.ndarray) -> np.ndarray:
    """0 là où l'original n'a quasiment pas de lumière (le rouge n'y est que
    du bruit), 1 dès qu'il y a assez de signal pour corriger la couleur."""
    x = np.clip((y_lin - SIG_Y0) / (SIG_Y1 - SIG_Y0), 0.0, 1.0)
    return (x * x * (3.0 - 2.0 * x)).astype(np.float32)


def _compensate(enc: np.ndarray, comp: np.ndarray,
                weight: Optional[np.ndarray] = None,
                sig: Optional[np.ndarray] = None) -> np.ndarray:
    """Compensation d'Ancuti.  comp = (k_R, k_B) = α·(Ḡ − R̄), α·(Ḡ − B̄).

    *weight* (carte 0..1) module la compensation : pleine sur les sujets
    proches, réduite dans l'eau lointaine (sinon halo jaune au soleil).
    """
    out = enc.copy()
    if weight is None:
        G = enc[..., 1]
    else:
        # Pas de rouge inventé dans le quasi-noir (sinon noirs bordeaux)
        if sig is None:
            sig = signal_weight(decode_linear(enc) @ LUMA)
        G = enc[..., 1] * weight * sig
    if comp[0] > 0:
        R = enc[..., 0]
        out[..., 0] = R + comp[0] * (1.0 - R) * G
    if comp[1] > 0:
        B = enc[..., 2]
        out[..., 2] = B + comp[1] * (1.0 - B) * G
    return np.clip(out, 0.0, 1.0)


CHROMA_T0, CHROMA_T1 = 0.25, 0.60   # couleur restaurée : nulle au loin, pleine de près
TEX_SIGMA = 0.03         # étalement de la texture (petit : pas de halo autour des sujets)
TCOL_SIGMA = 0.10        # lissage de la transmission « couleur » (part du petit côté)


def smooth_t(t: np.ndarray) -> np.ndarray:
    """Transmission lissée à grande échelle, pour moduler la couleur."""
    sig = max(2.0, TCOL_SIGMA * min(t.shape[:2]))
    return np.clip(cv2.GaussianBlur(t.astype(np.float32), (0, 0), sig,
                                    borderType=cv2.BORDER_REFLECT), 0.0, 1.0)


WATER_RG = 0.87          # eau lointaine : rouge ≤ 0,87 × vert (sinon indigo)
FAR_T0, FAR_T1 = 0.35, 0.80   # eau lointaine (t ≤ T0) … sujets proches (t ≥ T1)


def far_weight(t: np.ndarray) -> np.ndarray:
    """0 sur les sujets proches, 1 dans l'eau lointaine (transition douce)."""
    x = np.clip((FAR_T1 - t) / (FAR_T1 - FAR_T0), 0.0, 1.0)
    return (x * x * (3.0 - 2.0 * x)).astype(np.float32)


TEX_LO, TEX_HI = 0.05, 0.09   # texture fine : eau lisse … sujet (sable, récif)


def fine_texture(guide: np.ndarray) -> np.ndarray:
    """Texture fine (laplacien moyen) : l'eau, même près de la surface, reste
    sous 0,05 ; le sable (grain) et le récif sont au-dessus."""
    r = max(3, int(round(0.014 * min(guide.shape))))
    return cv2.blur(np.abs(cv2.Laplacian(guide, cv2.CV_32F, ksize=3)), (r, r))


OPEN_WATER_MIN = 0.035    # part d'eau lisse et lointaine à partir de laquelle on la préserve
OPEN_WATER_LO, OPEN_WATER_HI = 0.032, 0.040   # … progressivement, entre ces deux parts


def ramp(x: float, lo: float, hi: float) -> float:
    """0 sous *lo*, 1 au-dessus de *hi*, transition douce entre les deux."""
    t = min(1.0, max(0.0, (float(x) - lo) / (hi - lo)))
    return t * t * (3.0 - 2.0 * t)


def open_water_share(t: np.ndarray, guide: np.ndarray) -> float:
    """Part de l'image faite d'eau libre : lointaine ET lisse.  Un fond de
    sable clair, que le prior du canal sombre croit lointain, n'en est pas."""
    return float(((far_weight(smooth_t(t)) > 0.7)
                  & (fine_texture(guide) < 0.04)).mean())


WATER_CHROMA = 0.08       # distance de chromaticité « même teinte que l'eau »
WATER_AREA = 0.03         # une zone d'eau d'un seul tenant fait au moins 3 % de l'image
WATER_DARK = 0.25         # l'eau du bord fait au moins 25 % de la luminance de l'eau libre
WATER_RED = 0.12          # eau bleue : rouge linéaire < 12 % de (vert + bleu)


NORED_R0, NORED_R1 = 0.04, 0.10   # rouge/(vert+bleu) de l'original : nul … informatif
RED_OVER_GREEN = 1.12             # rouge reconstruit ≤ 1,12 × vert (sable beige)


def no_red_weight(enc0: Optional[np.ndarray],
                  lin: Optional[np.ndarray] = None) -> np.ndarray:
    """1 là où l'original n'a quasiment pas de rouge : tout le rouge y est
    reconstruit, il ne doit pas dépasser celui d'un sable beige.
    *lin* : original déjà décodé en linéaire (évite de le refaire)."""
    if lin is None:
        lin = decode_linear(enc0)
    ratio = lin[..., 0] / np.maximum(lin[..., 1] + lin[..., 2], 1e-4)
    x = np.clip((NORED_R1 - ratio) / (NORED_R1 - NORED_R0), 0.0, 1.0)
    return (x * x * (3.0 - 2.0 * x)).astype(np.float32)


WATER_RG_CAP = 0.85       # eau bleue lisse : rouge ≤ 0,85 × vert (linéaire)
SMOOTH_MAGENTA = 0.8      # part du magenta retirée dans les zones lisses (eau, voile)


def red_cap_weight(enc0: np.ndarray, guide: np.ndarray) -> np.ndarray:
    """Poids du plafond de magenta : 1 sur les zones lisses dont l'original
    n'a pas de rouge ; SMOOTH_MAGENTA sur toute zone lisse (eau voilée,
    dégradé) ; 0 sur les sujets texturés.  Les sujets magenta réels (coraux
    mous, éponges) sont texturés."""
    lin = decode_linear(enc0)
    red = lin[..., 0] / np.maximum(lin[..., 1] + lin[..., 2], 1e-4)
    fine = fine_texture(guide)
    ko = _odd(0.03 * min(fine.shape[:2]))
    fine = cv2.morphologyEx(fine, cv2.MORPH_OPEN,
                            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (ko, ko)))
    smooth = np.clip((TEX_LO - fine) / (0.5 * TEX_LO), 0.0, 1.0)
    nored = np.clip((WATER_RED - red) / (0.5 * WATER_RED), 0.0, 1.0)
    w = smooth * np.maximum(nored, SMOOTH_MAGENTA)
    sig = max(1.0, 0.01 * min(w.shape[:2]))
    return cv2.GaussianBlur(w.astype(np.float32), (0, 0), sig,
                            borderType=cv2.BORDER_REFLECT)


def color_t(t: np.ndarray, guide: np.ndarray,
            enc0: Optional[np.ndarray] = None,
            water: Optional[np.ndarray] = None,
            return_mask: bool = False):
    """Transmission « couleur » : t lissée, mais les zones texturées (récif,
    épave) comptent comme proches même si elles sont loin, et l'eau, lisse,
    compte comme lointaine même si le prior du canal sombre la croit proche
    (eau bleue laiteuse)."""
    fine = fine_texture(guide)
    # Poissons, bulles, particules : petits détails isolés sur l'eau.
    # L'ouverture morphologique les efface ; un récif ou une épave résiste.
    ko = _odd(0.03 * min(fine.shape[:2]))
    fine_o = cv2.morphologyEx(fine, cv2.MORPH_OPEN,
                              cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (ko, ko)))
    tex = np.clip((fine_o - TEX_LO) / (TEX_HI - TEX_LO), 0.0, 1.0).astype(np.float32)
    # Un contour isolé (silhouette sur l'eau) n'est pas une zone texturée
    k = _odd(0.05 * min(tex.shape[:2]))
    tex = cv2.erode(tex, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    sig = max(2.0, TEX_SIGMA * min(tex.shape[:2]))
    tex = cv2.GaussianBlur(tex.astype(np.float32), (0, 0), sig,
                           borderType=cv2.BORDER_REFLECT)
    tc = smooth_t(t)
    tc = tc + (1.0 - tc) * tex
    if enc0 is None:
        return (tc.astype(np.float32), None) if return_mask else tc.astype(np.float32)
    lin = decode_linear(enc0)
    y = lin @ LUMA
    red = lin[..., 0] / np.maximum(lin[..., 1] + lin[..., 2], 1e-4)
    colorlike = red < WATER_RED
    if water is not None:
        # Même teinte que l'eau estimée (bleue ou verte)
        c = lin / np.maximum(lin.sum(axis=-1, keepdims=True), 1e-4)
        cw = water / max(float(water.sum()), 1e-4)
        colorlike = colorlike | (np.sqrt(((c - cw) ** 2).sum(axis=-1)) < WATER_CHROMA)
    smooth = fine_o < TEX_LO
    base = smooth & colorlike
    y_w = float(np.median(y[base])) if base.mean() > 0.01 else 0.0
    rel = y / max(y_w, 1e-6)
    # Pas nettement plus sombre que l'eau (combinaison, roche en silhouette)
    cand = (base & (rel > WATER_DARK)).astype(np.uint8)
    # Eau = grandes zones d'un seul tenant, ou reliées à l'eau lointaine
    n, lab, st, _ = cv2.connectedComponentsWithStats(cand, connectivity=8)
    big = np.zeros(n, bool)
    big[1:] = st[1:, cv2.CC_STAT_AREA] >= WATER_AREA * cand.size
    far = far_weight(tc) > 0.7
    if n > 1:
        touched = np.unique(lab[far & (cand > 0)])
        big[touched[touched > 0]] = True
    mask = big[lab] & (cand > 0)
    wl = mask.astype(np.float32) * np.clip(
        (rel - WATER_DARK) / WATER_DARK, 0.0, 1.0).astype(np.float32)
    sig = max(1.0, 0.04 * min(wl.shape[:2]))
    wl = cv2.GaussianBlur(wl, (0, 0), sig, borderType=cv2.BORDER_REFLECT)
    tc = tc + (np.minimum(tc, FAR_T0) - tc) * wl
    if return_mask:
        return tc.astype(np.float32), np.clip(wl, 0.0, 1.0).astype(np.float32)
    return tc.astype(np.float32)


def _near_weight(t: np.ndarray, keep: float) -> np.ndarray:
    """1 sur les sujets proches, → 1 − keep dans l'eau lointaine."""
    return (1.0 - keep * far_weight(t)).astype(np.float32)


def choose_keep(enc0: np.ndarray, t: np.ndarray, comp: np.ndarray,
                water: np.ndarray, gains: np.ndarray, s: "Settings",
                lo: float = 0.4, hi: float = 0.9, max_bad: float = 0.01
                ) -> float:
    """Part de teinte d'eau gardée au loin.

    Teste les pixels lointains (t < 0,55) : on retient la plus petite valeur
    pour laquelle moins de *max_bad* d'entre eux virent au rose/violet/gris
    rosé (rouge > vert ou rouge > bleu après correction).  Si l'« eau »
    n'est pas bleue ou verte (filtre rouge, sable éclairé…), rien n'est
    gardé : correction pleine partout.
    """
    if water[0] * gains[0] >= 0.8 * min(water[1] * gains[1], water[2] * gains[2]) \
            and water[0] >= 0.8 * min(water[1], water[2]):
        return 0.0
    far = t < 0.7
    if far.mean() < 0.02:
        return lo
    e = enc0[far]
    tf = t[far]
    if len(e) > 20000:
        idx = np.linspace(0, len(e) - 1, 20000).astype(int)
        e, tf = e[idx], tf[idx]
    e, tf = e[None], tf[None]
    for keep in np.linspace(lo, hi, 11):
        lin = decode_linear(_compensate(e, comp, _near_weight(tf, keep)))
        c = _restore(lin, tf, water, gains, float(keep), s, tf)[0]
        # Rose, violet ou indigo : le rouge rattrape le vert (l'eau azur
        # garde nettement plus de vert que de rouge) ou dépasse le bleu.
        bad = (c[:, 0] > c[:, 1] * WATER_RG) | (c[:, 0] > c[:, 2] * 1.02)
        if bad.mean() < max_bad:
            return float(keep)
    # Aucune valeur ne garde ces zones bleues : ce sont surtout des sujets
    # lointains (épave, récif) -> compromis.
    return float(0.5 * (lo + hi))


WATER_TARGET = (25, 110, 205)   # bleu azur profond visé pour l'eau lointaine (sRGB)
WATER_TONE = 0.6                # part du chemin vers cette teinte


def water_tone(rec: np.ndarray, tc: np.ndarray, keep: float) -> np.ndarray:
    """Facteurs par canal qui orientent l'eau lointaine bleue ou cyan vers un
    bleu azur profond (l'appareil l'enregistre souvent trop cyan, et la
    correction la grise).  Luminance conservée ; eau verte non touchée."""
    one = np.ones(3, np.float32)
    if keep <= 0:
        return one
    m = far_weight(tc) > 0.7
    if m.mean() < 0.03:
        return one
    wc = np.maximum(rec[m].mean(axis=0), 1e-5)
    if not (wc[2] > wc[1] > wc[0]):          # eau bleue ou cyan seulement
        return one
    tgt = decode_linear(np.array(WATER_TARGET, np.float32) / 255.0)
    # Eau déjà aussi bleue que la cible (rapport bleu/vert) : rien à faire,
    # sinon elle passerait au violet.
    if wc[2] / wc[1] >= tgt[2] / tgt[1]:
        return one
    tgt = tgt * float(wc @ LUMA) / float(tgt @ LUMA)
    # On retire du rouge et du vert, on ajoute du bleu ; jamais l'inverse
    # (sinon l'eau vire au violet).
    f = np.clip(tgt / wc, 0.5, 2.0) ** WATER_TONE
    f[0], f[1] = min(f[0], 1.0), min(f[1], 1.0)
    f[2] = max(f[2], 1.0)
    return f.astype(np.float32)


def _planes(x: np.ndarray):
    """(H, W, 3) -> trois plans contigus.  Les calculs plan par plan sont
    bien plus rapides que la diffusion sur un dernier axe de longueur 3."""
    return [np.array(x[..., c], order="C") for c in range(3)]


def _stack(p) -> np.ndarray:
    return np.stack(p, axis=-1)


def _dot(p, w) -> np.ndarray:
    """Somme pondérée de trois plans (luminance)."""
    y = p[0] * w[0]
    y += p[1] * w[1]
    y += p[2] * w[2]
    return y


def _restore(lin: np.ndarray, t: np.ndarray, water: np.ndarray,
             gains: np.ndarray, keep: float, s: Settings,
             tc: Optional[np.ndarray] = None,
             wtone: Optional[np.ndarray] = None,
             capw: Optional[np.ndarray] = None,
             nored: Optional[np.ndarray] = None,
             cast: Optional[np.ndarray] = None) -> np.ndarray:
    """Retrait du voile + gains spatialement variables.

    W(x) = G(x) · [lin + s·((lin − A)/max(t, t_min) + A − lin)]
    G(x) = g ** (1 − keep·(1 − t(x))²)   (correction partielle, eau lointaine seulement)
    """
    return _stack(_restore_p(_planes(lin), t, water, gains, keep, s, tc, wtone,
                             capw, nored, cast=cast))


def _restore_p(lin, t, water, gains, keep, s, tc=None, wtone=None, capw=None,
               nored=None, y_lin=None, cast=None):
    """``_restore`` sur trois plans (liste R, G, B) ; retourne trois plans."""
    tt = np.maximum(t, np.float32(s.t_min))
    omt = 1.0 - tt
    # Voile à retirer V = A·(1 − t), borné pixel par pixel : jamais plus de
    # 90 % d'un canal, avec un facteur commun aux 3 canaux (sinon les ombres
    # perdent leur vert et virent au violet).
    f = None
    for c in range(3):
        q = 0.9 * lin[c] / np.maximum(water[c] * omt, 1e-6)
        f = q if f is None else np.minimum(f, q, out=f)
    np.clip(f, 0.0, 1.0, out=f)
    f *= omt                               # = 1 − te
    te = 1.0 - f
    k = np.float32(s.strength)
    rec = [lin[c] + k * ((lin[c] - water[c] * f) / te - lin[c]) for c in range(3)]
    # Au loin (t faible), le retrait du voile divise les écarts de teinte par
    # t : plancton, bulles et bruit deviennent des taches roses ou vertes.
    # On n'y restaure que la luminance ; la couleur complète ne revient que
    # sur les sujets proches.
    tg = t if tc is None else tc
    wcol = np.clip((tg - CHROMA_T0) / (CHROMA_T1 - CHROMA_T0), 0.0, 1.0)
    if y_lin is None:
        y_lin = _dot(lin, LUMA)
    if (wcol < 1.0).any():
        y_rec = _dot([np.maximum(r, 0.0) for r in rec], LUMA)
        ratio = y_rec / np.maximum(y_lin, 1e-6)
        for c in range(3):
            lum = lin[c] * ratio
            rec[c] -= lum
            rec[c] *= wcol
            rec[c] += lum
    fw = far_weight(tg) if keep > 0 else None
    expo = (1.0 - keep * fw) if keep > 0 else None
    # Sans signal, on n'amplifie pas (le rouge n'y est que du bruit) ; les
    # réductions de canal, elles, restent appliquées (sinon teinte lavande).
    lg = np.log(gains)
    sig = None
    out = []
    for c in range(3):
        if lg[c] > 0:
            if sig is None:
                sig = signal_weight(np.maximum(y_lin, 0.0))
            e = lg[c] * sig
            if expo is not None:
                e *= expo
        else:
            e = lg[c] * expo if expo is not None else None
        if wtone is not None and keep > 0:
            # Concentré sur l'eau franchement lointaine (pas le récif à mi-distance)
            w2 = float(np.log(wtone[c])) * (keep * fw ** 2)
            e = w2 if e is None else e + w2
        o = np.maximum(rec[c], 0.0)
        if e is None:
            o *= gains[c]
        else:
            o *= np.exp(e)
        out.append(o)
    if nored is not None:
        # Rouge entièrement reconstruit : pas plus qu'un sable beige (sinon
        # taches orange au soleil, rouge-violet dans l'eau).
        over = np.maximum(out[0] - RED_OVER_GREEN * out[1], 0.0)
        if over.any():
            over *= nored
            out[0] -= over
    if capw is not None:
        # Zones lisses sans rouge dans l'original (eau, même sombre ou au
        # bord d'un récif) : le rouge ajouté n'y porte aucune information.
        # S'il fait passer le pixel au violet (rouge ET bleu au-dessus du
        # vert), on retire cet excès de rouge.
        # Eau bleue lisse : rouge un peu sous le vert (sinon lavande)
        lim = out[1] * (1.0 - (1.0 - WATER_RG_CAP) * (out[2] > out[1]))
        m = np.maximum(np.minimum(out[0], np.maximum(out[2], lim)) - lim, 0.0)
        if m.any():
            m *= capw
            out[0] -= m
    if cast is not None:
        # Dominante locale des surfaces neutres (rose, jaune), luminance gardée
        y0 = _dot(out, LUMA)
        out[0] *= np.exp(cast[..., 0])
        out[2] *= np.exp(cast[..., 1])
        y0 /= np.maximum(_dot(out, LUMA), 1e-6)
        for c in range(3):
            out[c] *= y0
    return out


def _clip_weight(enc0: np.ndarray, k: float = 1.0) -> np.ndarray:
    """Poids 0..0,6 des pixels réellement écrêtés dans l'original."""
    return (0.6 * k * np.clip((_max3(enc0) - 0.97) / 0.025, 0.0, 1.0)).astype(np.float32)


def clip_dose(s: Settings) -> float:
    """Les hautes lumières écrêtées ne sont blanchies que si la couleur est
    corrigée (leur teinte est alors fausse) ; sinon un coucher de soleil
    perdrait son disque orange."""
    return float(min(1.0, (s.color + s.red_comp) / 0.3))


GAMMA_CHROMA = 0.6     # part du gamma appliquée à la luminance seule (couleurs gardées)
FULL_LIFT_KEEP = 0.25  # photo déjà pleine : part de l'éclaircissement auto conservée


def tonal_fullness(enc0: np.ndarray) -> float:
    """0..1 : l'original a-t-il déjà de vrais noirs ET de vraies hautes
    lumières ?  (photo au flash, fond noir volontaire, scène bien exposée).
    Une telle photo n'a pas besoin d'être éclaircie ni débouchée."""
    y = enc0.reshape(-1, 3) @ LUMA_E
    lo, hi = np.percentile(y, [1.0, 99.0])
    return float(np.clip((hi - 0.62) / 0.2, 0.0, 1.0)
                 * np.clip((0.10 - lo) / 0.07, 0.0, 1.0))


def _post(enc: np.ndarray, lo: float, slope: float, gamma: float,
          tgain: np.ndarray, sat: float,
          clipw: Optional[np.ndarray] = None,
          shadows: float = 0.0, vibrance: float = 0.0,
          nored: Optional[np.ndarray] = None) -> np.ndarray:
    return _stack(_post_p(_planes(enc), lo, slope, gamma, tgain, sat, clipw,
                          shadows, vibrance, nored))


def _max3p(x):
    return np.maximum(np.maximum(x[0], x[1]), x[2])


def _min3p(x):
    return np.minimum(np.minimum(x[0], x[1]), x[2])


def _post_p(enc, lo, slope, gamma, tgain, sat, clipw=None, shadows=0.0,
            vibrance=0.0, nored=None):
    """Niveaux, gamma, ombres, blancs écrêtés, température, saturation, sur
    trois plans (modifiés sur place)."""
    lo, slope = np.float32(lo), np.float32(slope)
    x = []
    for c in range(3):
        v = enc[c] - lo
        v *= slope
        np.maximum(v, 0.0, out=v)
        x.append(v)
    if abs(gamma - 1.0) > 1e-3:
        # Gamma en partie sur la luminance seule : un gamma par canal pâlit
        # les couleurs quand il éclaircit (rapports entre canaux écrasés).
        Y = np.maximum(_dot(x, LUMA_E), 1e-5)
        r = np.power(Y, gamma)
        r /= Y
        # ... sauf là où le rouge est entièrement reconstruit : le gamma par
        # canal y garde discrète une couleur qui n'existe pas dans l'original.
        k = np.float32(GAMMA_CHROMA) if nored is None else GAMMA_CHROMA * (1.0 - nored)
        for c in range(3):
            xc = np.power(x[c], gamma)
            xl = x[c] * r
            xl -= xc
            xl *= k
            xc += xl
            x[c] = xc
    if shadows > 1e-3:
        # Éclaircit les ombres (maximum vers L = 1/3), sans toucher aux blancs
        L = np.clip(_dot(x, LUMA_E), 1e-4, 1.0)
        r = 1.0 + (1.2 * shadows) * (1.0 - L) ** 2
        for c in range(3):
            x[c] *= r
    if clipw is not None and clipw.any():
        # Hautes lumières écrêtées dans l'original : couleur perdue -> blanc
        m = _max3p(x)
        for c in range(3):
            x[c] += clipw * (m - x[c])
    for c in range(3):
        if tgain[c] != 1.0:
            x[c] *= tgain[c]
    if abs(sat - 1.0) > 1e-3 or vibrance > 1e-3:
        y = _dot(x, LUMA_E)
        k = np.float32(sat)
        if vibrance > 1e-3:
            # Vibrance : gain fort sur les couleurs ternes, nul sur les vives
            mx = _max3p(x)
            ch = (mx - _min3p(x)) / np.maximum(mx, 1e-3)
            k = sat * (1.0 + (vibrance * 0.6) * np.clip(1.0 - 2.0 * ch, 0.0, 1.0))
        for c in range(3):
            x[c] -= y
            x[c] *= k
            x[c] += y
            np.maximum(x[c], 0.0, out=x[c])
    return _shadow_guard_p(x)


# Ombres : le rouge ajouté par la compensation et la balance, sous une lumière
# ambiante bleue, donne du pourpre (rouge et bleu tous deux au-dessus du vert).
MAGENTA_K = 0.85                     # part du magenta retirée dans les ombres
SHADOW_Y0, SHADOW_Y1 = 0.30, 0.60    # luminance encodée : pleine action -> aucune
BLACK_Y = 0.10                       # sous ce niveau, la couleur s'éteint vers le noir
NEUTRAL_C, NEUTRAL_K = 0.25, 0.7    # tons presque neutres : magenta retiré à 70 %
BLACK_KEEP = 0.35                    # chroma gardée au noir absolu


def _shadow_guard(x: np.ndarray) -> np.ndarray:
    """Retire le magenta des ombres et la teinte des quasi-noirs, à
    luminance constante."""
    return _stack(_shadow_guard_p(_planes(x)))


def _shadow_guard_p(x):
    """``_shadow_guard`` sur trois plans (modifiés sur place)."""
    y = _dot(x, LUMA_E)
    w = np.clip((SHADOW_Y1 - y) / (SHADOW_Y1 - SHADOW_Y0), 0.0, 1.0)
    m = np.maximum(np.minimum(x[0], x[2]) - x[1], 0.0)
    m *= w
    m *= MAGENTA_K
    if m.any():
        x[0] -= m
        x[2] -= m
        r = y / np.maximum(_dot(x, LUMA_E), 1e-5)
        for c in range(3):
            x[c] *= r
    # Tons presque neutres (sable, roche, épave) à toute luminosité : une
    # légère teinte lavande vient de la balance, pas de la scène.
    mx = _max3p(x)
    chroma = (mx - _min3p(x)) / np.maximum(mx, 1e-3)
    wn = np.clip((NEUTRAL_C - chroma) / (0.5 * NEUTRAL_C), 0.0, 1.0)
    wn *= NEUTRAL_K
    m = np.maximum(np.minimum(x[0], x[2]) - x[1], 0.0)
    m *= wn
    if m.any():
        x[0] -= m
        x[2] -= m
        # Rose (rouge au-dessus du vert ET du bleu) : même traitement
        pk = np.maximum(x[0] - np.maximum(x[1], x[2]), 0.0)
        pk *= (x[2] > x[1])
        pk *= wn
        x[0] -= pk
        r = y / np.maximum(_dot(x, LUMA_E), 1e-5)
        for c in range(3):
            x[c] *= r
    kb = BLACK_KEEP + (1.0 - BLACK_KEEP) * np.clip(y / BLACK_Y, 0.0, 1.0)
    for c in range(3):
        x[c] -= y
        x[c] *= kb
        x[c] += y
        np.maximum(x[c], 0.0, out=x[c])
    return x


SHOULDER_KNEE = 0.80


def shoulder(x: np.ndarray, knee: float = SHOULDER_KNEE) -> np.ndarray:
    """Épaule des hautes lumières, sans changer la teinte.

    Le canal le plus fort de chaque pixel est compressé au-dessus de *knee*
    vers 1 (courbe exponentielle, jamais atteinte) ; les trois canaux sont
    mis à l'échelle du même facteur.  Les blancs gardent leur modelé au lieu
    d'être coupés net à 255.
    """
    return _stack(_shoulder_p(_planes(x), knee))


def _shoulder_p(x, knee: float = SHOULDER_KNEE):
    m = _max3p(x)
    r = 1.0 - knee
    over = np.maximum(m - knee, 0.0)
    if over.any():
        # m2 = m sous le genou, knee + r·(1 − e^(−over/r)) au-dessus
        over /= -r
        np.exp(over, out=over)
        m2 = np.minimum(m, knee) + r * (1.0 - over)
        m2 /= np.maximum(m, 1e-6)
        for c in range(3):
            x[c] *= m2
    for c in range(3):
        np.clip(x[c], 0.0, 1.0, out=x[c])
    return x


def _clarity(enc: np.ndarray, a: np.ndarray, b: np.ndarray, amount: float
             ) -> np.ndarray:
    return _stack(_clarity_p(_planes(enc), a, b, amount))


def _clarity_p(enc, a, b, amount):
    L = np.clip(_dot(enc, LUMA_E), 0.0, 1.0)
    base = a * L
    base += b
    w = 4.0 * L * (1.0 - L)            # tons moyens seulement : ni blancs ni noirs
    L2 = L + amount * w * (L - base)
    ratio = np.clip(L2 / np.maximum(L, 1e-3), 0.5, 2.0)
    for c in range(3):
        enc[c] *= ratio
    return enc


# ---------------------------------------------------------------------------
# Réduction du bruit
# ---------------------------------------------------------------------------
# La correction amplifie le bruit (gain du rouge, retrait du voile, niveaux).
# Dans l'eau et les zones lisses, et dans les ombres, il se voit : mouchetis
# de couleur surtout.  On y lisse la couleur (chrominance) et, plus
# légèrement, le grain fin de la luminance ; les zones texturées (récif,
# sable, épave) et les contours ne sont pas touchés.

DN_CHROMA_SIGMA = 0.0011    # lissage de la couleur (part du grand côté)
DN_LUMA_SIGMA = 0.0008      # lissage du grain fin de luminance
DN_LUMA_T = 0.02           # écart de luminance en dessous duquel c'est du grain
DN_EDGE = 0.05              # écart de luminance qui signale un contour (poisson, bord)
DN_SHADOW_Y = (0.15, 0.30)  # luminance : ombre pleine … pas une ombre


def smooth_zones(guide: np.ndarray) -> np.ndarray:
    """Carte 0..1 des zones lisses (eau, voile, dégradés) de la copie réduite."""
    fine = fine_texture(guide)
    ko = _odd(0.03 * min(fine.shape[:2]))
    fine = cv2.morphologyEx(fine, cv2.MORPH_OPEN,
                            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (ko, ko)))
    w = np.clip((TEX_LO - fine) / (0.5 * TEX_LO), 0.0, 1.0).astype(np.float32)
    sig = max(1.0, 0.01 * min(w.shape[:2]))
    return cv2.GaussianBlur(w, (0, 0), sig, borderType=cv2.BORDER_REFLECT)


def _zone_noise(gray: np.ndarray, zones: np.ndarray) -> float:
    """Grain moyen (résidu du laplacien d'Immerkær) dans les zones lisses."""
    k = np.array([[1, -2, 1], [-2, 4, -2], [1, -2, 1]], np.float32)
    r = np.abs(cv2.filter2D(gray.astype(np.float32), -1, k, borderType=cv2.BORDER_REFLECT))
    m = zones > 0.5
    if m.sum() < 200:
        return float(r.mean())
    return float(r[m].mean())


def _denoise_rows(img: np.ndarray, dn: np.ndarray, amount: float,
                  full_hw: Tuple[int, int], ya: int, yb: int, y0: int, y1: int,
                  out: np.ndarray) -> None:
    """Réduit le bruit des lignes y0..y1 ; *img* couvre ya..yb (marge).
    Tout se fait en flottant : seuls les écarts de couleur et de grain sont
    ajoutés à l'image, sans aller-retour arrondi par un autre espace."""
    H, W = full_hw
    top = np.float32(65535 if img.dtype == np.uint16 else 255)
    rgb = [img[..., c].astype(np.float32) for c in range(3)]
    for p in rgb:
        p /= top
    Y = _dot(rgb, (0.299, 0.587, 0.114))
    blur = lambda a, sg: cv2.GaussianBlur(a, (0, 0), sg, borderType=cv2.BORDER_REFLECT)
    sc = max(0.8, DN_CHROMA_SIGMA * max(H, W))
    Yb = blur(Y, sc)
    zones = np.clip(upsample_rows(dn, full_hw, ya, yb), 0.0, 1.0)
    shadow = np.clip((DN_SHADOW_Y[1] - Yb) / (DN_SHADOW_Y[1] - DN_SHADOW_Y[0]), 0.0, 1.0)
    shadow *= 0.7
    wc = np.maximum(zones, shadow)
    # Contours (poisson sur l'eau, bord d'un récif) : la couleur n'y bave pas
    edge = np.abs(Y - Yb)
    edge *= -1.0 / DN_EDGE
    edge += 1.5
    np.clip(edge, 0.0, 1.0, out=edge)
    wc *= edge
    wc *= amount
    # Chrominance (R − Y, B − Y) lissée
    cr = rgb[0] - Y
    cb = rgb[2] - Y
    dcr = blur(cr, sc)
    dcr -= cr
    dcr *= wc
    dcb = blur(cb, sc)
    dcb -= cb
    dcb *= wc
    # Y inchangé : dR = dcr, dB = dcb, dV = −(0,299·dcr + 0,114·dcb) / 0,587
    dg = dcr * (-0.299 / 0.587)
    dg -= dcb * (0.114 / 0.587)
    sl = DN_LUMA_SIGMA * max(H, W)
    if sl >= 0.7:
        # Grain fin : les petits écarts sont atténués, les grands (détails) gardés
        d = Y - blur(Y, sl)
        d2 = d * d
        k = d2 / (d2 + DN_LUMA_T * DN_LUMA_T)
        k -= 1.0
        k *= d
        zones *= 0.85 * amount
        k *= zones                          # = −(part du grain retirée)
        dcr += k
        dg += k
        dcb += k
    a = y0 - ya
    b = a + (y1 - y0)
    for c, dlt in ((0, dcr), (1, dg), (2, dcb)):
        v = rgb[c][a:b]
        v += dlt[a:b]
        np.clip(v, 0.0, 1.0, out=v)
        v *= top
        v += 0.5
        out[y0:y1, :, c] = v.astype(img.dtype)


def denoise(img: np.ndarray, plan: "Plan", amount: Optional[float] = None,
            workers: Optional[int] = None) -> np.ndarray:
    """Réduit le bruit d'une image corrigée (uint8/uint16), par tranches."""
    amount = plan.settings.denoise if amount is None else amount
    if amount <= 0.02 or plan.dn is None:
        return img
    H, W = img.shape[:2]
    margin = int(np.ceil(4.5 * max(0.8, DN_CHROMA_SIGMA * max(H, W)))) + 2
    rows = int(min(H, max(64, round(1_500_000 / max(W, 1)))))
    spans = [(y0, min(H, y0 + rows)) for y0 in range(0, H, rows)]
    out = np.empty_like(img)

    def work(span):
        y0, y1 = span
        ya, yb = max(0, y0 - margin), min(H, y1 + margin)
        _denoise_rows(img[ya:yb], plan.dn, float(amount), (H, W), ya, yb, y0, y1, out)

    if workers is None:
        workers = MAX_WORKERS
    if workers <= 1 or len(spans) < 2 or threading.current_thread().name.startswith(
            "aquimage-strip"):
        for sp in spans:
            work(sp)
    else:
        list(_pool().map(work, spans))
    return out


# ---------------------------------------------------------------------------
# Plan de correction
# ---------------------------------------------------------------------------

@dataclass
class Plan:
    """Tout ce qu'il faut pour corriger l'image à n'importe quelle taille."""

    settings: Settings
    comp: np.ndarray            # (k_R, k_B) de la compensation
    gains: np.ndarray           # gains de balance des blancs (linéaire)
    water: np.ndarray           # lumière de l'eau, espace linéaire compensé
    keep: float                 # part de teinte d'eau gardée au loin
    t_ab: Tuple[np.ndarray, np.ndarray]     # coefficients de la transmission
    pre: float                  # mise à l'échelle linéaire avant encodage
    lo: float                   # niveau noir (encodé)
    slope: float                # pente des niveaux
    gamma: float                # gamma des tons moyens
    tgain: np.ndarray           # température / teinte
    clar_ab: Tuple[np.ndarray, np.ndarray]
    clarity: float
    stats: dict
    # Transmission très lissée (copie réduite) qui module la couleur : les
    # gains et la compensation ne suivent pas les marbrures de la carte fine,
    # sinon l'eau uniforme se tache de rose et de vert.
    tcol: Optional[np.ndarray] = None
    wtone: Optional[np.ndarray] = None   # facteurs de teinte de l'eau lointaine
    capw: Optional[np.ndarray] = None    # plafond du rouge (zones lisses sans rouge)
    clipk: float = 1.0                   # blanchiment des hautes lumières écrêtées
    cast: Optional[np.ndarray] = None    # (h, w, 2) dominante locale des surfaces neutres
    dn: Optional[np.ndarray] = None      # zones lisses (0..1) où le bruit est réduit

    def info(self) -> dict:
        d = dict(self.stats)
        d.update({
            "gains": [round(float(v), 3) for v in self.gains],
            "water": [round(float(v), 4) for v in self.water],
            "keep": round(self.keep, 3),
            "gamma": round(self.gamma, 3),
        })
        return d


SKY_BG = 5.0     # bleu/vert (linéaire) maximal d'un ciel ; l'eau bleue est bien plus saturée
SKY_Y = 0.15      # luminance linéaire minimale d'un ciel (l'eau bleue profonde est plus sombre)


def scene_evidence(enc0: np.ndarray) -> dict:
    """Indices sur la lumière de la scène (copie réduite, valeurs encodées).

    - ``red_p90`` : rapport rouge/vert (linéaire) du dixième le plus rouge
      des pixels éclairés.  Sous l'eau en lumière naturelle il reste bien
      sous 1 : le rouge manque partout.  S'il atteint 1, le rouge est là.
    - ``sky`` : part de zones lisses, bleues, mais gardant du rouge
      (R/V de 0,35 à 0,95) et claires : un ciel — ou une eau déjà
      rééquilibrée par l'appareil.  L'eau bleue brute n'a presque plus de rouge.
    - ``hl_rg``, ``hl_bg`` : teinte des hautes lumières non écrêtées.
    """
    lin = decode_linear(enc0)
    R, G, B = lin[..., 0], lin[..., 1], lin[..., 2]
    y = lin @ LUMA
    mx = _max3(enc0)
    lit = (mx < 0.97) & (y > 0.03)
    rg = R / np.maximum(G, 1e-4)
    smooth = fine_texture(guide_of(enc0)) < 0.03
    sky = float((lit & smooth & (B > G) & (rg >= 0.35) & (rg <= 0.95) & (y > SKY_Y)
                 & (B < SKY_BG * G)).mean())
    red_p90 = float(np.percentile(rg[lit], 90)) if lit.sum() > 50 else 0.0
    ok = mx < 0.97
    hl_rg = hl_bg = 1.0
    if ok.sum() > 100:
        hl = ok & (y >= np.percentile(y[ok], 97))
        hl_rg = float(np.median(rg[hl]))
        hl_bg = float(np.median(B[hl] / np.maximum(G[hl], 1e-4)))
    return {"red_p90": red_p90, "sky": sky, "hl_rg": hl_rg, "hl_bg": hl_bg}


def image_stats(img: np.ndarray) -> dict:
    """Statistiques globales utiles à l'auto-réglage (copie réduite)."""
    small = resize_max(img, ANALYSIS_MAX_SIDE)
    enc = to_float(small)
    m = enc.reshape(-1, 3).mean(axis=0)
    return {
        "mean_rgb": [float(v) for v in m],
        "red_deficit": float(1.0 - m[0] / max(m[1], 1e-4)),
        "blue_deficit": float(1.0 - m[2] / max(m[1], 1e-4)),
        "noise": estimate_noise(guide_of(to_float(resize_max(img, 1600)))),
    }


def _wb_gains(lin: np.ndarray, s: Settings,
              weight: Optional[np.ndarray] = None) -> np.ndarray:
    """Gray-world robuste en linéaire (pixels de luminance moyenne),
    éventuellement pondéré, gains plafonnés, luminance conservée."""
    px = lin.reshape(-1, 3)
    y = px @ LUMA
    lo_y, hi_y = np.percentile(y, [5, 98])
    keep = (y >= lo_y) & (y <= hi_y)
    if keep.sum() < 64:
        keep[:] = True
    w = np.ones(len(px), np.float32) if weight is None else weight.ravel() + 1e-3
    w = w * keep
    mc = np.maximum((px * w[:, None]).sum(axis=0) / w.sum(), 1e-5)
    g = (mc @ LUMA) / mc
    g = np.power(np.clip(g, 1.0 / s.max_gain, s.max_gain), s.color)
    return (g * ((mc @ LUMA) / max(float((mc * g) @ LUMA), 1e-6))).astype(np.float32)


# Surfaces presque neutres (sable, roche, corail mort) : s'il leur reste une
# teinte rose ou jaune après la balance, c'est une dominante.  Zone morte :
# un sable est naturellement un peu chaud, on ne le rend pas gris.
CAST_SAT = 0.24                       # saturation maximale d'une surface « presque neutre »
CAST_R = (-0.06, 0.05)                # ln(R/V) toléré
CAST_B = (-0.22, 0.06)                # ln(B/V) toléré
CAST_K = 0.9                          # part de l'excès retirée
CAST_MAX = 0.35                       # correction maximale (en ln)
CAST_DENSITY = (0.03, 0.12)           # densité locale de surfaces neutres (rien … pleine dose)
CAST_SIGMA = 0.12                     # étendue du voisinage (part du petit côté)


def surface_cast(wb: np.ndarray, guide: np.ndarray, enc0: np.ndarray,
                 dose: float = 1.0) -> Tuple[Optional[np.ndarray], float]:
    """Carte (h, w, 2) des corrections ln(R), ln(B) qui retirent la dominante
    rose, magenta ou jaune des surfaces presque neutres et texturées, région
    par région : un appareil réglé « sous l'eau » rosit souvent le sable
    proche tout en laissant le sable lointain cyan.  *wb* : image linéaire
    après balance.  Retourne (carte ou None, correction maximale)."""
    e = encode_srgb(np.minimum(wb / max(float(np.percentile(_max3(wb), 99.5)), 1e-3), 1.0))
    mx, mn = _max3(e), _min3(e)
    sat = (mx - mn) / np.maximum(mx, 1e-3)
    y = e @ LUMA_E
    tex = np.clip((fine_texture(guide) - 0.5 * TEX_LO) / (0.5 * TEX_LO), 0.0, 1.0)
    w = (np.clip(1.0 - sat / CAST_SAT, 0.0, 1.0) * tex
         * ((y > 0.25) & (y < 0.92) & (_max3(enc0) < 0.97))).astype(np.float32)
    if float(w.mean()) < 0.005 or dose <= 0:
        return None, 0.0
    sig = CAST_SIGMA * min(w.shape)
    blur = lambda a: cv2.GaussianBlur(a.astype(np.float32), (0, 0), sig,
                                      borderType=cv2.BORDER_REFLECT)
    G = np.maximum(wb[..., 1], 1e-5)
    den = blur(w)
    lr = blur(w * np.log(np.maximum(wb[..., 0], 1e-5) / G)) / np.maximum(den, 1e-4)
    lb = blur(w * np.log(np.maximum(wb[..., 2], 1e-5) / G)) / np.maximum(den, 1e-4)
    conf = np.clip((den - CAST_DENSITY[0]) / (CAST_DENSITY[1] - CAST_DENSITY[0]), 0.0, 1.0)
    conf = conf * conf * (3.0 - 2.0 * conf) * (CAST_K * dose)
    out = np.empty(w.shape + (2,), np.float32)
    out[..., 0] = -np.clip(conf * (lr - np.clip(lr, *CAST_R)), -CAST_MAX, CAST_MAX)
    out[..., 1] = -np.clip(conf * (lb - np.clip(lb, *CAST_B)), -CAST_MAX, CAST_MAX)
    peak = float(np.abs(out).max())
    return (out if peak > 0.01 else None), peak


def build_plan(img: np.ndarray, settings: Optional[Settings] = None,
               small: Optional[np.ndarray] = None) -> Plan:
    """Calcule le plan de correction d'une image (RGB encodé, toute taille).

    *small* : copie réduite déjà calculée (sinon elle est créée).
    """
    s = Settings.from_dict((settings or Settings()).to_dict())
    small = small if small is not None else resize_max(img, ANALYSIS_MAX_SIDE)
    enc0 = to_float(small)
    h, w = enc0.shape[:2]
    mm = min(h, w)
    guide = guide_of(enc0)

    # 1. Compensation (seulement si le canal est réellement déficitaire)
    m = enc0.reshape(-1, 3).mean(axis=0)
    comp = np.array([
        s.red_comp * max(0.0, float(m[1] - m[0])),
        s.red_comp * max(0.0, float(m[1] - m[2])) if m[1] > 1.05 * m[2] else 0.0,
    ], np.float32)
    lin = decode_linear(_compensate(enc0, comp))

    # 2-3. Balance des blancs puis voile, en deux passes : la 2e passe calcule
    # la balance surtout sur les sujets proches (pondération t²), pour que
    # l'eau, souvent majoritaire, n'impose pas sa couleur aux sujets.
    patch = _odd(0.03 * mm)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (patch, patch))
    t_rad = max(4, int(round(0.06 * mm)))
    weight = None
    for _ in range(2):
        g = _wb_gains(lin, s, weight)
        wb = lin * g
        dark = cv2.erode(_min3(wb), kernel)
        A_wb, flat = water_quadtree(wb, dark, guide)
        A_wb = np.maximum(A_wb, 1e-3)
        t_raw = 1.0 - s.omega * cv2.erode(_min3(wb / A_wb), kernel)
        t_raw = np.clip(t_raw, 0.0, 1.0).astype(np.float32)
        t_ab = guided_coeffs(guide, t_raw, t_rad, 1e-3)
        t = np.clip(t_ab[0] * guide + t_ab[1], 0.0, 1.0)
        weight = t * t
    water = (A_wb / g).astype(np.float32)          # dans l'espace linéaire
    cast, cast_peak = surface_cast(wb, guide, enc0, min(1.0, s.color / 0.3))

    # 4. Restauration + gains et compensation spatialement variables
    # Pas d'eau libre visible (fond de sable, récif plein cadre) : rien à
    # préserver, tout est corrigé comme un sujet.
    open_water = open_water_share(t, guide)
    # (progressif : deux photos presque identiques ne basculent pas d'un rendu
    # à l'autre pour un cheveu)
    keep = s.water_keep * ramp(open_water, OPEN_WATER_LO, OPEN_WATER_HI)
    if keep < 0.02:
        keep = 0.0
    tcol, wmask = color_t(t, guide, enc0 if keep > 0 else None, water, True)
    capw = red_cap_weight(enc0, guide)
    if wmask is not None:
        # Toute zone reconnue comme de l'eau (bleue ou verte) : pas de magenta
        capw = np.maximum(capw, wmask)
    nored = no_red_weight(enc0)
    lin = decode_linear(_compensate(enc0, comp, _near_weight(tcol, keep)))
    rec = _restore(lin, t, water, g, keep, s, tcol, None, capw, nored, cast)
    wtone = water_tone(rec, tcol, keep)
    if not np.allclose(wtone, 1.0):
        rec = _restore(lin, t, water, g, keep, s, tcol, wtone, capw, nored, cast)
    t_far = float(np.percentile(t, 5))

    # 5. Tonalité : rien n'est coupé net.  Mise à l'échelle linéaire pour
    # que les hautes lumières tiennent, niveaux bornés (étirement ≤ stretch),
    # auto-gamma modéré, puis épaule douce des hautes lumières.
    # L'échelle se règle sur les hautes lumières *non brûlées* de l'original :
    # soleil et reflets écrêtés (blanchis plus loin par _clip_weight), dont le
    # rouge a été multiplié par la balance, assombrissaient toute l'image.
    mx = _max3(rec)
    ok = _max3(enc0) < 0.95
    ref = mx[ok] if ok.mean() > 0.5 else mx
    pre = float(1.0 / max(1.0, float(np.percentile(ref, PRE_PCT))))
    enc = encode_srgb(rec * pre)
    Y = enc.reshape(-1, 3) @ LUMA_E
    # Point noir : on ne retire qu'une part (black_level) du « plancher » de
    # luminance ; retirer tout donne des noirs durs et un bruit visible.
    floor = float(np.percentile(Y, s.black_clip)) if s.black_clip > 0 else 0.0
    # Eau laiteuse (épave, eau verte) : plancher haut -> on en retire davantage,
    # sinon les « noirs » restent gris et l'image paraît délavée.
    # (pas en pleine eau, réglée sans étirement : son plancher est l'eau elle-même)
    haze = float(np.clip((floor - HAZE_FLOOR0) / HAZE_FLOOR_SPAN, 0.0, 1.0)) \
        if s.stretch > 1.5 else 0.0
    bl = s.black_level + (HAZE_BLACK - s.black_level) * haze
    lo = max(s.black_level, bl) * floor
    hi = float(np.percentile(_max3(enc), 100.0 - s.white_clip))
    slope = float(min(1.0 / max(hi - lo, 1e-3), s.stretch))
    med = float(np.median(np.clip((Y - lo) * slope, 0, 1)))
    # Cible des tons moyens : 0,42, mais une photo claire à l'origine (eau
    # lumineuse, épave dans une eau laiteuse) n'est pas assombrie bien
    # en dessous de sa luminosité d'origine.
    y0_med = float(np.median(enc0.reshape(-1, 3) @ LUMA_E))
    target = max(MID_TARGET, min(y0_med - MID_DROP, 0.55))
    # Noirs francs (eau laiteuse) : les tons moyens peuvent remonter davantage
    g_min = max(0.66, (0.8 if target <= MID_TARGET else 0.74) - 0.14 * haze)
    g_auto = float(np.clip(np.log(target) / np.log(min(max(med, 0.02), 0.98)),
                           g_min, 1.25))
    if g_auto < 1.0:
        # Photo déjà pleine (vrais noirs, vraies lumières) : on n'éclaircit
        # presque pas, un fond sombre au flash doit rester sombre.
        g_auto = 1.0 - (1.0 - g_auto) * (1.0 - (1.0 - FULL_LIFT_KEEP) * tonal_fullness(enc0))
    g_auto = 1.0 + (g_auto - 1.0) * s.auto_tone
    gamma = g_auto * float(2.0 ** (-0.5 * s.exposure))
    tgain = (np.exp(np.array([0.10, 0.0, -0.10]) * s.temperature)
             * np.exp(np.array([0.05, -0.08, 0.05]) * s.tint)).astype(np.float32)
    clipk = clip_dose(s)
    out = shoulder(_post(enc, lo, slope, gamma, tgain, s.saturation,
                         _clip_weight(enc0, clipk), s.shadows, s.vibrance, nored))
    L = guide_of(out)
    clar_ab = guided_coeffs(L, L, max(4, int(round(0.08 * mm))), 0.02)
    dn = smooth_zones(guide)
    # Bruit multiplié par la correction, mesuré dans les zones lisses
    noise_gain = _zone_noise(L, dn) / max(_zone_noise(guide, dn), 1e-4)

    stats = {
        "mean_rgb": [round(float(v), 4) for v in m],
        "red_deficit": round(float(1.0 - m[0] / max(m[1], 1e-4)), 3),
        "t_mean": round(float(t.mean()), 3),
        "water_flat": round(flat, 4),
        "water_wb": [round(float(v), 4) for v in A_wb],
        "t_far": round(t_far, 3),
        "open_water": round(open_water, 3),
        "cast": round(cast_peak, 3),
        "noise_gain": round(float(noise_gain), 2),
        "auto_keep": round(choose_keep(enc0, tcol, comp, water, g, s), 3),
    }
    return Plan(settings=s, comp=comp, gains=g, water=water, keep=keep,
                t_ab=t_ab, pre=pre, lo=lo, slope=slope, gamma=gamma, tgain=tgain,
                clar_ab=clar_ab, clarity=1.2 * s.contrast, stats=stats, tcol=tcol,
                wtone=wtone, capw=capw, clipk=clipk, cast=cast, dn=dn)


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

def _apply_strip(part: np.ndarray, plan: Plan, full_hw: Tuple[int, int],
                 y0: int, y1: int, out: np.ndarray) -> None:
    """Corrige une bande (lignes y0..y1) et l'écrit dans *out* (même forme)."""
    s = plan.settings
    scale = np.float32(1.0 / 255.0 if part.dtype == np.uint8 else
                       1.0 / 65535.0 if part.dtype == np.uint16 else 1.0)
    src = _planes(part)
    if part.dtype in (np.uint8, np.uint16):
        enc0 = [p.astype(np.float32) * scale for p in src]
        lin0 = [decode_linear(p) for p in src]
    else:
        enc0 = [np.clip(p.astype(np.float32), 0.0, 1.0) for p in src]
        lin0 = [decode_linear(p) for p in enc0]
    guide = _dot(enc0, (0.299, 0.587, 0.114))
    t = upsample_rows(plan.t_ab[0], full_hw, y0, y1)
    t *= guide
    t += upsample_rows(plan.t_ab[1], full_hw, y0, y1)
    np.clip(t, 0.0, 1.0, out=t)
    tc = (t if plan.tcol is None
          else np.clip(upsample_rows(plan.tcol, full_hw, y0, y1), 0.0, 1.0))
    cw = (None if plan.capw is None
          else np.clip(upsample_rows(plan.capw, full_hw, y0, y1), 0.0, 1.0))
    # L'original n'est décodé qu'une fois (signal, zones sans rouge)
    ratio = lin0[0] / np.maximum(lin0[1] + lin0[2], 1e-4)
    nored = _smooth01((NORED_R1 - ratio) / (NORED_R1 - NORED_R0))
    clipw = (0.6 * plan.clipk) * np.clip((_max3p(enc0) - 0.97) / 0.025, 0.0, 1.0)
    # Compensation d'Ancuti (voir _compensate)
    comp = plan.comp
    if comp[0] > 0 or comp[1] > 0:
        G = enc0[1] * _near_weight(tc, plan.keep)
        G *= signal_weight(_dot(lin0, LUMA))
        e = list(enc0)
        for c, k in ((0, comp[0]), (2, comp[1])):
            if k > 0:
                v = (1.0 - enc0[c]) * k
                v *= G
                v += enc0[c]
                e[c] = np.clip(v, 0.0, 1.0, out=v)
        lin = list(lin0)
        for c in (0, 2):
            if e[c] is not enc0[c]:
                lin[c] = decode_linear(e[c])
    else:
        lin = lin0
    cast = (None if plan.cast is None
            else upsample_rows(plan.cast, full_hw, y0, y1))
    rec = _restore_p(lin, t, plan.water, plan.gains, plan.keep, s, tc, plan.wtone,
                     cw, nored, cast=cast)
    pre = np.float32(plan.pre)
    for c in range(3):
        rec[c] *= pre
        rec[c] = encode_srgb(rec[c])
    enc = _post_p(rec, plan.lo, plan.slope, plan.gamma, plan.tgain, s.saturation,
                  clipw, s.shadows, s.vibrance, nored)
    if plan.clarity > 1e-3:
        a = upsample_rows(plan.clar_ab[0], full_hw, y0, y1)
        b = upsample_rows(plan.clar_ab[1], full_hw, y0, y1)
        enc = _clarity_p(enc, a, b, plan.clarity)
    enc = _shoulder_p(enc)
    q = 65535.0 if out.dtype == np.uint16 else 255.0
    for c in range(3):
        v = enc[c]
        v *= q
        v += 0.5
        out[..., c] = v.astype(out.dtype)


_POOL = None
_POOL_LOCK = threading.Lock()


def _pool() -> ThreadPoolExecutor:
    """Fils d'exécution partagés pour les bandes (numpy et OpenCV libèrent le
    verrou de l'interpréteur pendant les calculs)."""
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = ThreadPoolExecutor(max_workers=max(1, min(8, os.cpu_count() or 1)),
                                       thread_name_prefix="aquimage-strip")
        return _POOL


def apply_plan(img: np.ndarray, plan: Plan, out_dtype=None,
               progress=None, workers: Optional[int] = None) -> np.ndarray:
    """Applique *plan* à *img* (RGB encodé, toute résolution).

    Retourne une image du même type que l'entrée (ou *out_dtype*).
    *progress(fraction)* est appelé après chaque bande.  Les bandes sont
    traitées en parallèle (*workers* = 1 pour un traitement séquentiel) ; le
    résultat est identique dans les deux cas.
    """
    if out_dtype is None:
        out_dtype = img.dtype if img.dtype in (np.uint8, np.uint16) else np.uint8
    H, W = img.shape[:2]
    out = np.empty((H, W, 3), out_dtype)
    rows = int(min(256, max(8, round(STRIP_PIXELS / max(W, 1)))))
    spans = [(y0, min(H, y0 + rows)) for y0 in range(0, H, rows)]

    def work(span):
        y0, y1 = span
        _apply_strip(img[y0:y1], plan, (H, W), y0, y1, out[y0:y1])
        return y1

    if workers is None:
        workers = MAX_WORKERS
    if workers <= 1 or len(spans) < 3 or threading.current_thread().name.startswith(
            "aquimage-strip"):
        for sp in spans:
            y1 = work(sp)
            if progress:
                progress(y1 / H)
        return denoise(out, plan, workers=1)
    done = 0
    sem = threading.Semaphore(workers)       # au plus *workers* bandes à la fois

    def guarded(span):
        with sem:
            return work(span)

    for _ in _pool().map(guarded, spans):
        done += 1
        if progress:
            progress(done / len(spans))
    return denoise(out, plan, workers=workers)


def transmission_map(plan: Plan, img: np.ndarray) -> np.ndarray:
    """Carte de transmission à la taille de *img* (float 0..1)."""
    H, W = img.shape[:2]
    g = guide_of(to_float(img))
    return np.clip(upsample_rows(plan.t_ab[0], (H, W), 0, H) * g
                   + upsample_rows(plan.t_ab[1], (H, W), 0, H), 0, 1)


def enhance(img: np.ndarray, settings: Optional[Settings] = None) -> np.ndarray:
    """Corrige une image RGB encodée avec des réglages donnés."""
    return apply_plan(img, build_plan(img, settings))


def refresh_maps(plan: Plan, img: np.ndarray,
                 small: Optional[np.ndarray] = None,
                 prev: Optional[Plan] = None, smooth: float = 0.0) -> Plan:
    """Nouveau plan pour une autre image de la même scène (vidéo).

    Les paramètres globaux (compensation, gains, eau, niveaux, gamma) sont
    gardés : pas de scintillement de couleur.  Seules les cartes
    (transmission, clarté) sont recalculées, en une passe, et lissées dans le
    temps avec *prev* (smooth = poids de l'image précédente, 0..1).
    """
    s = plan.settings
    small = small if small is not None else resize_max(img, ANALYSIS_MAX_SIDE)
    enc0 = to_float(small)
    mm = min(enc0.shape[:2])
    guide = guide_of(enc0)
    t_rad = max(4, int(round(0.06 * mm)))
    patch = _odd(0.03 * mm)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (patch, patch))

    lin = decode_linear(_compensate(enc0, plan.comp))
    wb = lin * plan.gains
    A_wb = np.maximum(plan.water * plan.gains, 1e-3)
    t_raw = 1.0 - s.omega * cv2.erode(_min3(wb / A_wb), kernel)
    t_ab = guided_coeffs(guide, np.clip(t_raw, 0, 1).astype(np.float32),
                         t_rad, 1e-3)
    if prev is not None and smooth > 0 and prev.t_ab[0].shape == t_ab[0].shape:
        t_ab = (smooth * prev.t_ab[0] + (1 - smooth) * t_ab[0],
                smooth * prev.t_ab[1] + (1 - smooth) * t_ab[1])
    t = np.clip(t_ab[0] * guide + t_ab[1], 0.0, 1.0)
    tcol, wmask = color_t(t, guide, enc0 if plan.keep > 0 else None, plan.water, True)
    if prev is not None and smooth > 0 and prev.tcol is not None \
            and prev.tcol.shape == tcol.shape:
        tcol = smooth * prev.tcol + (1 - smooth) * tcol

    capw = red_cap_weight(enc0, guide)
    if wmask is not None:
        capw = np.maximum(capw, wmask)
    # Dominante locale des surfaces neutres : elle suit l'image
    cast, _ = surface_cast(wb, guide, enc0, min(1.0, s.color / 0.3))
    if prev is not None and smooth > 0:
        pc = prev.cast
        if cast is None and pc is not None:
            cast = np.zeros_like(pc)
        if cast is not None:
            if pc is None or pc.shape != cast.shape:
                pc = np.zeros_like(cast)
            cast = smooth * pc + (1 - smooth) * cast
    lin = decode_linear(_compensate(enc0, plan.comp, _near_weight(tcol, plan.keep)))
    rec = _restore(lin, t, plan.water, plan.gains, plan.keep, s, tcol, plan.wtone, capw,
                   no_red_weight(enc0), cast)
    out = shoulder(_post(encode_srgb(rec * plan.pre), plan.lo, plan.slope,
                         plan.gamma, plan.tgain, s.saturation,
                         _clip_weight(enc0, plan.clipk),
                s.shadows, s.vibrance, no_red_weight(enc0)))
    L = guide_of(out)
    clar_ab = guided_coeffs(L, L, max(4, int(round(0.08 * mm))), 0.02)
    if prev is not None and smooth > 0 and prev.clar_ab[0].shape == clar_ab[0].shape:
        clar_ab = (smooth * prev.clar_ab[0] + (1 - smooth) * clar_ab[0],
                   smooth * prev.clar_ab[1] + (1 - smooth) * clar_ab[1])
    return Plan(settings=s, comp=plan.comp, gains=plan.gains, water=plan.water,
                keep=plan.keep, t_ab=t_ab, pre=plan.pre, lo=plan.lo, slope=plan.slope,
                gamma=plan.gamma, tgain=plan.tgain, clar_ab=clar_ab,
                clarity=plan.clarity, stats=plan.stats, tcol=tcol, wtone=plan.wtone,
                capw=capw, clipk=plan.clipk, cast=cast, dn=smooth_zones(guide))


def merge_plans(plans) -> Plan:
    """Plan global d'une vidéo : médiane des paramètres globaux de plusieurs
    images clés (les cartes viennent de la première, recalculées ensuite)."""
    p0 = plans[0]
    med = lambda name: np.median(np.stack([getattr(p, name) for p in plans]), axis=0)
    return Plan(settings=p0.settings, comp=med("comp").astype(np.float32),
                gains=med("gains").astype(np.float32),
                water=med("water").astype(np.float32),
                keep=float(np.median([p.keep for p in plans])),
                t_ab=p0.t_ab, pre=float(np.median([p.pre for p in plans])),
                lo=float(np.median([p.lo for p in plans])),
                slope=float(np.median([p.slope for p in plans])),
                gamma=float(np.median([p.gamma for p in plans])),
                tgain=p0.tgain, clar_ab=p0.clar_ab, clarity=p0.clarity,
                stats=p0.stats, tcol=p0.tcol, capw=p0.capw, clipk=p0.clipk,
                cast=p0.cast, dn=p0.dn,
                wtone=(None if any(p.wtone is None for p in plans)
                       else med("wtone").astype(np.float32)))
