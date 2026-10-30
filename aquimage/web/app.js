"use strict";
/* AquImage — interface locale. Aucun appel extérieur : tout passe par l'API
   du serveur Python lancé sur 127.0.0.1. */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const fmtNum = (v, d = 2) => {
  const t = Number(v).toFixed(d).replace(/^-0\.0+$/, "0");
  return LANG === "en" ? t : t.replace(".", ",");
};

async function api(path, body) {
  const opt = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  };
  const r = await fetch(path, opt);
  const ct = r.headers.get("Content-Type") || "";
  if (!r.ok) {
    let msg = r.statusText;
    if (ct.includes("json")) { try { msg = (await r.json()).error || msg; } catch (e) {} }
    throw new Error(trMsg(msg));
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
        ? countText(d.images, d.videos, T("count.here"))
        : T("pk.roots");
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
  const name = prompt(T("pk.prompt"));
  if (name) Picker.done(joinPath(Picker.cur, name.trim()));
});
Picker.dlg.addEventListener("close", () => { if (Picker.resolve) { Picker.resolve(null); Picker.resolve = null; } });

$$("[data-pick]").forEach(b => b.addEventListener("click", async () => {
  const input = document.getElementById(b.dataset.pick);
  const p = await Picker.open(input.value || "");
  if (p) { input.value = p; input.dispatchEvent(new Event("change")); }
}));

function countText(img, vid, where = "") {
  if (!img && !vid) return `${T("count.none")} ${where}`.trim() + ".";
  const parts = [];
  if (img) parts.push(TP("count.photo", img));
  if (vid) parts.push(TP("count.video", vid));
  return `${parts.join(` ${T("count.and")} `)} ${where}`.trim() + ".";
}

/* ----------------------------------------------------------------- filtres */
/* Barres de filtres à boutons segmentés. state = {groupe: valeur}. */
const FILTERS = (() => {
  const def = { lot: { status: "all", kind: "all" }, strip: { hideDone: false, kind: "all" } };
  try {
    const v = JSON.parse(localStorage.getItem("aquimage.filters") || "null");
    if (v && v.lot && v.strip) {
      const st = { ...def.strip, kind: v.strip.kind || "all",
                   hideDone: v.strip.hideDone ?? v.strip.done === "todo" };
      return { lot: { ...def.lot, ...v.lot }, strip: st };
    }
  } catch (e) {}
  return def;
})();
function saveFilters() {
  try { localStorage.setItem("aquimage.filters", JSON.stringify(FILTERS)); } catch (e) {}
}
function initSeg(root, state, onChange) {
  $$(".seg", root).forEach(seg => seg.addEventListener("click", e => {
    const b = e.target.closest("button[data-f]");
    if (!b || b.disabled) return;
    state[seg.dataset.group] = b.dataset.f;
    saveFilters();
    onChange();
  }));
}
/* labels : {groupe: {valeur: clé i18n}} ; counts : {groupe: {valeur: n}} */
function paintSeg(root, state, labels, counts) {
  $$(".seg", root).forEach(seg => {
    const g = seg.dataset.group;
    $$("button", seg).forEach(b => {
      const f = b.dataset.f, n = counts[g][f] || 0;
      b.textContent = T(labels[g][f]);
      const span = document.createElement("span");
      span.className = "n"; span.textContent = n;
      b.appendChild(span);
      b.setAttribute("aria-pressed", String(state[g] === f));
      b.disabled = f !== "all" && n === 0 && state[g] !== f;
    });
  });
}
const KIND_LABELS = { all: "f.allkinds", image: "f.photos", video: "f.videos" };

