"""
Serveur local (bibliothèque standard uniquement) : sert l'interface web et
l'API JSON.  N'écoute que sur 127.0.0.1 et refuse les requêtes dont l'en-tête
Host n'est pas local (protection contre le « DNS rebinding »).
"""
from __future__ import annotations

import json
import mimetypes
import os
import socket
import string
import threading
import time
import traceback
import uuid
import webbrowser
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse

import numpy as np

from . import __version__, autotune, core, fileio, video
from .batch import BatchJob, BatchOptions
from .core import Settings

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
PREVIEW_SIDE = 1400
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".aquimage.json")


# ---------------------------------------------------------------------------
# État de l'application
# ---------------------------------------------------------------------------

class Session:
    """Une image (ou une vidéo) ouverte dans l'atelier."""

    def __init__(self, path: str):
        self.id = uuid.uuid4().hex[:12]
        self.path = path
        self.kind = "video" if fileio.is_video(path) else "image"
        self.pos = 0.5
        self.meta = None
        self.full: Optional[np.ndarray] = None
        if self.kind == "image":
            self.full, self.meta = fileio.read_image(path)
            self.info = {"width": int(self.full.shape[1]),
                         "height": int(self.full.shape[0]),
                         "bits": self.meta.bits}
            self.settings, self.diag = autotune.auto_settings(self.full)
            self._set_frame(self.full)
        else:
            self.info = video.probe(path)
            self.settings, self.diag = video.video_settings(path)
            self.set_pos(0.5)
        self.lock = threading.Lock()

    def _set_frame(self, img: np.ndarray) -> None:
        self.preview = core.resize_max(img, PREVIEW_SIDE)
        self.small = core.resize_max(img, core.ANALYSIS_MAX_SIDE)
        self.before_jpeg = fileio.jpeg_bytes(self.preview, 90)

    def set_pos(self, pos: float) -> None:
        self.pos = float(min(1.0, max(0.0, pos)))
        fr = video.read_frame_at(self.path, self.pos)
        if fr is None:
            raise IOError("image de la vidéo illisible")
        self._set_frame(fr)

    def render(self, settings: Settings) -> bytes:
        plan = core.build_plan(self.preview, settings, small=self.small)
        return fileio.jpeg_bytes(core.apply_plan(self.preview, plan), 90)

    def describe(self) -> dict:
        return {"id": self.id, "path": self.path, "name": os.path.basename(self.path),
                "kind": self.kind, "info": self.info, "pos": self.pos,
                "settings": self.settings.to_dict(), "diag": self.diag}


class App:
    def __init__(self):
        self.sessions: "OrderedDict[str, Session]" = OrderedDict()
        self.thumbs: "OrderedDict[tuple, bytes]" = OrderedDict()
        self.job: Optional[BatchJob] = None
        self.tasks: dict = {}
        self.lock = threading.Lock()
        self.config = self._load_config()

    # -- configuration (derniers dossiers utilisés) -----------------------
    def _load_config(self) -> dict:
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def save_config(self, **kw) -> None:
        self.config.update({k: v for k, v in kw.items() if v is not None})
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    # -- sessions ---------------------------------------------------------
    def open(self, path: str) -> Session:
        s = Session(path)
        with self.lock:
            self.sessions[s.id] = s
            while len(self.sessions) > 3:
                self.sessions.popitem(last=False)
        return s

    def get(self, sid: str) -> Session:
        s = self.sessions.get(sid)
        if s is None:
            raise KeyError("Image fermée : rouvrez-la.")
        return s

    # -- vignettes --------------------------------------------------------
    def thumb(self, path: str, w: int) -> bytes:
        st = os.stat(path)
        key = (path, st.st_mtime, st.st_size, w)
        with self.lock:
            if key in self.thumbs:
                self.thumbs.move_to_end(key)
                return self.thumbs[key]
        data = fileio.jpeg_bytes(fileio.thumbnail(path, w), 82)
        with self.lock:
            self.thumbs[key] = data
            while len(self.thumbs) > 600:
                self.thumbs.popitem(last=False)
        return data


APP = App()


# ---------------------------------------------------------------------------
# Système de fichiers
# ---------------------------------------------------------------------------

def _roots() -> list:
    out = []
    if os.name == "nt":
        for d in string.ascii_uppercase:
            p = f"{d}:\\"
            if os.path.exists(p):
                out.append({"name": p, "path": p})
    else:
        out.append({"name": "/", "path": "/"})
    home = os.path.expanduser("~")
    for name in ("", "Pictures", "Images", "Desktop", "Bureau", "Videos", "OneDrive"):
        p = os.path.join(home, name) if name else home
        if os.path.isdir(p):
            out.append({"name": p, "path": p})
    for k in ("src", "dst", "atelier"):
        p = APP.config.get(k)
        if p and os.path.isdir(p) and all(r["path"] != p for r in out):
            out.append({"name": p, "path": p})
    return out


