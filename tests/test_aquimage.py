"""Tests automatiques d'AquImage :  python -m pytest tests"""
from __future__ import annotations

import os
import sys
import shutil
import subprocess
import tempfile
import threading
import time
import json
import urllib.request

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aquimage import autotune, core, fileio, video  # noqa: E402
from aquimage.batch import BatchJob, BatchOptions, scan  # noqa: E402
from aquimage.core import Settings  # noqa: E402


# ---------------------------------------------------------------- données
def underwater(h=480, w=720, seed=0, water=(0.02, 0.25, 0.32), beta=(0.45, 0.12, 0.10)):
    """Scène texturée « immergée » par le modèle physique, + vérité terrain."""
    rng = np.random.default_rng(seed)
    J = np.zeros((h, w, 3), np.float32)
    for c in range(3):
        n = rng.random((h // 24, w // 24)).astype(np.float32)
        J[..., c] = np.array(Image.fromarray(n).resize((w, h), Image.BICUBIC))
    J = np.clip(0.1 + 0.8 * J, 0, 1)
    y = np.linspace(0, 1, h, dtype=np.float32)[:, None]
    d = 1.5 + 9 * (1 - y) ** 1.5 * np.ones((1, w), np.float32)
    d[: h // 6] = 60
    t = np.exp(-np.array(beta, np.float32) * d[..., None])
    I = J * t + np.array(water, np.float32) * (1 - t)
    enc = core._lin_to_srgb_f(I)
    return (enc * 255 + 0.5).astype(np.uint8), (core._lin_to_srgb_f(J) * 255 + 0.5).astype(np.uint8)


@pytest.fixture(scope="module")
def img():
    return underwater()[0]


# ---------------------------------------------------------------- moteur
def test_lut_roundtrip():
    x = np.linspace(0, 1, 1000, dtype=np.float32)
    back = core.encode_srgb(core.decode_linear(x))
    assert np.abs(back - x).max() < 2e-3


def test_correction_reduces_cast(img):
    out = core.enhance(img, autotune.auto_settings(img)[0])
    k = img.shape[0] // 3                       # zone des sujets
    m_in = img[k:].reshape(-1, 3).mean(0)
    m_out = out[k:].reshape(-1, 3).mean(0)
    # le rouge est restauré : l'écart vert/rouge des sujets est bien réduit
    assert (m_out[1] - m_out[0]) < 0.6 * (m_in[1] - m_in[0])
    assert out.shape == img.shape and out.dtype == np.uint8


def test_closer_to_ground_truth():
    """Photos naturelles « immergées » par le modèle physique (banc bench/)."""
    pytest.importorskip("skimage")
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from bench.groundtruth import cases, delta_e
    cs = cases(max_side=480)[:8]
    if not cs:
        pytest.skip("images de référence absentes")
    d_in = np.mean([delta_e(c["I"], c["J"]) for c in cs])
    d_out = np.mean([delta_e(core.enhance(c["I"], Settings(water_keep=0)), c["J"]) for c in cs])
    assert d_out < d_in - 3, (d_in, d_out)


def test_preview_matches_full_resolution(img):
    big = np.array(Image.fromarray(img).resize((2880, 1920), Image.BICUBIC))
    plan = core.build_plan(big, Settings())
    full = core.apply_plan(big, plan)
    prev = core.apply_plan(core.resize_max(big, 720), plan)
    full_small = np.array(Image.fromarray(full).resize(prev.shape[1::-1], Image.BOX))
    assert np.abs(prev.astype(int) - full_small).mean() < 2.0


def test_strips_equal_single_pass(img, monkeypatch):
    plan = core.build_plan(img, Settings())
    a = core.apply_plan(img, plan)
    monkeypatch.setattr(core, "STRIP_ROWS", 10_000)
    b = core.apply_plan(img, plan)
    assert np.abs(a.astype(int) - b).max() <= 1


def test_16bit_kept(img):
    im16 = img.astype(np.uint16) * 257
    out = core.enhance(im16)
    assert out.dtype == np.uint16
    assert len(np.unique(out[..., 1])) > 256


def test_neutral_image_mostly_unchanged():
    rng = np.random.default_rng(1)
    g = (rng.random((300, 400, 1)) * 200 + 30).astype(np.uint8)
    gray = np.repeat(g, 3, axis=2)
    s, diag = autotune.auto_settings(gray)
    assert diag["type"].startswith("déjà")
    out = core.enhance(gray, s).astype(int)
    assert np.abs(out[..., 0] - out[..., 2]).mean() < 6   # reste neutre


def test_settings_clamped():
    s = Settings.from_dict({"strength": 9, "temperature": -5, "inconnu": 1})
    assert s.strength == 1.0 and s.temperature == -1.0


# ---------------------------------------------------------------- fichiers
def test_unicode_path_and_exif(tmp_path, img):
    d = tmp_path / "Plongée été"
    d.mkdir()
    src = d / "requin é.jpg"
    exif = Image.Exif()
    exif[0x0110] = "SEALIFE DC2000"
    exif[0x0112] = 6                       # portrait : rotation à 90°
    Image.fromarray(img).save(src, "JPEG", exif=exif.tobytes(), quality=92)
    arr, meta = fileio.read_image(str(src))
    assert arr.shape[:2] == (img.shape[1], img.shape[0])     # redressée
    dst = d / "sortie" / "requin é.jpg"
    fileio.save_image(str(dst), core.enhance(arr), meta)
    with Image.open(dst) as im:
        e = im.getexif()
        assert e.get(0x0110) == "SEALIFE DC2000"
        assert e.get(0x0112) == 1
        assert im.size == (img.shape[0], img.shape[1])


def test_16bit_tiff_roundtrip(tmp_path, img):
    p = tmp_path / "a.tif"
    fileio.save_image(str(p), img.astype(np.uint16) * 257)
    arr, meta = fileio.read_image(str(p))
    assert arr.dtype == np.uint16 and meta.bits == 16


# ---------------------------------------------------------------- lot
def _make_tree(root, img):
    os.makedirs(os.path.join(root, "jour 2"), exist_ok=True)
    Image.fromarray(img).save(os.path.join(root, "a.jpg"), quality=90)
    Image.fromarray(img).save(os.path.join(root, "jour 2", "b.png"))
    os.makedirs(os.path.join(root, "AquImage"), exist_ok=True)       # sortie atelier
    Image.fromarray(img).save(os.path.join(root, "AquImage", "a.jpg"))


def test_batch(tmp_path, img):
    src, dst = str(tmp_path / "src"), str(tmp_path / "dst")
    _make_tree(src, img)
    assert sorted(i.rel for i in scan(src)) == ["a.jpg", os.path.join("jour 2", "b.png")]
    job = BatchJob(BatchOptions(src=src, dst=dst, adjust={"temperature": 0.2}))
    job.run()
    s = job.summary()
    assert s["state"] == "terminé" and s["ok"] == 2, s
    assert os.path.exists(os.path.join(dst, "jour 2", "b.png"))
    rep = open(os.path.join(dst, "aquimage_rapport.csv"), encoding="utf-8-sig").read()
    assert "a.jpg" in rep and "sous-marine" in rep
    # 2e passage : rien à refaire
    job2 = BatchJob(BatchOptions(src=src, dst=dst))
    job2.run()
    assert job2.summary()["skipped"] == 2


def test_batch_refuses_same_folder(tmp_path):
    with pytest.raises(ValueError):
        BatchJob(BatchOptions(src=str(tmp_path), dst=str(tmp_path)))


# ---------------------------------------------------------------- vidéo
@pytest.mark.skipif(not video.ffmpeg_exe(), reason="ffmpeg absent")
def test_video_with_audio(tmp_path):
    ff = video.ffmpeg_exe()
    src = str(tmp_path / "clip.mov")
    subprocess.run([ff, "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc2=size=320x240:rate=15:duration=2", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=2", "-vf", "colorchannelmixer=rr=0.3:gg=0.9:bb=1.0",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le", src], check=True)
    s, diag = video.video_settings(src)
    dst = str(tmp_path / "out.mp4")
    info = video.process_video(src, dst, s, workers=2)
    assert info["frames"] == 30 and info["audio"]
    meta = video.probe(dst)
    assert meta["width"] == 320 and meta["frames"] >= 29


def test_video_cancel(tmp_path):
    if not video.ffmpeg_exe():
        pytest.skip("ffmpeg absent")
    ff = video.ffmpeg_exe()
    src = str(tmp_path / "c.mp4")
    subprocess.run([ff, "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc2=size=320x240:rate=15:duration=4", "-pix_fmt", "yuv420p", src], check=True)
    ev = threading.Event()
    ev.set()
    with pytest.raises(video.Cancelled):
        video.process_video(src, str(tmp_path / "o.mp4"), Settings(), cancel=ev)
    assert not os.path.exists(tmp_path / "o.mp4")


# ---------------------------------------------------------------- serveur
def test_server_api(tmp_path, img):
    from http.server import ThreadingHTTPServer
    from aquimage import server
    server.CONFIG_PATH = str(tmp_path / "cfg.json")
    server.APP.config = {}
    Image.fromarray(img).save(tmp_path / "x.jpg")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    def post(path, body):
        req = urllib.request.Request(base + path, json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        return urllib.request.urlopen(req, timeout=60)

    try:
        assert b"AquImage" in urllib.request.urlopen(base + "/").read()
        d = json.loads(post("/api/open", {"path": str(tmp_path / "x.jpg")}).read())
        r = post("/api/render", {"id": d["id"], "settings": d["settings"]})
        assert r.headers["Content-Type"] == "image/jpeg" and len(r.read()) > 1000
        out = json.loads(post("/api/save", {"id": d["id"], "settings": d["settings"],
                                            "dest": str(tmp_path / "out")}).read())
        assert os.path.exists(out["out"])
        b = json.loads(urllib.request.urlopen(base + "/api/browse?path=" + urllib.request.quote(str(tmp_path))).read())
        assert b["images"] == 1
        req = urllib.request.Request(base + "/api/info", headers={"Host": "evil.example"})
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(req)
    finally:
        httpd.shutdown()
