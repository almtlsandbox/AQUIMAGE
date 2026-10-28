"use strict";
/* AquImage — interface locale. Aucun appel extérieur : tout passe par l'API
   du serveur Python lancé sur 127.0.0.1. */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const fmtNum = (v, d = 2) => Number(v).toFixed(d).replace(".", ",").replace(/^-0,0+$/, "0");

async function api(path, body) {
  const opt = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  };
  const r = await fetch(path, opt);
  const ct = r.headers.get("Content-Type") || "";
  if (!r.ok) {
    let msg = r.statusText;
    if (ct.includes("json")) { try { msg = (await r.json()).error || msg; } catch (e) {} }
    throw new Error(msg);
  }
  return ct.includes("json") ? r.json() : r.blob();
}
const thumbUrl = (p, w = 240) => `/api/thumb?w=${w}&path=${encodeURIComponent(p)}`;
const joinPath = (dir, rel) => dir.replace(/[\\/]+$/, "") + (dir.includes("\\") ? "\\" : "/") + rel;

let INFO = null;

/* ---------------------------------------------------------------- onglets */
function showView(name) {
  $$(".tabs button").forEach(b => b.setAttribute("aria-selected", String(b.dataset.view === name)));
  $("#view-lot").hidden = name !== "lot";
  $("#view-atelier").hidden = name !== "atelier";
  if (name === "atelier") Viewer.fit();
}
$$(".tabs button").forEach(b => b.addEventListener("click", () => showView(b.dataset.view)));

/* ------------------------------------------------------ sélecteur de dossier */
const Picker = {
  dlg: $("#picker"), cur: "", resolve: null,
  async open(start) {
    this.dlg.showModal();
    await this.go(start || "");
    return new Promise(res => { this.resolve = res; });
  },
  async go(path) {
    try {
      const d = await api(`/api/browse?path=${encodeURIComponent(path)}`);
      this.cur = d.path;
      $("#pk-path").value = d.path;
      $("#pk-up").disabled = !d.path;
      $("#pk-up").dataset.to = d.parent || "";
      const ul = $("#pk-list");
      ul.innerHTML = "";
      for (const it of d.dirs) {
        const li = document.createElement("li");
        li.tabIndex = 0;
        li.textContent = it.name;
        li.title = it.path;
        li.addEventListener("click", () => this.go(it.path));
        li.addEventListener("keydown", e => { if (e.key === "Enter") this.go(it.path); });
        ul.appendChild(li);
      }
      $("#pk-count").textContent = d.path
        ? countText(d.images, d.videos, "dans ce dossier")
        : "Choisissez un lecteur ou un dossier récent.";
      $("#pk-ok").disabled = !d.path;
      $("#pk-new").disabled = !d.path;
    } catch (e) {
      $("#pk-count").textContent = e.message;
    }
  },
  done(val) { this.dlg.close(); if (this.resolve) this.resolve(val); this.resolve = null; },
};
$("#pk-up").addEventListener("click", () => Picker.go($("#pk-up").dataset.to || ""));
$("#pk-path").addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); Picker.go(e.target.value); } });
$("#pk-ok").addEventListener("click", () => Picker.done(Picker.cur));
$("#pk-new").addEventListener("click", () => {
  const name = prompt("Nom du nouveau dossier");
  if (name) Picker.done(joinPath(Picker.cur, name.trim()));
});
Picker.dlg.addEventListener("close", () => { if (Picker.resolve) { Picker.resolve(null); Picker.resolve = null; } });

$$("[data-pick]").forEach(b => b.addEventListener("click", async () => {
  const input = document.getElementById(b.dataset.pick);
  const p = await Picker.open(input.value || "");
  if (p) { input.value = p; input.dispatchEvent(new Event("change")); }
}));

