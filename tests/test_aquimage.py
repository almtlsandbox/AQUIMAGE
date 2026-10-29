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


# ---------------------------------------------------------------- v1.1 : retours Mer Rouge
def _backlit(h=400, w=600, seed=0):
    """Contre-jour : ciel clair dégradé en haut, récif sombre bleuté en bas."""
    rng = np.random.default_rng(seed)
    y = np.linspace(0, 1, h, dtype=np.float32)[:, None, None]
    sky = np.array([0.72, 0.96, 1.0], np.float32) * (1.0 - 0.25 * y) * np.ones((1, w, 1), np.float32)
    reef = (0.05 + 0.15 * rng.random((h, w, 1), dtype=np.float32)) * np.array([0.4, 0.8, 1.0], np.float32)
    m = (y > 0.45).astype(np.float32)
    img = sky * (1 - m) + reef * m
    return (np.clip(img, 0, 1) * 255 + 0.5).astype(np.uint8)


def test_backlit_highlights_not_blown():
    img = _backlit()
    s, diag = autotune.auto_settings(img)
    assert "contre-jour" in diag["type"]
    out = core.enhance(img, s)
    sky_in = img[: img.shape[0] // 3]
    sky_out = out[: img.shape[0] // 3]
    # le ciel garde son modelé : presque aucun pixel à 255, dégradé conservé
    assert (sky_out.max(axis=2) >= 254).mean() < 0.01
    assert sky_out[..., 1].astype(float).std() > 0.5 * sky_in[..., 1].astype(float).std()


def test_open_water_not_stretched():
    rng = np.random.default_rng(0)
    base = np.array([20, 40, 120], np.float32)
    img = np.clip(base + rng.normal(0, 3, (300, 450, 3)), 0, 255).astype(np.uint8)
    s, diag = autotune.auto_settings(img)
    assert "pleine eau" in diag["type"]
    out = core.enhance(img, s)
    g_in = img.astype(float) @ [0.299, 0.587, 0.114]
    g_out = out.astype(float) @ [0.299, 0.587, 0.114]
    assert g_out.std() < 3.0 * g_in.std() + 2      # pas de bruit étiré x4


def test_no_hard_clipping_on_typical_scene(img):
    out = core.enhance(img, autotune.auto_settings(img)[0])
    assert (out.max(axis=2) >= 254).mean() < 0.005


def test_jpeg_save_falls_back(monkeypatch, tmp_path, img):
    """Un échec « broken data stream » de Pillow ne doit pas perdre la photo."""
    from PIL import Image as PILImage
    real = PILImage.Image.save
    calls = {"n": 0}

    def flaky(self, fp, format=None, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("broken data stream when writing image file")
        return real(self, fp, format, **kw)

    monkeypatch.setattr(PILImage.Image, "save", flaky)
    p = tmp_path / "x.jpg"
    fileio.save_image(str(p), img)
    assert p.exists() and calls["n"] >= 2


def test_jpeg_size_reasonable(tmp_path, img):
    p_in, p_out = tmp_path / "in.jpg", tmp_path / "out.jpg"
    Image.fromarray(img).save(p_in, quality=92)
    arr, meta = fileio.read_image(str(p_in))
    fileio.save_image(str(p_out), core.enhance(arr), meta)
    assert os.path.getsize(p_out) < 2.0 * os.path.getsize(p_in)


# ---------------------------------------------------------------- v1.2
def test_duplicates_and_old_outputs_skipped(tmp_path, img):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    Image.fromarray(img).save(src / "DSCI0001.JPG", quality=90)
    (src / "DSCI0001 2.JPG").write_bytes((src / "DSCI0001.JPG").read_bytes())
    Image.fromarray(img[::-1]).save(src / "DSCI0002.JPG", quality=90)
    Image.fromarray(img).save(src / "DSCI0003_enhanced.JPG", quality=90)
    job = BatchJob(BatchOptions(src=str(src), dst=str(dst)))
    job.run()
    s = job.summary()
    assert s["ok"] == 2 and s["duplicates"] == 1, s
    st = {i["rel"]: (i["status"], i["message"]) for i in s["items"]}
    assert st["DSCI0001 2.JPG"] == ("ignoré", "doublon de DSCI0001.JPG")
    assert st["DSCI0003_enhanced.JPG"][0] == "ignoré"
    assert not (dst / "DSCI0001 2.JPG").exists()
    # option désactivée : la copie est traitée
    job2 = BatchJob(BatchOptions(src=str(src), dst=str(tmp_path / "d2"), dedupe=False))
    job2.run()
    assert job2.summary()["ok"] == 3


def test_report_written_during_run(tmp_path, img):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    for i in range(3):
        Image.fromarray(np.roll(img, i * 7, axis=1)).save(src / f"p{i}.jpg", quality=90)
    job = BatchJob(BatchOptions(src=str(src), dst=str(dst), workers=1))
    seen = []
    orig = job._do_image

    def spy(it):
        orig(it)
        rep = (dst / "aquimage_rapport.csv").read_text(encoding="utf-8-sig")
        seen.append(it.rel in rep)

    job._do_image = spy
    job.run()
    assert all(seen) and len(seen) == 3


def test_open_water_no_vibrance():
    rng = np.random.default_rng(1)
    img = np.clip(np.array([25, 60, 160], np.float32) + rng.normal(0, 3, (300, 450, 3)), 0, 255).astype(np.uint8)
    s, diag = autotune.auto_settings(img)
    assert "pleine eau" in diag["type"] and s.vibrance == 0 and s.stretch == 1.0
    out = core.enhance(img, s)
    lab_in = cv2_lab(img)
    lab_out = cv2_lab(out)
    assert np.hypot(lab_out[..., 1], lab_out[..., 2]).mean() <= np.hypot(lab_in[..., 1], lab_in[..., 2]).mean() + 2


def cv2_lab(x):
    import cv2
    lab = cv2.cvtColor(x, cv2.COLOR_RGB2LAB).astype(np.float32)
    lab[..., 1:] -= 128
    return lab


def test_black_point_kept(img):
    s = autotune.auto_settings(img)[0]
    out = core.enhance(img, s)
    p1 = lambda x: np.percentile(x.astype(float) @ [0.299, 0.587, 0.114], 1)
    assert p1(out) > 0.4 * p1(img)


# ---------------------------------------------------------------- v1.3 : rotation et langues
def test_rotate90():
    a = np.arange(2 * 3 * 3, dtype=np.uint8).reshape(2, 3, 3)
    r = core.rotate90(a, 1)                       # sens horaire
    assert r.shape == (3, 2, 3) and r.flags.c_contiguous
    assert (r[0, -1] == a[0, 0]).all()            # coin haut-gauche -> haut-droit
    assert (core.rotate90(a, 4) == a).all() and (core.rotate90(core.rotate90(a, 1), -1) == a).all()


def test_server_rotation_and_lang(tmp_path, img):
    from http.server import ThreadingHTTPServer
    from aquimage import server
    server.CONFIG_PATH = str(tmp_path / "cfg.json")
    server.APP.config = {}
    Image.fromarray(img).save(tmp_path / "x.jpg")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def post(path, body):
        req = urllib.request.Request(base + path, json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        return urllib.request.urlopen(req, timeout=60)

    try:
        h, w = img.shape[:2]
        d = json.loads(post("/api/open", {"path": str(tmp_path / "x.jpg")}).read())
        assert d["rot"] == 0 and d["diag"]["kind"] in ("underwater", "light", "balanced", "red")
        d = json.loads(post("/api/rotate", {"id": d["id"], "rot": 1}).read())
        assert d["rot"] == 1
        before = Image.open(urllib.request.urlopen(f"{base}/api/before?id={d['id']}"))
        assert before.size[0] < before.size[1]            # portrait après rotation
        r = Image.open(post("/api/render", {"id": d["id"], "settings": d["settings"]}))
        assert r.size == before.size
        out = json.loads(post("/api/save", {"id": d["id"], "settings": d["settings"],
                                            "dest": str(tmp_path / "out")}).read())
        with Image.open(out["out"]) as im:
            assert im.size == (h, w)
            assert im.getexif().get(0x0112, 1) == 1         # orientation EXIF neutre
        post("/api/config", {"lang": "de"})
        assert json.loads(urllib.request.urlopen(base + "/api/info").read())["config"]["lang"] == "de"
    finally:
        httpd.shutdown()


@pytest.mark.skipif(not video.ffmpeg_exe(), reason="ffmpeg absent")
def test_video_rotated(tmp_path):
    ff = video.ffmpeg_exe()
    src = str(tmp_path / "r.mp4")
    subprocess.run([ff, "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc2=size=320x240:rate=10:duration=1", "-pix_fmt", "yuv420p", src], check=True)
    dst = str(tmp_path / "o.mp4")
    info = video.process_video(src, dst, Settings(), rotate=3)
    assert (info["width"], info["height"]) == (240, 320)
    meta = video.probe(dst)
    assert (meta["width"], meta["height"]) == (240, 320)


def test_i18n_keys_complete():
    """Chaque clé utilisée par l'interface existe dans les quatre langues."""
    import re
    web = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "aquimage", "web")
    src = open(os.path.join(web, "i18n.js"), encoding="utf-8").read()
    langs = {}
    for code in ("fr", "en", "es", "de"):
        m = re.search(r"\n" + code + r": \{(.*?)\n\},", src, re.S)
        assert m, code
        langs[code] = set(re.findall(r'"([\w.]+)":', m.group(1)))
    assert langs["fr"] == langs["en"] == langs["es"] == langs["de"]
    html = open(os.path.join(web, "index.html"), encoding="utf-8").read()
    js = open(os.path.join(web, "app.js"), encoding="utf-8").read()
    used = set(re.findall(r'data-i18n(?:-\w+)?="([\w.]+)"', html))
    used |= set(re.findall(r'\bTP?\("([\w.]+)"', js))
    used |= {k + s for k in re.findall(r'\bTP\("([\w.]+)"', js) for s in ("_one", "_other")}
    used -= set(re.findall(r'\bTP\("([\w.]+)"', js))
    used |= set(re.findall(r'"((?:sl|adj|g|st|a)\.\w+)"', js))
    used |= {"kind." + k for k in ("underwater", "light", "balanced", "red", "fixed")}
    used |= {"scene." + k for k in ("backlit", "openwater", "antiviolet")}
    missing = used - langs["fr"]
    assert not missing, missing


# ---------------------------------------------------------------- v1.4 : filtres
def test_list_media_done_flag(tmp_path, img):
    from aquimage import server
    for n in ("a.jpg", "b.jpg"):
        Image.fromarray(img).save(tmp_path / n)
    (tmp_path / "c.mp4").write_bytes(b"")
    out = tmp_path / "AquImage"
    out.mkdir()
    Image.fromarray(img).save(out / "a.tif")          # sortie dans un autre format
    files = {f["name"]: f for f in server.list_media(str(tmp_path), str(out))}
    assert files["a.jpg"]["done"] and not files["b.jpg"]["done"]
    assert files["c.mp4"]["kind"] == "video" and not files["c.mp4"]["done"]
    assert not any(f["done"] for f in server.list_media(str(tmp_path), ""))
