"""
Réglages conservés par photo ou vidéo.

Ce que l'on règle dans l'atelier (curseurs, rotation, recadrage, découpe,
stabilisation) est enregistré dans un petit fichier ``aquimage_reglages.json``
placé dans le dossier des originaux — qui, eux, ne sont jamais modifiés.
L'atelier retrouve ces réglages à la réouverture, et le traitement par lot
les respecte.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from typing import Dict, Optional

FILE = "aquimage_reglages.json"
VERSION = 1

_LOCK = threading.RLock()
_CACHE: Dict[str, tuple] = {}          # dossier -> (mtime_ns, fichiers)


def _file(folder: str) -> str:
    return os.path.join(folder, FILE)


def load(folder: str) -> Dict[str, dict]:
    """{nom de fichier: réglages enregistrés} du dossier (vide si aucun)."""
    folder = os.path.abspath(folder)
    p = _file(folder)
    with _LOCK:
        try:
            mt = os.stat(p).st_mtime_ns
        except OSError:
            _CACHE.pop(folder, None)
            return {}
        hit = _CACHE.get(folder)
        if hit and hit[0] == mt:
            return hit[1]
        try:
            with open(p, encoding="utf-8") as f:
                files = json.load(f).get("files", {})
            if not isinstance(files, dict):
                files = {}
        except Exception:
            files = {}
        _CACHE[folder] = (mt, files)
        return files


def _write(folder: str, files: Dict[str, dict]) -> bool:
    p = _file(folder)
    try:
        if not files:
            if os.path.exists(p):
                os.unlink(p)
            _CACHE.pop(folder, None)
            return True
        fd, tmp = tempfile.mkstemp(prefix=".aqi_", suffix=".json", dir=folder)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"version": VERSION, "files": files}, f,
                      ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, p)
        _CACHE[folder] = (os.stat(p).st_mtime_ns, files)
        return True
    except OSError:
        return False


def get(path: str) -> Optional[dict]:
    """Réglages enregistrés pour ce fichier, ou None."""
    path = os.path.abspath(path)
    e = load(os.path.dirname(path)).get(os.path.basename(path))
    return dict(e) if isinstance(e, dict) else None


def put(path: str, settings: Optional[dict], rot: int = 0, crop=None,
        trim=None, stab: float = 0.0) -> bool:
    """Enregistre les réglages de ce fichier.  *settings* None : seule la
    géométrie (rotation, recadrage, découpe) est gardée, la couleur reste
    automatique.  Faux si le dossier n'est pas accessible en écriture."""
    path = os.path.abspath(path)
    folder = os.path.dirname(path)
    entry = {"rot": int(rot) % 4, "saved": time.strftime("%Y-%m-%d %H:%M:%S")}
    if settings:
        entry["settings"] = {k: round(float(v), 4) for k, v in settings.items()}
    if crop:
        entry["crop"] = [round(float(v), 5) for v in crop]
    if trim and (float(trim[0]) > 1e-4 or float(trim[1]) < 1 - 1e-4):
        entry["trim"] = [round(float(trim[0]), 5), round(float(trim[1]), 5)]
    if stab:
        entry["stab"] = round(float(stab), 3)
    with _LOCK:
        files = dict(load(folder))
        files[os.path.basename(path)] = entry
        return _write(folder, files)


def remove(path: str) -> bool:
    """Oublie les réglages de ce fichier (retour à l'automatique)."""
    path = os.path.abspath(path)
    folder = os.path.dirname(path)
    with _LOCK:
        files = dict(load(folder))
        if files.pop(os.path.basename(path), None) is None:
            return True
        return _write(folder, files)


def names(folder: str) -> set:
    """Noms des fichiers du dossier qui ont des réglages enregistrés."""
    return set(load(folder))