function countText(img, vid, where = "") {
  if (!img && !vid) return `Aucune photo ni vidéo ${where}`.trim() + ".";
  const parts = [];
  if (img) parts.push(`${img} photo${img > 1 ? "s" : ""}`);
  if (vid) parts.push(`${vid} vidéo${vid > 1 ? "s" : ""}`);
  return `${parts.join(" et ")} ${where}`.trim() + ".";
}

/* ---------------------------------------------------------------- réglages */
const SLIDERS = [
  { group: "Couleur", items: [
    ["red_comp", "Compensation du rouge", 0, 2, 0.05],
    ["color", "Correction de la dominante", 0, 1, 0.05],
    ["water_keep", "Bleu gardé dans l'eau lointaine", 0, 1, 0.05],
    ["temperature", "Température (froid à chaud)", -1, 1, 0.05],
    ["tint", "Teinte (vert à magenta)", -1, 1, 0.05],
  ]},
  { group: "Voile", items: [
    ["strength", "Retrait du voile", 0, 1, 0.05],
    ["t_min", "Protection contre le bruit au loin", 0.05, 0.6, 0.01],
  ]},
  { group: "Tonalité", items: [
    ["exposure", "Luminosité", -2, 2, 0.05],
    ["shadows", "Éclaircir les ombres", 0, 1, 0.05],
    ["contrast", "Contraste local", 0, 1, 0.05],
    ["vibrance", "Vibrance (couleurs ternes)", 0, 1, 0.05],
    ["saturation", "Saturation", 0, 2, 0.05],
  ]},
  { group: "Réglages experts", advanced: true, items: [
    ["max_gain", "Gain de couleur maximal", 1, 12, 0.1],
    ["stretch", "Étirement maximal des niveaux", 1, 4, 0.1],
    ["omega", "Part du voile estimée (ω)", 0.5, 0.98, 0.01],
    ["black_clip", "Noirs écrêtés (%)", 0, 5, 0.1],
    ["white_clip", "Blancs écrêtés (%)", 0, 5, 0.1],
  ]},
];
const ADJUST = [
  ["temperature", "Température", -0.5, 0.5],
  ["tint", "Teinte", -0.5, 0.5],
  ["exposure", "Luminosité", -1, 1],
  ["shadows", "Ombres", -0.3, 0.3],
  ["saturation", "Saturation", -0.5, 0.5],
  ["contrast", "Contraste local", -0.3, 0.3],
  ["strength", "Retrait du voile", -0.4, 0.4],
  ["water_keep", "Bleu gardé au loin", -0.4, 0.4],
];

function sliderEl(key, label, min, max, step, value, onInput) {
  const wrap = document.createElement("div");
  wrap.className = "sl";
  wrap.dataset.key = key;
  const id = "s-" + key + "-" + Math.random().toString(36).slice(2, 7);
  wrap.innerHTML = `<div class="row"><span><label for="${id}">${label}</label></span><output></output></div>
    <input type="range" id="${id}" min="${min}" max="${max}" step="${step}" value="${value}">`;
  const input = $("input", wrap), out = $("output", wrap);
  const show = () => { out.textContent = fmtNum(input.value, step < 0.05 ? 2 : (step >= 1 ? 0 : 2)); };
  input.addEventListener("input", () => { show(); onInput(key, Number(input.value)); });
  show();
  wrap.set = v => { input.value = v; show(); };
  return wrap;
}