/* ---------------------------------------------------------------- réglages */
const SLIDERS = [
  { group: "g.color", items: [
    ["red_comp", "sl.red_comp", 0, 2, 0.05],
    ["color", "sl.color", 0, 1, 0.05],
    ["water_keep", "sl.water_keep", 0, 1, 0.05],
    ["temperature", "sl.temperature", -1, 1, 0.05],
    ["tint", "sl.tint", -1, 1, 0.05],
  ]},
  { group: "g.haze", items: [
    ["strength", "sl.strength", 0, 1, 0.05],
    ["t_min", "sl.t_min", 0.05, 0.6, 0.01],
  ]},
  { group: "g.tone", items: [
    ["exposure", "sl.exposure", -2, 2, 0.05],
    ["shadows", "sl.shadows", 0, 1, 0.05],
    ["contrast", "sl.contrast", 0, 1, 0.05],
    ["vibrance", "sl.vibrance", 0, 1, 0.05],
    ["saturation", "sl.saturation", 0, 2, 0.05],
  ]},
  { group: "g.expert", advanced: true, items: [
    ["max_gain", "sl.max_gain", 1, 12, 0.1],
    ["stretch", "sl.stretch", 1, 4, 0.1],
    ["omega", "sl.omega", 0.5, 0.98, 0.01],
    ["black_clip", "sl.black_clip", 0, 5, 0.1],
    ["white_clip", "sl.white_clip", 0, 5, 0.1],
  ]},
];
const ADJUST = [
  ["temperature", "adj.temperature", -0.5, 0.5],
  ["tint", "adj.tint", -0.5, 0.5],
  ["exposure", "adj.exposure", -1, 1],
  ["shadows", "adj.shadows", -0.3, 0.3],
  ["saturation", "adj.saturation", -0.5, 0.5],
  ["contrast", "adj.contrast", -0.3, 0.3],
  ["strength", "adj.strength", -0.4, 0.4],
  ["water_keep", "adj.water_keep", -0.4, 0.4],
];

