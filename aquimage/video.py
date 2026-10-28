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
from typing import Callable, List, Optional

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


def key_frames(path: str, n: int = 8, max_side: int = 960) -> List[np.ndarray]:
    out = []
    for i in range(n):
        f = read_frame_at(path, 0.05 + 0.9 * i / max(1, n - 1))
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


def process_video(src: str, dst: str, settings: Settings,
                  progress: Optional[Callable[[float], None]] = None,
                  cancel: Optional[threading.Event] = None,
                  crf: int = 21, preset: str = "veryfast",
                  workers: int = 2, rotate: int = 0) -> dict:
    """Corrige *src* vers *dst* (mp4) avec *settings* (déjà estimés).

    *rotate* : quarts de tour dans le sens horaire appliqués à chaque image.
    """
    t0 = time.time()
    rotate = int(rotate) % 4
    frames = [core.rotate90(f, rotate)
              for f in key_frames(src, 8, VIDEO_ANALYSIS_SIDE * 2)]
    plans = [core.build_plan(f, settings,
                             small=core.resize_max(f, VIDEO_ANALYSIS_SIDE))
             for f in frames]
    gplan = core.merge_plans(plans)

    cap = cv2.VideoCapture(src)
    if not cap.isOpened():
        raise IOError(f"vidéo illisible : {src}")
    n_total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0) or 30.0
    ok, first = cap.read()
    if not ok:
        cap.release()
        raise IOError("aucune image lisible dans la vidéo")
    h, w = first.shape[:2]
    if rotate % 2:
        w, h = h, w
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
        cmd = [ff, "-y", "-loglevel", "error",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
               "-r", f"{fps:.6f}", "-i", "-",
               "-i", src,
               "-map", "0:v:0", "-map", "1:a?", "-map_metadata", "1",
               "-c:v", "libx264", "-preset", preset, "-crf", str(int(crf)),
               ] + rate_cap + [
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
               "-movflags", "+faststart", "-f", "mp4", tmp_dst]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=errlog, creationflags=_NO_WINDOW)
    else:
        writer = cv2.VideoWriter(tmp_dst, cv2.VideoWriter_fourcc(*"mp4v"),
                                 fps, (w, h))

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
            frame = first
            while frame is not None:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                rgb = core.rotate90(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB), rotate)
                small = core.resize_max(rgb, VIDEO_ANALYSIS_SIDE)
                prev = core.refresh_maps(gplan, rgb, small, prev,
                                         smooth=SMOOTH if prev is not None else 0.0)
                pending.append(pool.submit(core.apply_plan, rgb, prev))
                while len(pending) > 2 * max(1, workers):
                    write(pending.popleft().result())
                    done += 1
                    if progress and n_total:
                        progress(min(0.999, done / n_total))
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
    return {"frames": done, "fps": round(fps, 3), "width": w, "height": h,
            "seconds": round(time.time() - t0, 1),
            "audio": bool(proc is not None), "encoder": "ffmpeg" if proc else "opencv"}
