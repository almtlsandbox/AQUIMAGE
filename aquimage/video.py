"""
Correction des vidéos.

- Réglages estimés une fois par vidéo, sur 8 images clés (médiane), puis
  appliqués à toutes les images : couleur stable, pas de scintillement.
- Cartes de transmission et de clarté recalculées à chaque image (la scène
  bouge) et lissées dans le temps.
- Encodage H.264 (ffmpeg, preset veryfast, CRF 21, débit plafonné à celui de
  la source) avec le son d'origine
  (réencodé en AAC) et les métadonnées (date de prise de vue).
- Sans ffmpeg : repli sur OpenCV (MP4 sans son).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, List, Optional, Sequence, Tuple

import cv2
import numpy as np

from . import autotune, core
from .core import Settings

VIDEO_ANALYSIS_SIDE = 480
SMOOTH = 0.6           # lissage temporel des cartes (part de l'image précédente)
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def ffmpeg_exe() -> Optional[str]:
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.exists(exe):
            return exe
    except Exception:
        pass
    return shutil.which("ffmpeg")


def source_bitrate(path: str) -> Optional[int]:
    """Débit global de la source en kb/s (lu par ffmpeg), ou None."""
    ff = ffmpeg_exe()
    if not ff:
        return None
    try:
        r = subprocess.run([ff, "-hide_banner", "-i", path], capture_output=True,
                           text=True, errors="replace", timeout=30,
                           creationflags=_NO_WINDOW)
        m = re.search(r"bitrate:\s*(\d+)\s*kb/s", r.stderr)
        return int(m.group(1)) if m else None
    except Exception:
        return None


def probe(path: str) -> dict:
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise IOError(f"vidéo illisible : {path}")
    try:
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0) or 30.0
        ok, fr = cap.read()
        h, w = (fr.shape[:2] if ok else (int(cap.get(4)), int(cap.get(3))))
    finally:
        cap.release()
    return {"frames": n, "fps": fps, "width": w, "height": h,
            "duration": n / fps if fps else 0.0}


def read_frame_at(path: str, frac: float) -> Optional[np.ndarray]:
    """Image RGB à la position *frac* (0..1) de la vidéo."""
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return None
    try:
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if n > 1:
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(max(0, min(n - 1, frac * (n - 1)))))
        ok, fr = cap.read()
        return cv2.cvtColor(fr, cv2.COLOR_BGR2RGB) if ok else None
    finally:
        cap.release()


def key_frames(path: str, n: int = 8, max_side: int = 960,
               span: Tuple[float, float] = (0.0, 1.0)) -> List[np.ndarray]:
    """*n* images réparties dans la plage *span* (fractions de la durée)."""
    a, b = span
    out = []
    for i in range(n):
        f = read_frame_at(path, a + (b - a) * (0.05 + 0.9 * i / max(1, n - 1)))
        if f is not None:
            out.append(core.resize_max(f, max_side))
    return out


def video_settings(path: str, base: Optional[Settings] = None,
                   auto: bool = True) -> tuple:
    """(réglages, diagnostic) pour une vidéo."""
    frames = key_frames(path)
    if not frames:
        raise IOError("aucune image lisible dans la vidéo")
    if auto:
        return autotune.auto_settings_multi(frames, base)
    return (base or Settings()), {"type": "réglages fixes", "kind": "fixed"}


class Cancelled(Exception):
    pass


# ---------------------------------------------------------------------------
# Stabilisation
# ---------------------------------------------------------------------------

STAB_SIDE = 480          # analyse du mouvement sur une copie réduite
STAB_MAX_ZOOM = 1.15     # recadrage maximal pour masquer les bords


def stab_transforms(src: str, rotate: int, start: int, count: int,
                    fps: float, strength: float,
                    progress: Optional[Callable[[float], None]] = None,
                    cancel: Optional[threading.Event] = None) -> Tuple[list, float]:
    """Matrices 2×3 (pleine résolution, image pivotée) qui stabilisent
    chaque image, et le zoom appliqué.

    Mouvement image à image : points suivis (Lucas-Kanade) puis similitude
    (translation + rotation).  La trajectoire cumulée est lissée par une
    moyenne glissante ; on corrige l'écart entre trajectoire réelle et lissée.
    Plus *strength* est grand (0..1), plus la fenêtre est longue (0,5 à 2 s).
    """
    cap = cv2.VideoCapture(src)
    cap.set(cv2.CAP_PROP_POS_FRAMES, start)
    prev, f, size = None, 1.0, None
    steps = []
    for i in range(count):
        if cancel is not None and cancel.is_set():
            cap.release()
            raise Cancelled()
        ok, fr = cap.read()
        if not ok:
            break
        g = cv2.cvtColor(core.rotate90(fr, rotate), cv2.COLOR_BGR2GRAY)
        if size is None:
            size = g.shape[1], g.shape[0]
            f = STAB_SIDE / max(size)
        g = cv2.resize(g, (max(8, int(size[0] * f)), max(8, int(size[1] * f))),
                       interpolation=cv2.INTER_AREA)
        d = (0.0, 0.0, 0.0)
        if prev is not None:
            pts = cv2.goodFeaturesToTrack(prev, 200, 0.01, 12)
            if pts is not None and len(pts) >= 8:
                nxt, st, _ = cv2.calcOpticalFlowPyrLK(prev, g, pts, None)
                ok_ = st.ravel() == 1
                if ok_.sum() >= 8:
                    m, _ = cv2.estimateAffinePartial2D(pts[ok_], nxt[ok_])
                    if m is not None:
                        d = (float(m[0, 2]), float(m[1, 2]),
                             float(np.arctan2(m[1, 0], m[0, 0])))
        steps.append(d)
        prev = g
        if progress and count:
            progress(i / count)
    cap.release()
    if not steps or size is None:
        return [], 1.0
    traj = np.cumsum(np.array(steps, np.float64), axis=0)
    r = max(1, int(round(fps * (0.5 + 1.5 * float(strength)))))
    pad = np.pad(traj, ((r, r), (0, 0)), mode="edge")
    ker = np.ones(2 * r + 1) / (2 * r + 1)
    smooth = np.stack([np.convolve(pad[:, k], ker, mode="valid") for k in range(3)], 1)
    corr = smooth - traj
    corr[:, :2] /= f                       # en pixels pleine résolution
    W, H = size
    need = 1.0 + 2.0 * max(np.abs(corr[:, 0]).max() / W,
                           np.abs(corr[:, 1]).max() / H) + 2.0 * np.abs(corr[:, 2]).max()
    if need > STAB_MAX_ZOOM:              # mouvement trop ample : correction partielle
        corr *= (STAB_MAX_ZOOM - 1.0) / (need - 1.0)
        need = STAB_MAX_ZOOM
    zoom = float(need)
    mats = []
    for dx, dy, da in corr:
        m = cv2.getRotationMatrix2D((W / 2.0, H / 2.0), float(np.degrees(da)), zoom)
        m[0, 2] += dx * zoom
        m[1, 2] += dy * zoom
        mats.append(m)
    return mats, zoom


def crop_box(w: int, h: int, crop: Optional[Sequence[float]]) -> Tuple[int, int, int, int]:
    """Rectangle (x0, y0, x1, y1) en pixels, dimensions paires (H.264).

    Largeur et hauteur viennent des écarts de fractions (un cadre carré
    reste carré malgré les arrondis).
    """
    if not crop:
        fx0, fy0, fx1, fy1 = 0.0, 0.0, 1.0, 1.0
    else:
        fx0, fy0, fx1, fy1 = [min(max(float(v), 0.0), 1.0) for v in crop]
    cw = max(16, int(round((fx1 - fx0) * w)) // 2 * 2)
    ch = max(16, int(round((fy1 - fy0) * h)) // 2 * 2)
    cw, ch = min(cw, w - w % 2), min(ch, h - h % 2)
    x0 = min(max(int(round(fx0 * w)), 0), w - cw)
    y0 = min(max(int(round(fy0 * h)), 0), h - ch)
    return x0, y0, x0 + cw, y0 + ch


def process_video(src: str, dst: str, settings: Settings,
                  progress: Optional[Callable[[float], None]] = None,
                  cancel: Optional[threading.Event] = None,
                  crf: int = 21, preset: str = "veryfast",
                  workers: int = 2, rotate: int = 0,
                  crop: Optional[Sequence[float]] = None,
                  trim: Sequence[float] = (0.0, 1.0),
                  stabilize: float = 0.0) -> dict:
    """Corrige *src* vers *dst* (mp4) avec *settings* (déjà estimés).

    Ordre : découpe (*trim*, fractions de la durée) → rotation (*rotate*,
    quarts de tour horaires) → stabilisation (*stabilize* 0..1, 0 = aucune)
    → recadrage (*crop*, fractions x0, y0, x1, y1 de l'image pivotée)
    → correction des couleurs.
    """
    t0 = time.time()
    rotate = int(rotate) % 4
    a = float(min(max(trim[0], 0.0), 1.0))
    b = float(min(max(trim[1], a), 1.0))

    cap = cv2.VideoCapture(src)
    if not cap.isOpened():
        raise IOError(f"vidéo illisible : {src}")
    n_total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0) or 30.0
    trimmed = (a > 0.0 or b < 1.0) and n_total > 1
    start = int(round(a * (n_total - 1))) if trimmed else 0
    end = int(round(b * (n_total - 1))) + 1 if trimmed else max(n_total, 1)
    count = max(1, end - start)
    if start:
        cap.set(cv2.CAP_PROP_POS_FRAMES, start)
    ok, first = cap.read()
    if not ok:
        cap.release()
        raise IOError("aucune image lisible dans la vidéo")
    h, w = first.shape[:2]
    if rotate % 2:
        w, h = h, w
    x0, y0, x1, y1 = crop_box(w, h, crop)
    ow, oh = x1 - x0, y1 - y0

    # Stabilisation : première passe (analyse du mouvement)
    stab_share = 0.25 if stabilize > 0 else 0.0
    mats, zoom = ([], 1.0)
    if stabilize > 0:
        mats, zoom = stab_transforms(
            src, rotate, start, count, fps, stabilize,
            progress=(lambda f: progress(stab_share * f)) if progress else None,
            cancel=cancel)

    def prepare(rgb: np.ndarray, i: int) -> np.ndarray:
        rgb = core.rotate90(rgb, rotate)
        if mats:
            m = mats[min(i, len(mats) - 1)]
            rgb = cv2.warpAffine(rgb, m, (w, h), flags=cv2.INTER_LINEAR,
                                 borderMode=cv2.BORDER_REFLECT)
        return np.ascontiguousarray(rgb[y0:y1, x0:x1])

    # Réglages globaux estimés sur des images de la plage gardée, recadrées
    span = (start / max(1, n_total - 1), (end - 1) / max(1, n_total - 1)) if trimmed else (0.0, 1.0)
    frames = []
    for fk in key_frames(src, 8, VIDEO_ANALYSIS_SIDE * 2, span):
        fr = core.rotate90(fk, rotate)
        fh, fw = fr.shape[:2]
        bx = crop_box(fw, fh, crop) if crop else (0, 0, fw, fh)
        frames.append(np.ascontiguousarray(fr[bx[1]:bx[3], bx[0]:bx[2]]))
    plans = [core.build_plan(f, settings,
                             small=core.resize_max(f, VIDEO_ANALYSIS_SIDE))
             for f in frames]
    gplan = core.merge_plans(plans)

    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    tmp_dst = os.path.join(os.path.dirname(os.path.abspath(dst)),
                           ".aqi_" + os.path.basename(dst))

    ff = ffmpeg_exe()
    proc = None
    writer = None
    errlog = tempfile.TemporaryFile()
    if ff:
        # Débit plafonné à celui de la source : une vidéo corrigée ne doit pas
        # peser plus lourd que l'originale (le bruit rehaussé coûte cher).
        kbps = source_bitrate(src)
        rate_cap = (["-maxrate", f"{kbps}k", "-bufsize", f"{2 * kbps}k"] if kbps else [])
        audio_in = (["-ss", f"{start / fps:.3f}", "-t", f"{count / fps:.3f}"]
                    if trimmed else [])
        cmd = [ff, "-y", "-loglevel", "error",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{ow}x{oh}",
               "-r", f"{fps:.6f}", "-i", "-",
               ] + audio_in + ["-i", src,
               "-map", "0:v:0", "-map", "1:a?", "-map_metadata", "1",
               "-c:v", "libx264", "-preset", preset, "-crf", str(int(crf)),
               ] + rate_cap + [
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-shortest",
               "-movflags", "+faststart", "-f", "mp4", tmp_dst]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=errlog, creationflags=_NO_WINDOW)
    else:
        writer = cv2.VideoWriter(tmp_dst, cv2.VideoWriter_fourcc(*"mp4v"),
                                 fps, (ow, oh))

    def write(rgb: np.ndarray) -> None:
        if proc is not None:
            proc.stdin.write(np.ascontiguousarray(rgb).tobytes())
        else:
            writer.write(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))

    done = 0
    prev = None
    pending: deque = deque()
    try:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            frame, idx = first, 0
            while frame is not None and idx < count:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                rgb = prepare(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB), idx)
                small = core.resize_max(rgb, VIDEO_ANALYSIS_SIDE)
                prev = core.refresh_maps(gplan, rgb, small, prev,
                                         smooth=SMOOTH if prev is not None else 0.0)
                pending.append(pool.submit(core.apply_plan, rgb, prev))
                while len(pending) > 2 * max(1, workers):
                    write(pending.popleft().result())
                    done += 1
                    if progress:
                        progress(min(0.999, stab_share + (1 - stab_share) * done / count))
                idx += 1
                ok, frame = cap.read()
                if not ok:
                    frame = None
            while pending:
                write(pending.popleft().result())
                done += 1
        if proc is not None:
            proc.stdin.close()
            rc = proc.wait()
            if rc != 0:
                errlog.seek(0)
                raise IOError("ffmpeg : " + errlog.read().decode("utf-8", "replace")[-800:])
        else:
            writer.release()
        os.replace(tmp_dst, dst)
    except BaseException:
        if proc is not None:
            try:
                proc.stdin.close()
            except Exception:
                pass
            proc.kill()
            proc.wait()
        if writer is not None:
            writer.release()
        try:
            os.unlink(tmp_dst)
        except OSError:
            pass
        raise
    finally:
        cap.release()
        errlog.close()
    if progress:
        progress(1.0)
    return {"frames": done, "fps": round(fps, 3), "width": ow, "height": oh,
            "seconds": round(time.time() - t0, 1), "stab_zoom": round(zoom, 3),
            "audio": bool(proc is not None), "encoder": "ffmpeg" if proc else "opencv"}
