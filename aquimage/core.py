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

from dataclasses import asdict, dataclass, fields
from typing import Dict, Optional, Tuple

import cv2
import numpy as np

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

ANALYSIS_MAX_SIDE = 640          # côté max de la copie d'analyse
STRIP_ROWS = 256                 # hauteur des bandes pour apply_plan
LUMA = np.array([0.2126, 0.7152, 0.0722], np.float32)    # linéaire Rec.709
LUMA_E = np.array([0.299, 0.587, 0.114], np.float32)     # encodé (Rec.601)
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

    LIMITS = {
        "red_comp": (0.0, 2.0), "color": (0.0, 1.0), "max_gain": (1.0, 12.0),
        "water_keep": (0.0, 1.0), "strength": (0.0, 1.0),
        "omega": (0.5, 0.98), "t_min": (0.05, 0.6),
        "temperature": (-1.0, 1.0), "tint": (-1.0, 1.0),
        "exposure": (-2.0, 2.0), "contrast": (0.0, 1.0),
        "saturation": (0.0, 2.0), "black_clip": (0.0, 5.0),
        "white_clip": (0.0, 5.0), "stretch": (1.0, 4.0),
        "vibrance": (0.0, 1.0), "shadows": (0.0, 1.0), "black_level": (0.0, 1.0),
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

def _compensate(enc: np.ndarray, comp: np.ndarray,
                weight: Optional[np.ndarray] = None) -> np.ndarray:
    """Compensation d'Ancuti.  comp = (k_R, k_B) = α·(Ḡ − R̄), α·(Ḡ − B̄).

    *weight* (carte 0..1) module la compensation : pleine sur les sujets
    proches, réduite dans l'eau lointaine (sinon halo jaune au soleil).
    """
    out = enc.copy()
    G = enc[..., 1] if weight is None else enc[..., 1] * weight
    if comp[0] > 0:
        R = enc[..., 0]
        out[..., 0] = R + comp[0] * (1.0 - R) * G
    if comp[1] > 0:
        B = enc[..., 2]
        out[..., 2] = B + comp[1] * (1.0 - B) * G
    return np.clip(out, 0.0, 1.0)


def _near_weight(t: np.ndarray, keep: float) -> np.ndarray:
    """1 sur les sujets proches (t≈1), → 1 − keep dans l'eau très lointaine."""
    return (1.0 - keep * (1.0 - t) ** 2).astype(np.float32)


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
        c = _restore(lin, tf, water, gains, float(keep), s)[0]
        bad = (c[:, 0] > c[:, 1] * 1.02) | (c[:, 0] > c[:, 2] * 1.02)
        if bad.mean() < max_bad:
            return float(keep)
    # Aucune valeur ne garde ces zones bleues : ce sont surtout des sujets
    # lointains (épave, récif) -> compromis.
    return float(0.5 * (lo + hi))


def _restore(lin: np.ndarray, t: np.ndarray, water: np.ndarray,
             gains: np.ndarray, keep: float, s: Settings) -> np.ndarray:
    """Retrait du voile + gains spatialement variables.

    W(x) = G(x) · [lin + s·((lin − A)/max(t, t_min) + A − lin)]
    G(x) = g ** (1 − keep·(1 − t(x))²)   (correction partielle, eau lointaine seulement)
    """
    tt = np.maximum(t, s.t_min)[..., None]
    # Voile à retirer V = A·(1 − t), borné pixel par pixel : jamais plus de
    # 90 % d'un canal, avec un facteur commun aux 3 canaux (sinon les ombres
    # perdent leur vert et virent au violet).
    veil = water * (1.0 - tt)
    f = np.clip(_min3(0.9 * lin / np.maximum(veil, 1e-6))[..., None],
                0.0, 1.0)
    te = 1.0 - f * (1.0 - tt)
    rec = lin + s.strength * ((lin - water * (1.0 - te)) / te - lin)
    if keep > 0:
        expo = (1.0 - keep * (1.0 - t) ** 2)[..., None]
        G = np.exp(np.log(gains)[None, None, :] * expo)
    else:
        G = gains[None, None, :]
    return np.maximum(rec, 0.0) * G


def _clip_weight(enc0: np.ndarray) -> np.ndarray:
    """Poids 0..0,6 des pixels réellement écrêtés dans l'original."""
    return (0.6 * np.clip((_max3(enc0) - 0.97) / 0.025, 0.0, 1.0)).astype(np.float32)


def _post(enc: np.ndarray, lo: float, slope: float, gamma: float,
          tgain: np.ndarray, sat: float,
          clipw: Optional[np.ndarray] = None,
          shadows: float = 0.0, vibrance: float = 0.0) -> np.ndarray:
    x = np.maximum((enc - lo) * slope, 0.0)
    if abs(gamma - 1.0) > 1e-3:
        x = np.power(x, gamma)
    if shadows > 1e-3:
        # Éclaircit les ombres (maximum vers L = 1/3), sans toucher aux blancs
        L = np.clip(x @ LUMA_E, 1e-4, 1.0)
        L2 = L + 1.2 * shadows * L * (1.0 - L) ** 2
        x = x * (L2 / L)[..., None]
    if clipw is not None and clipw.any():
        # Hautes lumières écrêtées dans l'original : couleur perdue -> blanc
        m = _max3(x)[..., None]
        x = x + clipw[..., None] * (m - x)
    x = x * tgain
    if abs(sat - 1.0) > 1e-3 or vibrance > 1e-3:
        y = (x @ LUMA_E)[..., None]
        k = sat
        if vibrance > 1e-3:
            # Vibrance : gain fort sur les couleurs ternes, nul sur les vives
            c = (_max3(x)[..., None] - _min3(x)[..., None]) \
                / np.maximum(_max3(x)[..., None], 1e-3)
            k = sat * (1.0 + vibrance * 0.6 * np.clip(1.0 - 2.0 * c, 0.0, 1.0))
        x = np.maximum(y + k * (x - y), 0.0)
    return x


SHOULDER_KNEE = 0.80


def shoulder(x: np.ndarray, knee: float = SHOULDER_KNEE) -> np.ndarray:
    """Épaule des hautes lumières, sans changer la teinte.

    Le canal le plus fort de chaque pixel est compressé au-dessus de *knee*
    vers 1 (courbe exponentielle, jamais atteinte) ; les trois canaux sont
    mis à l'échelle du même facteur.  Les blancs gardent leur modelé au lieu
    d'être coupés net à 255.
    """
    m = _max3(x)[..., None]
    r = 1.0 - knee
    over = np.maximum(m - knee, 0.0)
    m2 = np.where(m > knee, knee + r * (1.0 - np.exp(-over / r)), m)
    return np.clip(x * (m2 / np.maximum(m, 1e-6)), 0.0, 1.0)


def _clarity(enc: np.ndarray, a: np.ndarray, b: np.ndarray, amount: float
             ) -> np.ndarray:
    L = np.clip(guide_of(enc), 0.0, 1.0)
    base = a * L + b
    w = 4.0 * L * (1.0 - L)            # tons moyens seulement : ni blancs ni noirs
    L2 = L + amount * w * (L - base)
    ratio = np.clip(L2 / np.maximum(L, 1e-3), 0.5, 2.0)
    return enc * ratio[..., None]


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

    def info(self) -> dict:
        d = dict(self.stats)
        d.update({
            "gains": [round(float(v), 3) for v in self.gains],
            "water": [round(float(v), 4) for v in self.water],
            "keep": round(self.keep, 3),
            "gamma": round(self.gamma, 3),
        })
        return d


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

    # 4. Restauration + gains et compensation spatialement variables
    keep = s.water_keep
    lin = decode_linear(_compensate(enc0, comp, _near_weight(t, keep)))
    rec = _restore(lin, t, water, g, keep, s)
    t_far = float(np.percentile(t, 5))

    # 5. Tonalité : rien n'est coupé net.  Mise à l'échelle linéaire pour
    # que les hautes lumières tiennent, niveaux bornés (étirement ≤ stretch),
    # auto-gamma modéré, puis épaule douce des hautes lumières.
    mx = _max3(rec)
    pre = float(1.0 / max(1.0, float(np.percentile(mx, 99.8))))
    enc = encode_srgb(rec * pre)
    Y = enc.reshape(-1, 3) @ LUMA_E
    # Point noir : on ne retire qu'une part (black_level) du « plancher » de
    # luminance ; retirer tout donne des noirs durs et un bruit visible.
    lo = (s.black_level * float(np.percentile(Y, s.black_clip))) if s.black_clip > 0 else 0.0
    hi = float(np.percentile(_max3(enc), 100.0 - s.white_clip))
    slope = float(min(1.0 / max(hi - lo, 1e-3), s.stretch))
    med = float(np.median(np.clip((Y - lo) * slope, 0, 1)))
    g_auto = float(np.clip(np.log(MID_TARGET) / np.log(min(max(med, 0.02), 0.98)),
                           0.8, 1.25))
    gamma = g_auto * float(2.0 ** (-0.5 * s.exposure))
    tgain = (np.exp(np.array([0.10, 0.0, -0.10]) * s.temperature)
             * np.exp(np.array([0.05, -0.08, 0.05]) * s.tint)).astype(np.float32)
    out = shoulder(_post(enc, lo, slope, gamma, tgain, s.saturation, _clip_weight(enc0),
                s.shadows, s.vibrance))
    L = guide_of(out)
    clar_ab = guided_coeffs(L, L, max(4, int(round(0.08 * mm))), 0.02)

    stats = {
        "mean_rgb": [round(float(v), 4) for v in m],
        "red_deficit": round(float(1.0 - m[0] / max(m[1], 1e-4)), 3),
        "t_mean": round(float(t.mean()), 3),
        "water_flat": round(flat, 4),
        "water_wb": [round(float(v), 4) for v in A_wb],
        "t_far": round(t_far, 3),
        "auto_keep": round(choose_keep(enc0, t, comp, water, g, s), 3),
    }
    return Plan(settings=s, comp=comp, gains=g, water=water, keep=keep,
                t_ab=t_ab, pre=pre, lo=lo, slope=slope, gamma=gamma, tgain=tgain,
                clar_ab=clar_ab, clarity=1.2 * s.contrast, stats=stats)


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

def _apply_strip(part: np.ndarray, plan: Plan, full_hw: Tuple[int, int],
                 y0: int, y1: int) -> np.ndarray:
    s = plan.settings
    enc0 = to_float(part)
    guide = guide_of(enc0)
    t = np.clip(upsample_rows(plan.t_ab[0], full_hw, y0, y1) * guide
                + upsample_rows(plan.t_ab[1], full_hw, y0, y1), 0.0, 1.0)
    lin = decode_linear(_compensate(enc0, plan.comp, _near_weight(t, plan.keep)))
    rec = _restore(lin, t, plan.water, plan.gains, plan.keep, s)
    enc = _post(encode_srgb(rec * plan.pre), plan.lo, plan.slope, plan.gamma,
                plan.tgain, s.saturation, _clip_weight(enc0),
                s.shadows, s.vibrance)
    if plan.clarity > 1e-3:
        a = upsample_rows(plan.clar_ab[0], full_hw, y0, y1)
        b = upsample_rows(plan.clar_ab[1], full_hw, y0, y1)
        enc = _clarity(enc, a, b, plan.clarity)
    return shoulder(enc)


def apply_plan(img: np.ndarray, plan: Plan, out_dtype=None,
               progress=None) -> np.ndarray:
    """Applique *plan* à *img* (RGB encodé, toute résolution).

    Retourne une image du même type que l'entrée (ou *out_dtype*).
    *progress(fraction)* est appelé après chaque bande.
    """
    if out_dtype is None:
        out_dtype = img.dtype if img.dtype in (np.uint8, np.uint16) else np.uint8
    H, W = img.shape[:2]
    out = np.empty((H, W, 3), out_dtype)
    for y0 in range(0, H, STRIP_ROWS):
        y1 = min(H, y0 + STRIP_ROWS)
        out[y0:y1] = quantize(_apply_strip(img[y0:y1], plan, (H, W), y0, y1),
                              out_dtype)
        if progress:
            progress(y1 / H)
    return out


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

    lin = decode_linear(_compensate(enc0, plan.comp, _near_weight(t, plan.keep)))
    rec = _restore(lin, t, plan.water, plan.gains, plan.keep, s)
    out = shoulder(_post(encode_srgb(rec * plan.pre), plan.lo, plan.slope,
                         plan.gamma, plan.tgain, s.saturation, _clip_weight(enc0),
                s.shadows, s.vibrance))
    L = guide_of(out)
    clar_ab = guided_coeffs(L, L, max(4, int(round(0.08 * mm))), 0.02)
    if prev is not None and smooth > 0 and prev.clar_ab[0].shape == clar_ab[0].shape:
        clar_ab = (smooth * prev.clar_ab[0] + (1 - smooth) * clar_ab[0],
                   smooth * prev.clar_ab[1] + (1 - smooth) * clar_ab[1])
    return Plan(settings=s, comp=plan.comp, gains=plan.gains, water=plan.water,
                keep=plan.keep, t_ab=t_ab, pre=plan.pre, lo=plan.lo, slope=plan.slope,
                gamma=plan.gamma, tgain=plan.tgain, clar_ab=clar_ab,
                clarity=plan.clarity, stats=plan.stats)


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
                stats=p0.stats)