/* ===================================================================== LOT */
const Lot = {
  timer: null, rows: [],
  init() {
    const box = $("#adjust-sliders");
    for (const [k, label, min, max] of ADJUST) {
      box.appendChild(sliderEl(k, label, min, max, 0.05, 0, () => {}));
    }
    $("#quality").addEventListener("input", e => { $("#quality-v").textContent = e.target.value; });
    $("#src").addEventListener("change", () => this.count());
    $("#lot-form").addEventListener("submit", e => { e.preventDefault(); this.start(); });
    $("#stop").addEventListener("click", () => api("/api/batch/cancel", {}));
    $$("input[name=mode]").forEach(r => r.addEventListener("change", () => this.modeHint()));
    $("#items").addEventListener("click", e => {
      const li = e.target.closest("li[data-rel]");
      if (li && $("#src").value) Atelier.openFile(joinPath($("#src").value, li.dataset.rel), true);
    });
  },
  modeHint() {
    const fixed = $("input[name=mode]:checked").value === "fixe";
    $("#fixed-hint").hidden = !fixed;
    $("#adjust").hidden = fixed;
    $("#fixed-hint").textContent = Atelier.settings
      ? `Réglages repris de « ${Atelier.sess ? Atelier.sess.name : "l'atelier"} ».`
      : "Ouvrez d'abord une photo dans l'atelier et réglez-la.";
  },
  async count() {
    const p = $("#src").value.trim();
    if (!p) { $("#src-count").textContent = ""; return; }
    try {
      const d = await api(`/api/browse?path=${encodeURIComponent(p)}`);
      let txt = countText(d.images, d.videos, "dans ce dossier");
      if (d.dirs.length) txt += ` Sous-dossiers : ${d.dirs.length}.`;
      $("#src-count").textContent = txt;
      if (!$("#dst").value) $("#dst").value = joinPath(p, "AquImage");
    } catch (e) { $("#src-count").textContent = e.message; }
  },
  async start() {
    $("#lot-error").textContent = "";
    const mode = $("input[name=mode]:checked").value;
    if (mode === "fixe" && !Atelier.settings) {
      $("#lot-error").textContent = "Ouvrez une photo dans l'atelier pour définir les réglages communs.";
      return;
    }
    const adjust = {};
    $$("#adjust-sliders .sl").forEach(s => { const v = Number($("input", s).value); if (v) adjust[s.dataset.key] = v; });
    const body = {
      src: $("#src").value.trim(), dst: $("#dst").value.trim(),
      recursive: $("#recursive").checked, videos: $("#videos").checked,
      overwrite: $("#overwrite").checked, dedupe: $("#dedupe").checked, fmt: $("#fmt").value,
      quality: Number($("#quality").value), mode, adjust,
      fixed: mode === "fixe" ? Atelier.settings : null,
    };
    if (!body.src || !body.dst) { $("#lot-error").textContent = "Indiquez les deux dossiers."; return; }
    try {
      const s = await api("/api/batch/start", body);
      this.rows = [];
      $("#items").innerHTML = "";
      this.render(s);
      this.poll();
    } catch (e) { $("#lot-error").textContent = e.message; }
  },
  poll() {
    clearTimeout(this.timer);
    this.timer = setTimeout(async () => {
      try {
        const s = await api("/api/batch/status");
        this.render(s);
        if (s.state === "en cours") this.poll();
      } catch (e) { this.poll(); }
    }, 900);
  },
  render(s) {
    if (!s || s.state === "aucun") return;
    $("#lot-empty").hidden = true;
    $("#lot-run").hidden = false;
    const running = s.state === "en cours";
    $("#run").hidden = running;
    $("#stop").hidden = !running;
    $("#run-bar").style.width = (100 * s.fraction).toFixed(1) + "%";
    let sum;
    if (running) sum = `${s.done} sur ${s.total} fichiers traités`;
    else if (s.state === "terminé") sum = `Terminé : ${s.ok} corrigé${s.ok > 1 ? "s" : ""}`
      + (s.duplicates ? `, ${s.duplicates} doublon${s.duplicates > 1 ? "s" : ""} ignoré${s.duplicates > 1 ? "s" : ""}` : "")
      + (s.skipped - (s.duplicates || 0) > 0 ? `, ${s.skipped - s.duplicates} autre${s.skipped - s.duplicates > 1 ? "s" : ""} ignoré${s.skipped - s.duplicates > 1 ? "s" : ""}` : "")
      + (s.errors ? `, ${s.errors} en erreur` : "") + ".";
    else if (s.state === "annulé") sum = `Arrêté après ${s.done} fichier${s.done > 1 ? "s" : ""}.`;
    else sum = `Erreur : ${s.error}`;
    $("#run-summary").textContent = sum;
    $("#run-eta").textContent = running && s.eta != null ? `encore ${dur(s.eta)} environ`
      : (!running && s.elapsed ? `en ${dur(s.elapsed)}, rapport dans le dossier de sortie` : "");
    const ul = $("#items");
    s.items.forEach((it, i) => {
      let li = this.rows[i];
      if (!li) {
        li = document.createElement("li");
        li.className = "item";
        li.dataset.rel = it.rel;
        li.title = "Ouvrir dans l'atelier";
        li.innerHTML = `<img class="th" alt="" loading="lazy"><img class="th none" alt="" loading="lazy">
          <div><div class="name"></div><div class="sub"></div><div class="mini" hidden><div></div></div></div><div class="st"></div>`;
        $(".th", li).src = thumbUrl(joinPath(s.src, it.rel), 192);
        $(".name", li).textContent = it.rel;
        ul.appendChild(li);
        this.rows[i] = li;
      }
      const st = $(".st", li);
      const LABEL = { ok: "Corrigé", "ignoré": "Ignoré", erreur: "Erreur", "annulé": "Arrêté", "en attente": "En attente" };
      st.textContent = it.status === "en cours" ? `${Math.round(it.progress * 100)} %` : (LABEL[it.status] || it.status);
      st.className = "st " + ({ ok: "ok", erreur: "err", "en cours": "run", "ignoré": "skip" }[it.status] || "");
      $(".sub", li).textContent = [it.kind === "video" ? "Vidéo" : "", it.type, it.message,
        it.status === "ok" && it.seconds ? dur(it.seconds) : ""].filter(Boolean).join(", ");
      const mini = $(".mini", li);
      mini.hidden = it.status !== "en cours";
      $("div", mini).style.width = (it.progress * 100) + "%";
      const after = $$(".th", li)[1];
      if (it.status === "ok" && it.out && after.classList.contains("none")) {
        after.src = thumbUrl(it.out, 192);
        after.classList.remove("none");
      }
    });
  },
};
function dur(sec) {
  sec = Math.round(sec);
  if (sec < 60) return `${sec} s`;
  const m = Math.floor(sec / 60), s = sec % 60;
  if (m < 60) return `${m} min ${s ? s + " s" : ""}`.trim();
  return `${Math.floor(m / 60)} h ${m % 60} min`;
}

