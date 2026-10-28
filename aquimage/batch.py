"""
Traitement par lot : dossier source -> dossier destination.

Chaque image (et chaque vidéo) reçoit ses propres réglages automatiques.
Options : sous-dossiers, vidéos, format de sortie, qualité JPEG, ajustements
globaux ajoutés aux réglages automatiques (ex. +0,2 de température pour
tout le lot), ou réglages fixes identiques pour tous les fichiers.

Un rapport ``aquimage_rapport.csv`` est écrit dans la destination (réglages
retenus, durée, erreurs).
"""
from __future__ import annotations

import csv
import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from . import autotune, core, fileio, video
from .core import Settings

ADJUSTABLE = ("temperature", "tint", "exposure", "saturation", "contrast",
              "strength", "water_keep", "color")


@dataclass
class BatchOptions:
    src: str
    dst: str
    recursive: bool = True
    videos: bool = True
    mode: str = "auto"                 # "auto" | "fixe"
    adjust: Dict[str, float] = field(default_factory=dict)   # deltas (auto)
    fixed: Optional[dict] = None       # réglages (mode fixe)
    fmt: str = "same"                  # same | jpg | png | tif
    quality: int = 95
    overwrite: bool = False
    suffix: str = ""
    workers: int = 0                   # 0 = automatique


@dataclass
class Item:
    rel: str
    kind: str                          # image | video
    status: str = "en attente"         # en attente | en cours | ok | ignoré | erreur | annulé
    progress: float = 0.0
    message: str = ""
    out: str = ""
    seconds: float = 0.0
    settings: dict = field(default_factory=dict)
    diag: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"rel": self.rel, "kind": self.kind, "status": self.status,
                "progress": round(self.progress, 3), "message": self.message,
                "out": self.out, "seconds": round(self.seconds, 1),
                "type": self.diag.get("type", "")}


def scan(src: str, recursive: bool = True, videos: bool = True) -> List[Item]:
    items: List[Item] = []
    src = os.path.abspath(src)
    for root, dirs, files in os.walk(src):
        # On ne retraite jamais une sortie d'AquImage (dossier « AquImage »
        # créé par l'atelier, ou tout dossier contenant un rapport de lot).
        dirs[:] = sorted(d for d in dirs if not d.startswith(".")
                         and d.lower() != "aquimage"
                         and not os.path.exists(os.path.join(root, d, "aquimage_rapport.csv")))
        for f in sorted(files):
            if f.startswith("."):
                continue
            p = os.path.join(root, f)
            rel = os.path.relpath(p, src)
            if fileio.is_image(p):
                items.append(Item(rel, "image"))
            elif videos and fileio.is_video(p):
                items.append(Item(rel, "video"))
        if not recursive:
            break
    return items


def out_path(opts: BatchOptions, item: Item) -> str:
    stem, ext = os.path.splitext(item.rel)
    if item.kind == "video":
        ext = ".mp4"
    elif opts.fmt in ("jpg", "png", "tif"):
        ext = {"jpg": ".jpg", "png": ".png", "tif": ".tif"}[opts.fmt]
    return os.path.join(os.path.abspath(opts.dst), stem + opts.suffix + ext)


def settings_for(opts: BatchOptions, img=None, video_path: str = "") -> tuple:
    """(réglages, diagnostic) d'un fichier selon le mode du lot."""
    if opts.mode == "fixe" and opts.fixed:
        return Settings.from_dict(opts.fixed), {"type": "réglages fixes"}
    if video_path:
        s, diag = video.video_settings(video_path)
    else:
        s, diag = autotune.auto_settings(img)
    if opts.adjust:
        d = s.to_dict()
        for k, v in opts.adjust.items():
            if k in ADJUSTABLE and v:
                d[k] = d.get(k, 0.0) + float(v)
        s = Settings.from_dict(d)
    return s, diag