def browse(path: str) -> dict:
    if not path:
        return {"path": "", "parent": None, "dirs": _roots(), "images": 0, "videos": 0}
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        raise FileNotFoundError(f"Dossier introuvable : {path}")
    dirs, n_img, n_vid = [], 0, 0
    try:
        entries = sorted(os.scandir(path), key=lambda e: e.name.lower())
    except PermissionError:
        raise PermissionError(f"Accès refusé : {path}")
    for e in entries:
        if e.name.startswith((".", "$")):
            continue
        try:
            if e.is_dir():
                dirs.append({"name": e.name, "path": e.path})
            elif fileio.is_image(e.name):
                n_img += 1
            elif fileio.is_video(e.name):
                n_vid += 1
        except OSError:
            pass
    parent = os.path.dirname(path)
    return {"path": path, "parent": parent if parent != path else "",
            "dirs": dirs, "images": n_img, "videos": n_vid}


def list_media(path: str) -> list:
    path = os.path.abspath(path)
    out = []
    for e in sorted(os.scandir(path), key=lambda e: e.name.lower()):
        if e.is_file() and not e.name.startswith("."):
            if fileio.is_image(e.name):
                out.append({"name": e.name, "path": e.path, "kind": "image"})
            elif fileio.is_video(e.name):
                out.append({"name": e.name, "path": e.path, "kind": "video"})
    return out


# ---------------------------------------------------------------------------
# Tâches (export vidéo depuis l'atelier)
# ---------------------------------------------------------------------------

def start_task(fn, *args) -> str:
    tid = uuid.uuid4().hex[:10]
    task = {"state": "en cours", "progress": 0.0, "message": "", "out": "",
            "cancel": threading.Event()}
    APP.tasks[tid] = task

    def run():
        try:
            task["out"] = fn(task, *args)
            task["state"] = "annulé" if task["cancel"].is_set() else "terminé"
            task["progress"] = 1.0
        except video.Cancelled:
            task["state"] = "annulé"
        except Exception as exc:
            traceback.print_exc()
            task["state"], task["message"] = "erreur", str(exc)

    threading.Thread(target=run, daemon=True).start()
    return tid