function sliderEl(key, label, min, max, step, value, onInput) {
  const wrap = document.createElement("div");
  wrap.className = "sl";
  wrap.dataset.key = key;
  const id = "s-" + key + "-" + Math.random().toString(36).slice(2, 7);
  wrap.innerHTML = `<div class="row"><span><label for="${id}" data-i18n="${label}">${T(label)}</label></span><output></output></div>
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
    $("#run-open").addEventListener("click", () => {
      if (this.last && this.last.dst) reveal(this.last.dst, $("#run-eta"));
    });
    $$("input[name=mode]").forEach(r => r.addEventListener("change", () => this.modeHint()));
    initSeg($("#lot-filters"), FILTERS.lot, () => this.applyFilter());
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
      ? T("lot.fixed_from", { name: Atelier.sess ? Atelier.sess.name : T("lot.atelier") })
      : T("lot.fixed_need");
  },
  async count() {
    const p = $("#src").value.trim();
    if (!p) { $("#src-count").textContent = ""; return; }
    try {
      const d = await api(`/api/browse?path=${encodeURIComponent(p)}`);
      let txt = countText(d.images, d.videos, T("count.here"));
      if (d.dirs.length) txt += " " + T("lot.subdirs", { n: d.dirs.length });
      $("#src-count").textContent = txt;
      if (!$("#dst").value) $("#dst").value = joinPath(p, "AquImage");
    } catch (e) { $("#src-count").textContent = e.message; }
  },
  async start() {
    $("#lot-error").textContent = "";
    const mode = $("input[name=mode]:checked").value;
    if (mode === "fixe" && !Atelier.settings) {
      $("#lot-error").textContent = T("lot.err_fixed");
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
    if (!body.src || !body.dst) { $("#lot-error").textContent = T("lot.err_dirs"); return; }
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
    this.last = s;
    $("#lot-empty").hidden = true;
    $("#lot-run").hidden = false;
    const running = s.state === "en cours";
    $("#run").hidden = running;
    $("#stop").hidden = !running;
    $("#run-bar").style.width = (100 * s.fraction).toFixed(1) + "%";
    let sum;
    const dups = s.duplicates || 0, others = s.skipped - dups;
    if (running) sum = T("run.progress", { done: s.done, total: s.total });
    else if (s.state === "terminé") sum = TP("run.done", s.ok)
      + (dups ? TP("run.dups", dups) : "")
      + (others > 0 ? TP("run.others", others) : "")
      + (s.errors ? T("run.errors", { n: s.errors }) : "") + ".";
    else if (s.state === "annulé") sum = TP("run.stopped", s.done);
    else sum = T("run.error", { e: trMsg(s.error) });
    $("#run-summary").textContent = sum;
    $("#run-eta").textContent = running && s.eta != null ? T("run.eta", { d: dur(s.eta) })
      : (!running && s.elapsed ? T("run.elapsed", { d: dur(s.elapsed) }) : "");
    const ul = $("#items");
    s.items.forEach((it, i) => {
      let li = this.rows[i];
      if (!li) {
        li = document.createElement("li");
        li.className = "item";
        li.dataset.rel = it.rel;
        li.dataset.i18nTitle = "item.open";
        li.title = T("item.open");
        li.innerHTML = `<img class="th" alt="" loading="lazy"><img class="th none" alt="" loading="lazy">
          <div><div class="name"></div><div class="sub"></div><div class="mini" hidden><div></div></div></div><div class="st"></div>`;
        $(".th", li).src = thumbUrl(joinPath(s.src, it.rel), 192);
        $(".name", li).textContent = it.rel;
        ul.appendChild(li);
        this.rows[i] = li;
      }
      const st = $(".st", li);
      const LABEL = { ok: "st.ok", "ignoré": "st.skip", erreur: "st.error", "annulé": "st.cancel", "en attente": "st.wait" };
      st.textContent = it.status === "en cours" ? `${Math.round(it.progress * 100)} %` : (LABEL[it.status] ? T(LABEL[it.status]) : it.status);
      st.className = "st " + ({ ok: "ok", erreur: "err", "en cours": "run", "ignoré": "skip" }[it.status] || "");
      $(".sub", li).textContent = [it.kind === "video" ? T("video") : "",
        it.type ? diagText({ ...(it.diag || {}), type: it.type }) : "", trMsg(it.message),
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
    this.applyFilter();
  },
  /* Groupe d'état d'un fichier du lot, pour le filtre. */
  group(st) {
    return st === "ok" ? "ok" : st === "erreur" ? "err"
      : (st === "ignoré" || st === "annulé") ? "skip" : "todo";
  },
  applyFilter() {
    const s = this.last;
    if (!s) return;
    const F = FILTERS.lot;
    const counts = { status: { all: 0, todo: 0, ok: 0, skip: 0, err: 0 }, kind: { all: 0, image: 0, video: 0 } };
    let shown = 0;
    s.items.forEach((it, i) => {
      const g = this.group(it.status);
      const okS = F.status === "all" || F.status === g;
      const okK = F.kind === "all" || F.kind === it.kind;
      // Nombres fixes : chaque bouton compte tous les fichiers de sa catégorie
      counts.status.all++; counts.status[g]++;
      counts.kind.all++; counts.kind[it.kind]++;
      const li = this.rows[i];
      if (li) { li.hidden = !(okS && okK); if (!li.hidden) shown++; }
    });
    paintSeg($("#lot-filters"), F, {
      status: { all: "f.all", todo: "f.todo", ok: "f.ok", skip: "f.skip", err: "f.err" }, kind: KIND_LABELS,
    }, counts);
    $("#lot-none").hidden = shown > 0 || !s.items.length;
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
      if ((e.key === "r" || e.key === "R") && !e.ctrlKey && !e.metaKey && !e.altKey
          && !$("#view-atelier").hidden && !e.target.matches("input,select,textarea")) {
        e.preventDefault(); Atelier.rotate(e.shiftKey ? -1 : 1);
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
    const ss = Atelier.sess;
    const full = ss && ss.info.width ? (ss.rot % 2 ? ss.info.height : ss.info.width) : this.w;
    $("#zoom-v").textContent = this.fitted ? T("zoom.fit") : T("zoom", { n: Math.round(this.s * this.w / full * 100) });
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
        det.innerHTML = `<summary data-i18n="${g.group}">${T(g.group)}</summary>`;
        sec.appendChild(det); host = det;
      } else {
        sec.innerHTML = `<h3 data-i18n="${g.group}">${T(g.group)}</h3>`;
      }
      for (const [k, label, min, max, step] of g.items) {
        const el = sliderEl(k, label, min, max, step, (INFO.defaults[k] ?? 0), (key, v) => this.change(key, v));
        $("input", el).disabled = true;
        el.addEventListener("dblclick", () => { if (this.auto) { this.change(k, this.auto[k]); el.set(this.auto[k]); } });
        $(".row span", el).dataset.i18nTitle = "p.dblclick";
        $(".row span", el).title = T("p.dblclick");
        this.els[k] = el;
        host.appendChild(el);
      }
      box.appendChild(sec);
    }
    $("#strip-pick").addEventListener("click", () => this.pickDir());
    $("#viewer-pick").addEventListener("click", () => this.pickDir());
    $("#btn-auto").addEventListener("click", () => this.resetAuto());
    $("#btn-save").addEventListener("click", () => this.save());
    $("#btn-reveal").addEventListener("click", () => { if (this.lastOut) reveal(this.lastOut, $("#save-msg")); });
    $("#btn-tolot").addEventListener("click", () => {
      $("input[name=mode][value=fixe]").checked = true;
      Lot.modeHint(); showView("lot");
    });
    $("#vpos").addEventListener("change", e => this.frame(Number(e.target.value)));
    $("#rot-l").addEventListener("click", () => this.rotate(-1));
    $("#rot-r").addEventListener("click", () => this.rotate(1));
    $("#sf-kind").value = FILTERS.strip.kind;
    $("#sf-hide").checked = !!FILTERS.strip.hideDone;
    $("#sf-kind").addEventListener("change", e => { FILTERS.strip.kind = e.target.value; saveFilters(); this.applyFilter(); });
    $("#sf-hide").addEventListener("change", e => { FILTERS.strip.hideDone = e.target.checked; saveFilters(); this.applyFilter(); });
    $("#a-dst").addEventListener("change", () => { if (this.dir) this.loadDir(this.dir); });
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
      if (!$("#a-dst").value) $("#a-dst").value = joinPath(dir, "AquImage");
      const dst = $("#a-dst").value.trim();
      const d = await api(`/api/files?dir=${encodeURIComponent(dir)}&dst=${encodeURIComponent(dst)}`);
      this.dir = dir; this.files = d.files;
      delete $("#strip-dir").dataset.i18n;
      $("#strip-dir").textContent = dir.split(/[\\/]/).filter(Boolean).pop() || dir; $("#strip-dir").title = dir;
      const ul = $("#thumbs"); ul.innerHTML = "";
      if (!d.files.length) ul.innerHTML = `<li class="hint" data-i18n="strip.empty">${T("strip.empty")}</li>`;
      for (const f of d.files) {
        const li = document.createElement("li");
        li.dataset.path = f.path; li.title = f.name;
        if (this.sess && this.sess.path === f.path) li.setAttribute("aria-current", "true");
        li.innerHTML = `<img alt="${f.name.replace(/"/g, "")}" loading="lazy">` + (f.kind === "video" ? `<span class="vid" data-i18n="video">${T("video")}</span>` : "");
        $("img", li).src = thumbUrl(f.path, 240);
        ul.appendChild(li);
        this.markDone(f, li);
      }
      this.applyFilter();
    } catch (e) { $("#strip-dir").textContent = e.message; }
  },
  async openFile(path, switchView) {
    if (switchView) showView("atelier");
    const dir = path.replace(/[\\/][^\\/]*$/, "");
    if (dir !== this.dir) await this.loadDir(dir);
    $$("#thumbs li").forEach(li => li.setAttribute("aria-current", String(li.dataset.path === path)));
    const cur = $(`#thumbs li[aria-current="true"]`); if (cur) cur.scrollIntoView({ block: "nearest" });
    delete $("#p-name").dataset.i18n;
    $("#p-name").textContent = path.split(/[\\/]/).pop();
    $("#p-diag").textContent = T("p.analysing");
    $("#busy").hidden = false;
    try {
      const d = await api("/api/open", { path });
      this.setSession(d, true);
      this.applyFilter();
    } catch (e) {
      $("#p-diag").textContent = e.message; $("#busy").hidden = true;
    }
  },
  step(delta) {
    if (!this.sess || !this.files.length) return;
    // On ne parcourt que les fichiers visibles avec les filtres actuels
    const vis = this.files.filter(f => this.visible(f) || f.path === this.sess.path);
    const i = vis.findIndex(f => f.path === this.sess.path);
    const n = vis[i + delta];
    if (n) this.openFile(n.path);
  },
  visible(f) {
    const F = FILTERS.strip;
    return (F.kind === "all" || F.kind === f.kind) && (!F.hideDone || !f.done);
  },
  markDone(f, li) {
    li = li || $$("#thumbs li").find(x => x.dataset.path === f.path);
    if (!li) return;
    let b = $(".done", li);
    if (f.done && !b) {
      b = document.createElement("span");
      b.className = "done"; b.textContent = "✓";
      li.appendChild(b);
    }
    if (b) { b.hidden = !f.done; b.title = T("f.done_badge"); }
  },
  applyFilter() {
    const n = { all: 0, image: 0, video: 0, done: 0 };
    const byPath = new Map($$("#thumbs li[data-path]").map(li => [li.dataset.path, li]));
    let shown = 0;
    for (const f of this.files) {
      n.all++; n[f.kind]++; if (f.done) n.done++;
      const vis = this.visible(f) || (this.sess && this.sess.path === f.path);
      const li = byPath.get(f.path);
      // Le fichier ouvert reste visible, même s'il vient d'être enregistré
      if (li) li.hidden = !vis;
      if (vis) shown++;
    }
    const sel = $("#sf-kind");
    sel.options[0].textContent = T("f.opt_all", { n: n.all });
    sel.options[1].textContent = T("f.opt_img", { n: n.image });
    sel.options[2].textContent = T("f.opt_vid", { n: n.video });
    $("#sf-hide-l").textContent = T("f.hide_done", { n: n.done });
    $("#sf-hide").disabled = !n.done && !FILTERS.strip.hideDone;
    $("#sf-count").textContent = this.files.length && shown !== n.all
      ? TP("f.shown", shown, { total: n.all }) : "";
  },
  showReveal(out) {
    this.lastOut = out;
    $("#btn-reveal").hidden = !out;
  },
  setDone(path) {
    const f = this.files.find(x => x.path === path);
    if (!f) return;
    f.done = true;
    this.markDone(f);
    this.applyFilter();
  },
  setSession(d, fresh) {
    this.sess = d;
    this.settings = { ...d.settings };
    this.auto = { ...d.settings };
    for (const [k, el] of Object.entries(this.els)) { el.set(this.settings[k]); $("input", el).disabled = false; el.classList.remove("changed"); }
    this.showDiag();
    $("#btn-auto").disabled = false; $("#btn-save").disabled = false; $("#btn-tolot").disabled = false;
    $("#rot-l").disabled = false; $("#rot-r").disabled = false;
    $("#btn-save").dataset.i18n = d.kind === "video" ? "a.save_video" : "a.save";
    $("#btn-save").textContent = T($("#btn-save").dataset.i18n);
    $("#vpos-wrap").hidden = d.kind !== "video";
    $("#vpos").value = d.pos;
    $("#save-msg").textContent = "";
    this.showReveal(null);
    this.loadBefore(fresh);
    this.render();
  },
  showDiag() {
    const d = this.sess;
    if (!d) return;
    let w = d.info.width, h = d.info.height;
    if (d.rot % 2) [w, h] = [h, w];
    const sz = d.kind === "video"
      ? T("p.video_size", { w, h, d: dur(d.info.duration) })
      : `${w}×${h}${d.info.bits === 16 ? T("p.bits16") : ""}`;
    let txt = T("p.diag", { sz, type: diagText(d.diag) });
    if (d.rot) txt += " " + T("p.rotated", { deg: d.rot * 90 }) + ".";
    $("#p-diag").textContent = txt;
  },
  async rotate(dir) {
    if (!this.sess) return;
    const rot = ((this.sess.rot || 0) + dir + 4) % 4;
    $("#busy").hidden = false;
    try {
      const d = await api("/api/rotate", { id: this.sess.id, rot });
      this.sess.rot = d.rot;
      this.showDiag();
      this.loadBefore(true);
      this.render();
    } catch (e) { $("#save-msg").textContent = e.message; $("#busy").hidden = true; }
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
    $("#p-diag").textContent = T("p.reanalysing");
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
    msg.textContent = T(this.sess.kind === "video" ? "a.saving_video" : "a.saving");
    $("#btn-save").disabled = true;
    try {
      const r = await api("/api/save", { id: this.sess.id, settings: this.settings,
        dest: $("#a-dst").value.trim(), fmt: $("#a-fmt").value });
      if (r.task) {
        $("#btn-save").disabled = false;
        await this.followTask(r.task, r.out, this.sess.path);
      } else {
        msg.textContent = T("a.saved", { out: r.out });
        this.showReveal(r.out);
        this.setDone(this.sess.path);
      }
    } catch (e) { msg.textContent = e.message; }
    $("#btn-save").disabled = false;
  },
  async followTask(id, out, src) {
    const msg = $("#save-msg");
    for (;;) {
      const t = await api(`/api/task?id=${id}`);
      if (t.state === "en cours") msg.textContent = T("a.video_progress", { p: Math.round(t.progress * 100) });
      else {
        if (t.state === "terminé") { this.setDone(src); this.showReveal(out); }
        msg.textContent = t.state === "terminé" ? T("a.video_done", { out })
          : t.state === "annulé" ? T("a.video_cancel") : T("a.video_error", { m: trMsg(t.message) });
        return;
      }
      await new Promise(r => setTimeout(r, 1000));
    }
  },
};