class BatchJob:
    """Lot exécuté dans un fil d'exécution ; état consultable à tout moment."""

    def __init__(self, opts: BatchOptions,
                 on_update: Optional[Callable[["BatchJob"], None]] = None):
        if os.path.abspath(opts.src) == os.path.abspath(opts.dst) and not opts.suffix:
            raise ValueError("La destination doit être différente de la source "
                             "(ou indiquez un suffixe).")
        if not os.path.isdir(opts.src):
            raise ValueError(f"Dossier source introuvable : {opts.src}")
        self.opts = opts
        self.items = scan(opts.src, opts.recursive, opts.videos)
        dst_abs = os.path.abspath(opts.dst)
        # Ne pas retraiter la destination si elle est dans la source.
        self.items = [i for i in self.items
                      if not os.path.abspath(os.path.join(opts.src, i.rel)).startswith(dst_abs + os.sep)]
        self.cancel = threading.Event()
        self.started = 0.0
        self.finished = 0.0
        self.state = "prêt"
        self.error = ""
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._on_update = on_update

    # -- état -------------------------------------------------------------
    def summary(self) -> dict:
        with self._lock:
            n = len(self.items)
            done = sum(1 for i in self.items if i.status in ("ok", "ignoré", "erreur", "annulé"))
            frac = sum(1.0 if i.status in ("ok", "ignoré", "erreur", "annulé") else i.progress
                       for i in self.items) / n if n else 1.0
            el = (self.finished or time.time()) - self.started if self.started else 0.0
            eta = el / frac * (1 - frac) if 0.02 < frac < 1 and self.state == "en cours" else None
            return {
                "state": self.state, "error": self.error, "total": n,
                "done": done, "fraction": round(frac, 4),
                "ok": sum(1 for i in self.items if i.status == "ok"),
                "errors": sum(1 for i in self.items if i.status == "erreur"),
                "skipped": sum(1 for i in self.items if i.status == "ignoré"),
                "elapsed": round(el, 1), "eta": None if eta is None else round(eta),
                "src": self.opts.src, "dst": self.opts.dst,
                "items": [i.to_dict() for i in self.items],
            }

    def _touch(self):
        if self._on_update:
            try:
                self._on_update(self)
            except Exception:
                pass

    # -- exécution --------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, daemon=True)
        self._thread.start()

    def run(self) -> None:
        self.state = "en cours"
        self.started = time.time()
        os.makedirs(self.opts.dst, exist_ok=True)
        workers = self.opts.workers or max(1, min(4, (os.cpu_count() or 2) // 2))
        images = [i for i in self.items if i.kind == "image"]
        videos = [i for i in self.items if i.kind == "video"]
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(self._do_image, images))
            for it in videos:
                self._do_video(it, workers=max(2, workers))
            self.state = "annulé" if self.cancel.is_set() else "terminé"
        except Exception as exc:          # pragma: no cover
            self.state, self.error = "erreur", str(exc)
        self.finished = time.time()
        try:
            self.write_report()
        except Exception as exc:
            self.error = f"rapport non écrit : {exc}"
        self._touch()

    def _skip_existing(self, it: Item, dst: str) -> bool:
        if os.path.exists(dst) and not self.opts.overwrite:
            it.status, it.message, it.out = "ignoré", "déjà présent", dst
            self._touch()
            return True
        return False

    def _do_image(self, it: Item) -> None:
        if self.cancel.is_set():
            it.status = "annulé"
            return
        src = os.path.join(self.opts.src, it.rel)
        dst = out_path(self.opts, it)
        if self._skip_existing(it, dst):
            return
        t0 = time.time()
        it.status = "en cours"
        self._touch()
        try:
            img, meta = fileio.read_image(src)
            s, diag = settings_for(self.opts, img)
            it.progress = 0.15
            plan = core.build_plan(img, s)
            out = core.apply_plan(img, plan, progress=lambda f: setattr(it, "progress", 0.15 + 0.75 * f))
            fileio.save_image(dst, out, meta, self.opts.quality)
            it.settings, it.diag = s.to_dict(), diag
            it.status, it.out, it.progress = "ok", dst, 1.0
        except Exception as exc:
            it.status, it.message = "erreur", f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
        it.seconds = time.time() - t0
        self._touch()

    def _do_video(self, it: Item, workers: int) -> None:
        if self.cancel.is_set():
            it.status = "annulé"
            return
        src = os.path.join(self.opts.src, it.rel)
        dst = out_path(self.opts, it)
        if self._skip_existing(it, dst):
            return
        t0 = time.time()
        it.status, it.message = "en cours", "analyse"
        self._touch()
        try:
            s, diag = settings_for(self.opts, video_path=src)
            it.settings, it.diag, it.message = s.to_dict(), diag, ""
            info = video.process_video(
                src, dst, s, progress=lambda f: setattr(it, "progress", f),
                cancel=self.cancel, workers=workers)
            it.status, it.out, it.progress = "ok", dst, 1.0
            if not info.get("audio"):
                it.message = "sans son (ffmpeg absent)"
        except video.Cancelled:
            it.status, it.message = "annulé", "interrompu"
        except Exception as exc:
            it.status, it.message = "erreur", f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
        it.seconds = time.time() - t0
        self._touch()

    def write_report(self) -> str:
        path = os.path.join(self.opts.dst, "aquimage_rapport.csv")
        keys = list(Settings().to_dict().keys())
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["fichier", "type", "statut", "sortie", "secondes",
                        "diagnostic", "message"] + keys)
            for it in self.items:
                w.writerow([it.rel, it.kind, it.status, it.out, f"{it.seconds:.1f}",
                            it.diag.get("type", ""), it.message]
                           + [it.settings.get(k, "") for k in keys])
        return path