def _export_video(task, sess: Session, settings: Settings, dst: str) -> str:
    video.process_video(sess.path, dst, settings,
                        progress=lambda f: task.__setitem__("progress", f),
                        cancel=task["cancel"],
                        workers=max(2, min(4, (os.cpu_count() or 2) // 2)))
    return dst


def save_output(sess: Session, settings: Settings, dest_dir: str, fmt: str,
                quality: int, suffix: str) -> dict:
    os.makedirs(dest_dir, exist_ok=True)
    stem, ext = os.path.splitext(os.path.basename(sess.path))
    if sess.kind == "video":
        dst = os.path.join(dest_dir, stem + suffix + ".mp4")
        tid = start_task(_export_video, sess, settings, dst)
        return {"task": tid, "out": dst}
    if fmt in ("jpg", "png", "tif"):
        ext = "." + fmt
    dst = os.path.join(dest_dir, stem + suffix + ext)
    if os.path.abspath(dst) == os.path.abspath(sess.path):
        raise ValueError("Le fichier de sortie remplacerait l'original : "
                         "choisissez un autre dossier ou un suffixe.")
    plan = core.build_plan(sess.full, settings)
    out = core.apply_plan(sess.full, plan)
    fileio.save_image(dst, out, sess.meta, quality)
    return {"out": dst}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = f"AquImage/{__version__}"

    def log_message(self, fmt, *args):      # silencieux
        pass

    # -- réponses -----------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str, cache: bool = False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _error(self, exc: Exception, code: int = 400):
        self._json({"error": str(exc) or type(exc).__name__}, code)

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost", "")

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    # -- GET ------------------------------------------------------------------
    def do_GET(self):
        if not self._host_ok():
            return self._send(403, b"forbidden", "text/plain")
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/" or not u.path.startswith("/api/"):
                return self._static(u.path)
            if u.path == "/api/info":
                return self._json({
                    "version": __version__, "ffmpeg": bool(video.ffmpeg_exe()),
                    "cpus": os.cpu_count(), "config": APP.config,
                    "defaults": Settings().to_dict(), "limits": Settings.LIMITS,
                })
            if u.path == "/api/browse":
                return self._json(browse(q.get("path", "")))
            if u.path == "/api/files":
                return self._json({"files": list_media(q["dir"])})
            if u.path == "/api/thumb":
                return self._send(200, APP.thumb(q["path"], int(q.get("w", 240))),
                                  "image/jpeg", cache=True)
            if u.path == "/api/before":
                return self._send(200, APP.get(q["id"]).before_jpeg, "image/jpeg")
            if u.path == "/api/batch/status":
                return self._json(APP.job.summary() if APP.job else {"state": "aucun"})
            if u.path == "/api/task":
                t = APP.tasks.get(q["id"])
                if not t:
                    raise KeyError("tâche inconnue")
                return self._json({k: v for k, v in t.items() if k != "cancel"})
            return self._send(404, b"not found", "text/plain")
        except FileNotFoundError as exc:
            return self._error(exc, 404)
        except Exception as exc:
            traceback.print_exc()
            return self._error(exc)

    def _static(self, path: str):
        name = "index.html" if path in ("", "/") else path.lstrip("/")
        full = os.path.abspath(os.path.join(WEB_DIR, name))
        if not full.startswith(WEB_DIR) or not os.path.isfile(full):
            return self._send(404, b"not found", "text/plain")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        with open(full, "rb") as f:
            self._send(200, f.read(), ctype)

    # -- POST -----------------------------------------------------------------
    def do_POST(self):
        if not self._host_ok():
            return self._send(403, b"forbidden", "text/plain")
        u = urlparse(self.path)
        try:
            b = self._body()
            if u.path == "/api/open":
                sess = APP.open(b["path"])
                APP.save_config(atelier=os.path.dirname(b["path"]))
                return self._json(sess.describe())
            if u.path == "/api/render":
                sess = APP.get(b["id"])
                with sess.lock:
                    data = sess.render(Settings.from_dict(b.get("settings")))
                return self._send(200, data, "image/jpeg")
            if u.path == "/api/frame":
                sess = APP.get(b["id"])
                with sess.lock:
                    sess.set_pos(float(b.get("pos", 0.5)))
                return self._json(sess.describe())
            if u.path == "/api/auto":
                sess = APP.get(b["id"])
                base = Settings.from_dict(b.get("settings"))
                with sess.lock:
                    if sess.kind == "video":
                        s, d = video.video_settings(sess.path, base)
                    else:
                        s, d = autotune.auto_settings(sess.full, base)
                sess.settings, sess.diag = s, d
                return self._json(sess.describe())
            if u.path == "/api/save":
                sess = APP.get(b["id"])
                dest = b.get("dest") or os.path.join(os.path.dirname(sess.path), "AquImage")
                res = save_output(sess, Settings.from_dict(b.get("settings")), dest,
                                  b.get("fmt", "same"), int(b.get("quality", 95)),
                                  b.get("suffix", ""))
                APP.save_config(atelier_dst=dest)
                return self._json(res)
            if u.path == "/api/task/cancel":
                t = APP.tasks.get(b["id"])
                if t:
                    t["cancel"].set()
                return self._json({"ok": True})
            if u.path == "/api/batch/start":
                if APP.job and APP.job.state == "en cours":
                    raise RuntimeError("Un lot est déjà en cours.")
                opts = BatchOptions(
                    src=b["src"], dst=b["dst"], recursive=bool(b.get("recursive", True)),
                    videos=bool(b.get("videos", True)), mode=b.get("mode", "auto"),
                    adjust={k: float(v) for k, v in (b.get("adjust") or {}).items()},
                    fixed=b.get("fixed"), fmt=b.get("fmt", "same"),
                    quality=int(b.get("quality", 95)),
                    overwrite=bool(b.get("overwrite", False)),
                    suffix=b.get("suffix", ""), workers=int(b.get("workers", 0)))
                job = BatchJob(opts)
                if not job.items:
                    raise ValueError("Aucune image ni vidéo dans le dossier source.")
                APP.job = job
                APP.save_config(src=opts.src, dst=opts.dst)
                job.start()
                return self._json(job.summary())
            if u.path == "/api/batch/cancel":
                if APP.job:
                    APP.job.cancel.set()
                return self._json({"ok": True})
            return self._send(404, b"not found", "text/plain")
        except Exception as exc:
            traceback.print_exc()
            return self._error(exc)


def _free_port(preferred: int) -> int:
    for port in [preferred] + list(range(preferred + 1, preferred + 30)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return 0


def serve(port: int = 8765, open_browser: bool = True) -> None:
    port = _free_port(port)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    url = f"http://127.0.0.1:{port}/"
    print(f"AquImage {__version__} — interface : {url}")
    print("Fermez cette fenêtre (ou Ctrl+C) pour quitter.")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