/* Ouvre un dossier (ou sélectionne un fichier) dans l'explorateur. */
async function reveal(path, msgEl) {
  try { await api("/api/reveal", { path }); }
  catch (e) { if (msgEl) msgEl.textContent = e.message; }
}

/* ================================================================== langue */
function setLang(code, save) {
  LANG = STRINGS[code] ? code : "fr";
  applyI18n();
  if (!save) return;
  try { localStorage.setItem("aquimage.lang", LANG); } catch (e) {}
  api("/api/config", { lang: LANG }).catch(() => {});
  // Textes calculés : on les refait dans la nouvelle langue
  $$(".sl").forEach(el => el.set && el.set($("input", el).value));
  if (Lot.last) Lot.render(Lot.last);
  if ($("#src").value) Lot.count();
  Lot.modeHint();
  Atelier.showDiag();
  Atelier.applyFilter();
  Atelier.files.forEach(f => Atelier.markDone(f));
  Viewer.apply();
}

/* ================================================================ démarrage */
(async function main() {
  INFO = await api("/api/info");
  setLang(pickLang((INFO.config || {}).lang), false);
  const sel = $("#lang");
  for (const [code, name] of Object.entries(LANGS)) sel.add(new Option(name, code, false, code === LANG));
  sel.addEventListener("change", () => setLang(sel.value, true));
  if (!INFO.ffmpeg) { $("#top-note").dataset.i18n = "note.ffmpeg"; $("#top-note").textContent = T("note.ffmpeg"); }
  Lot.init();
  Viewer.init();
  Atelier.init();
  Atelier.applyFilter();
  const c = INFO.config || {};
  if (c.src) { $("#src").value = c.src; Lot.count(); }
  if (c.dst) $("#dst").value = c.dst;
  if (c.atelier_dst) $("#a-dst").value = c.atelier_dst;
  if (c.atelier) Atelier.loadDir(c.atelier);
  try { const s = await api("/api/batch/status"); if (s.state && s.state !== "aucun") { Lot.render(s); if (s.state === "en cours") Lot.poll(); } } catch (e) {}
})();