/* ================================================================ VISIONNEUSE */
const Viewer = {
  el: $("#viewer"), stage: $("#stage"), before: $("#img-before"), after: $("#img-after"),
  w: 0, h: 0, s: 1, tx: 0, ty: 0, split: 0.5, fitted: true, showBefore: false,
  init() {
    const el = this.el;
    el.addEventListener("wheel", e => {
      if (!this.w) return;
      e.preventDefault();
      const r = el.getBoundingClientRect();
      const mx = e.clientX - r.left, my = e.clientY - r.top;
      const k = Math.exp(-e.deltaY * 0.0015);
      const ns = Math.min(Math.max(this.s * k, this.fitScale() * 0.5), 8);
      this.tx = mx - (mx - this.tx) * ns / this.s;
      this.ty = my - (my - this.ty) * ns / this.s;
      this.s = ns; this.fitted = false; this.apply();
    }, { passive: false });
    let drag = null;
    el.addEventListener("pointerdown", e => {
      if (!this.w) return;
      el.setPointerCapture(e.pointerId);
      const r = el.getBoundingClientRect();
      const onSplit = e.target.closest("#split");
      drag = onSplit ? { split: true, r } : { x: e.clientX, y: e.clientY, tx: this.tx, ty: this.ty };
      if (!onSplit) el.classList.add("panning");
    });
    el.addEventListener("pointermove", e => {
      if (!drag) return;
      if (drag.split) {
        this.split = Math.min(1, Math.max(0, (e.clientX - drag.r.left) / drag.r.width));
      } else {
        this.tx = drag.tx + e.clientX - drag.x; this.ty = drag.ty + e.clientY - drag.y; this.fitted = false;
      }
      this.apply();
    });
    const end = () => { drag = null; el.classList.remove("panning"); };
    el.addEventListener("pointerup", end);
    el.addEventListener("pointercancel", end);
    el.addEventListener("dblclick", () => this.fit());
    window.addEventListener("resize", () => { if (this.fitted) this.fit(); else this.apply(); });
    document.addEventListener("keydown", e => {
      if (e.code === "Space" && !$("#view-atelier").hidden && !e.target.matches("input,select,textarea,button")) {
        e.preventDefault(); this.showBefore = true; this.apply();
      }
      if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
        if (!$("#view-atelier").hidden && !e.target.matches("input,select,textarea")) Atelier.step(e.key === "ArrowRight" ? 1 : -1);
      }
    });
    document.addEventListener("keyup", e => { if (e.code === "Space") { this.showBefore = false; this.apply(); } });
  },
  setSize(w, h) {
    const changed = w !== this.w || h !== this.h;
    this.w = w; this.h = h;
    for (const im of [this.before, this.after]) { im.width = w; im.height = h; }
    this.el.classList.toggle("has-image", w > 0);
    if (changed) this.fit();
  },
  fitScale() {
    const r = this.el.getBoundingClientRect();
    return this.w ? Math.min(r.width / this.w, r.height / this.h) : 1;
  },
  fit() {
    if (!this.w) return;
    const r = this.el.getBoundingClientRect();
    this.s = this.fitScale();
    this.tx = (r.width - this.w * this.s) / 2;
    this.ty = (r.height - this.h * this.s) / 2;
    this.fitted = true; this.apply();
  },
  apply() {
    const r = this.el.getBoundingClientRect();
    this.stage.style.transform = `translate(${this.tx}px, ${this.ty}px) scale(${this.s})`;
    const sx = this.split * r.width;
    const ix = Math.min(this.w, Math.max(0, (sx - this.tx) / this.s));
    this.after.style.clipPath = this.showBefore ? "inset(0 0 0 100%)" : `inset(0 0 0 ${ix}px)`;
    $("#split").style.left = sx + "px";
    $("#split").hidden = this.showBefore;
    const full = Atelier.sess && Atelier.sess.info.width ? Atelier.sess.info.width : this.w;
    $("#zoom-v").textContent = this.fitted ? "Ajusté à la fenêtre" : `Zoom ${Math.round(this.s * this.w / full * 100)} %`;
  },
};

