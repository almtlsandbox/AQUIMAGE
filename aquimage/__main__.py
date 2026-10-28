"""
AquImage en ligne de commande.

    python -m aquimage                       interface (ouvre le navigateur)
    python -m aquimage lot SOURCE DEST       corrige tout un dossier
    python -m aquimage image ENTREE SORTIE   corrige une image
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from . import __version__


def _cmd_lot(a) -> int:
    from .batch import BatchJob, BatchOptions
    adjust = {}
    for kv in a.ajuste or []:
        k, _, v = kv.partition("=")
        adjust[k.strip()] = float(v)
    job = BatchJob(BatchOptions(
        src=a.source, dst=a.dest, recursive=not a.sans_sous_dossiers,
        videos=not a.sans_videos, fmt=a.format, quality=a.qualite,
        overwrite=a.remplacer, suffix=a.suffixe, workers=a.workers,
        dedupe=not a.garder_doublons,
        adjust=adjust))
    n = len(job.items)
    if not n:
        print("Aucune image ni vidéo dans", a.source)
        return 1
    print(f"{n} fichier(s) à corriger -> {a.dest}")
    job.start()
    last = ""
    try:
        while job.state in ("prêt", "en cours"):
            time.sleep(1)
            s = job.summary()
            line = f"\r{s['done']}/{s['total']}  {100 * s['fraction']:5.1f} %"
            if s["eta"] is not None:
                line += f"  reste ~{s['eta']} s"
            if line != last:
                sys.stdout.write(line + "   ")
                sys.stdout.flush()
                last = line
    except KeyboardInterrupt:
        job.cancel.set()
        print("\nArrêt demandé…")
        while job.state == "en cours":
            time.sleep(0.5)
    s = job.summary()
    print(f"\n{s['ok']} corrigé(s), {s['skipped']} ignoré(s) dont {s['duplicates']} doublon(s), "
          f"{s['errors']} erreur(s). "
          "Rapport :", os.path.join(a.dest, "aquimage_rapport.csv"))
    for it in s["items"]:
        if it["status"] == "erreur":
            print("  erreur :", it["rel"], "-", it["message"])
    return 0 if not s["errors"] else 2


def _cmd_image(a) -> int:
    from . import autotune, core, fileio
    img, meta = fileio.read_image(a.entree)
    s, diag = autotune.auto_settings(img)
    print("Analyse :", diag)
    out = core.apply_plan(img, core.build_plan(img, s))
    fileio.save_image(a.sortie, out, meta, a.qualite)
    print("Enregistrée :", a.sortie)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="aquimage",
                                description=f"AquImage {__version__} : correction des photos et vidéos sous-marines.")
    p.add_argument("--version", action="version", version=f"AquImage {__version__}")
    sub = p.add_subparsers(dest="cmd")

    ui = sub.add_parser("ui", help="interface dans le navigateur (par défaut)")
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("--sans-navigateur", action="store_true")

    lot = sub.add_parser("lot", help="corriger tout un dossier")
    lot.add_argument("source")
    lot.add_argument("dest")
    lot.add_argument("--sans-sous-dossiers", action="store_true")
    lot.add_argument("--sans-videos", action="store_true")
    lot.add_argument("--format", choices=["same", "jpg", "png", "tif"], default="same")
    lot.add_argument("--qualite", type=int, default=92)
    lot.add_argument("--remplacer", action="store_true", help="refaire les fichiers déjà présents")
    lot.add_argument("--suffixe", default="")
    lot.add_argument("--workers", type=int, default=0)
    lot.add_argument("--garder-doublons", action="store_true",
                     help="traiter aussi les copies identiques (« X 2.JPG »)")
    lot.add_argument("--ajuste", action="append", metavar="REGLAGE=VALEUR",
                     help="ajustement commun, ex. --ajuste temperature=0.2")

    im = sub.add_parser("image", help="corriger une image")
    im.add_argument("entree")
    im.add_argument("sortie")
    im.add_argument("--qualite", type=int, default=92)

    a = p.parse_args(argv)
    if a.cmd == "lot":
        return _cmd_lot(a)
    if a.cmd == "image":
        return _cmd_image(a)
    from .server import serve
    serve(port=getattr(a, "port", 8765), open_browser=not getattr(a, "sans_navigateur", False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