/* ================================================================== ATELIER */
const Atelier = {
  sess: null, settings: null, auto: null, dir: "", files: [], seq: 0, inflight: false, pending: false,
  els: {},
  init() {
    const box = $("#sliders");
    for (const g of SLIDERS) {
      const sec = document.createElement("div");
      sec.className = "group";
      let host = sec;
      if (g.advanced) {
        const det = document.createElement("details");
        det.innerHTML = `<summary>${g.group}</summary>`;
        sec.appendChild(det); host = det;
      } else {
        sec.innerHTML = `<h3>${g.group}</h3>`;
      }
      for (const [k, label, min, max, step] of g.items) {
        const el = sliderEl(k, label, min, max, step, (INFO.defaults[k] ?? 0), (key, v) => this.change(key, v));
        $("input", el).disabled = true;
        el.addEventListener("dblclick", () => { if (this.auto) { this.change(k, this.auto[k]); el.set(this.auto[k]); } });
        $(".row span", el).title = "Double-clic : revenir à la valeur automatique";
        this.els[k] = el;
        host.appendChild(el);
      }
      box.appendChild(sec);
    }
    $("#strip-pick").addEventListener("click", () => this.pickDir());
    $("#viewer-pick").addEventListener("click", () => this.pickDir());
    $("#btn-auto").addEventListener("click", () => this.resetAuto());
    $("#btn-save").addEventListener("click", () => this.save());
    $("#btn-tolot").addEventListener("click", () => {
      $("input[name=mode][value=fixe]").checked = true;
      Lot.modeHint(); showView("lot");
    });
    $("#vpos").addEventListener("change", e => this.frame(Number(e.target.value)));
    $("#thumbs").addEventListener("click", e => {
      const li = e.target.closest("li[data-path]");
      if (li) this.openFile(li.dataset.path);
    });
  },
  async pickDir() {
    const p = await Picker.open(this.dir || (INFO.config.atelier || ""));
    if (p) this.loadDir(p);
  },
  async loadDir(dir) {
    try {
      const d = await api(`/api/files?dir=${encodeURIComponent(dir)}`);
      this.dir = dir; this.files = d.files;
      $("#strip-dir").textContent = dir.split(/[\\/]/).filter(Boolean).pop() || dir; $("#strip-dir").title = dir;
      const ul = $("#thumbs"); ul.innerHTML = "";
      if (!d.files.length) ul.innerHTML = `<li class="hint">Aucune photo ni vidéo ici.</li>`;
      for (const f of d.files) {
        const li = document.createElement("li");
        li.dataset.path = f.path; li.title = f.name;
        li.innerHTML = `<img alt="${f.name.replace(/"/g, "")}" loading="lazy">` + (f.kind === "video" ? `<span class="vid">Vidéo</span>` : "");
        $("img", li).src = thumbUrl(f.path, 240);
        ul.appendChild(li);
      }
      if (!$("#a-dst").value) $("#a-dst").value = joinPath(dir, "AquImage");
    } catch (e) { $("#strip-dir").textContent = e.message; }
  },
  async openFile(path, switchView) {
    if (switchView) showView("atelier");
    const dir = path.replace(/[\\/][^\\/]*$/, "");
    if (dir !== this.dir) await this.loadDir(dir);
    $$("#thumbs li").forEach(li => li.setAttribute("aria-current", String(li.dataset.path === path)));
    const cur = $(`#thumbs li[aria-current="true"]`); if (cur) cur.scrollIntoView({ block: "nearest" });
    $("#p-name").textContent = path.split(/[\\/]/).pop();
    $("#p-diag").textContent = "Analyse de l'image…";
    $("#busy").hidden = false;
    try {
      const d = await api("/api/open", { path });
      this.setSession(d, true);
    } catch (e) {
      $("#p-diag").textContent = e.message; $("#busy").hidden = true;
    }
  },
  step(delta) {
    if (!this.sess || !this.files.length) return;
    const i = this.files.findIndex(f => f.path === this.sess.path);
    const n = this.files[i + delta];
    if (n) this.openFile(n.path);
  },
  setSession(d, fresh) {
    this.sess = d;
    this.settings = { ...d.settings };
    this.auto = { ...d.settings };
    for (const [k, el] of Object.entries(this.els)) { el.set(this.settings[k]); $("input", el).disabled = false; el.classList.remove("changed"); }
    const diag = d.diag || {};
    const sz = d.kind === "video"
      ? `Vidéo ${d.info.width}×${d.info.height}, ${dur(d.info.duration)}`
      : `${d.info.width}×${d.info.height}${d.info.bits === 16 ? ", 16 bits" : ""}`;
    $("#p-diag").textContent = `${sz}. Analyse : ${diag.type || "?"}.`;
    $("#btn-auto").disabled = false; $("#btn-save").disabled = false; $("#btn-tolot").disabled = false;
    $("#btn-save").textContent = d.kind === "video" ? "Exporter la vidéo" : "Enregistrer";
    $("#vpos-wrap").hidden = d.kind !== "video";
    $("#vpos").value = d.pos;
    $("#save-msg").textContent = "";
    this.loadBefore(fresh);
    this.render();
  },
  loadBefore(fresh) {
    const im = Viewer.before;
    im.onload = () => { Viewer.setSize(im.naturalWidth, im.naturalHeight); if (fresh) Viewer.fit(); };
    im.src = `/api/before?id=${this.sess.id}&t=${Date.now()}`;
  },
  change(key, v) {
    if (!this.settings) return;
    this.settings[key] = v;
    const el = this.els[key];
    el.classList.toggle("changed", Math.abs(v - this.auto[key]) > 1e-6);
    this.render();
  },
  async render() {
    if (!this.sess) return;
    if (this.inflight) { this.pending = true; return; }
    this.inflight = true; this.pending = false;
    $("#busy").hidden = false;
    try {
      const blob = await api("/api/render", { id: this.sess.id, settings: this.settings });
      const url = URL.createObjectURL(blob);
      const old = Viewer.after.src;
      Viewer.after.src = url;
      if (old && old.startsWith("blob:")) setTimeout(() => URL.revokeObjectURL(old), 2000);
    } catch (e) {
      $("#save-msg").textContent = e.message;
    }
    this.inflight = false;
    if (this.pending) this.render(); else $("#busy").hidden = true;
  },
  async resetAuto() {
    if (!this.sess) return;
    $("#p-diag").textContent = "Nouvelle analyse…";
    const d = await api("/api/auto", { id: this.sess.id, settings: {} });
    this.setSession(d, false);
  },
  async frame(pos) {
    if (!this.sess) return;
    $("#busy").hidden = false;
    const d = await api("/api/frame", { id: this.sess.id, pos });
    this.sess.pos = d.pos;
    this.loadBefore(false);
    this.render();
  },
  async save() {
    const msg = $("#save-msg");
    msg.textContent = this.sess.kind === "video" ? "Export de la vidéo…" : "Enregistrement en pleine résolution…";
    $("#btn-save").disabled = true;
    try {
      const r = await api("/api/save", { id: this.sess.id, settings: this.settings,
        dest: $("#a-dst").value.trim(), fmt: $("#a-fmt").value });
      if (r.task) {
        $("#btn-save").disabled = false;
        await this.followTask(r.task, r.out);
      } else {
        msg.textContent = `Enregistrée : ${r.out}`;
      }
    } catch (e) { msg.textContent = e.message; }
    $("#btn-save").disabled = false;
  },
  async followTask(id, out) {
    const msg = $("#save-msg");
    for (;;) {
      const t = await api(`/api/task?id=${id}`);
      if (t.state === "en cours") msg.textContent = `Export de la vidéo : ${Math.round(t.progress * 100)} %`;
      else { msg.textContent = t.state === "terminé" ? `Vidéo exportée : ${out}` : `Export ${t.state}${t.message ? " : " + t.message : ""}`; return; }
      await new Promise(r => setTimeout(r, 1000));
    }
  },
};

/* ================================================================ démarrage */
(async function main() {
  INFO = await api("/api/info");
  if (!INFO.ffmpeg) $("#top-note").textContent = "ffmpeg introuvable : les vidéos seront exportées sans le son.";
  Lot.init();
  Viewer.init();
  Atelier.init();
  const c = INFO.config || {};
  if (c.src) { $("#src").value = c.src; Lot.count(); }
  if (c.dst) $("#dst").value = c.dst;
  if (c.atelier_dst) $("#a-dst").value = c.atelier_dst;
  if (c.atelier) Atelier.loadDir(c.atelier);
  try { const s = await api("/api/batch/status"); if (s.state && s.state !== "aucun") { Lot.render(s); if (s.state === "en cours") Lot.poll(); } } catch (e) {}
})();
