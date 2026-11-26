/* ai-models.js -- the AI tab's model list (Settings -> AI).
 *
 * Every model on the connected Ollama server, best first, each with what
 * Calibrate measured here: a score out of 100, how long it takes, where it
 * runs, and the settings it chose. Actions per model: Results, Calibrate,
 * Use this model. Modelled on DockLlama's list.
 *
 * Loaded BEFORE settings.js, which calls loadOllamaModels, loadValidations
 * and resumeValidationIfRunning while it starts up; they are exported on
 * window for that and for the settings-bridge whitelist. Helpers it borrows
 * from settings.js (_applyModel, showApplyAnywayDialog, loadBatchCalibration)
 * are only called later, from clicks and fetch callbacks, by which time
 * settings.js has defined them.
 *
 * Ranking (DockLlama's rule): the highest score; among models within 2
 * points of it, the one that fits on the graphics card, then the fastest.
 * The page only suggests. Nothing switches by itself.
 */
"use strict";

(function () {
  const WITHIN = 2;              // points: a tie, decided by fit then speed
  const FIT_OVERHEAD_GB = 1.0;   // context cache + runtime on top of the model file

  const S = {
    models: [], details: {}, current: "", loaded: false, error: "",
    validations: {}, server: "", others: 0,
    hardware: "", hwOptions: [],
    running: null, poll: null, open: {}, history: {},
    runs: [], last: null, banner: null, allOpen: false,
  };

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const k = (n) => (n >= 1024 ? Math.round(n / 1024) + "K" : String(n));
  // Stored times are UTC; show them in the viewer's own time.
  const when = (iso) => {
    if (!iso) return "";
    const d = new Date(/Z|[+-]\d\d:?\d\d$/.test(iso) ? iso : iso + "Z");
    return isNaN(d) ? iso.replace("T", " ").slice(0, 16)
      : d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
  };

  // --- loading ---------------------------------------------------------------
  function loadOllamaModels() {
    S.loaded = false;
    render();
    return fetch("/config/ollama/models")
      .then((r) => r.json())
      .then((d) => {
        S.loaded = true;
        S.error = d.ok ? "" : (d.message || "Could not read the model list");
        S.models = d.models || [];
        S.current = d.current || "";
        S.details = {};
        (d.details || []).forEach((m) => { S.details[m.name] = m; });
        syncInUseControls();
        return Promise.all([loadValidations(), loadHardware(), loadRuns()]);
      })
      .catch(() => { S.loaded = true; S.error = "Could not reach the server"; render(); });
  }

  function loadValidations() {
    return fetch("/config/ai/validations")
      .then((r) => r.json())
      .then((d) => {
        S.validations = {};
        (d.validations || []).forEach((v) => { S.validations[v.model] = v; });
        S.server = d.server || "";
        S.others = d.other_servers || 0;
        renderServerNote();
        render();
      })
      .catch(() => render());
  }

  function loadHardware() {
    return fetch("/config/ai/hardware")
      .then((r) => r.json())
      .then((d) => {
        S.hardware = d.hardware || "";
        S.hwOptions = d.options || [];
        const sel = $("ai-hardware");
        if (sel) {
          sel.innerHTML = '<option value="">Choose…</option>' + S.hwOptions.map((o) =>
            `<option value="${esc(o.value)}"${o.value === S.hardware ? " selected" : ""}>${esc(o.label)}</option>`
          ).join("");
        }
        render();
      })
      .catch(() => {});
  }

  function saveHardware(value) {
    fetch("/config/ai/hardware", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ hardware: value }),
    }).then(() => { S.hardware = value; render(); });
  }

  function renderServerNote() {
    const note = $("ollama-server-note");
    if (!note || !S.server) return;
    note.innerHTML = "Models and results below are for <code>" + esc(S.server) + "</code>." +
      (S.others ? " Results from " + S.others + " other server" + (S.others === 1 ? "" : "s") +
        " are kept and come back when you switch to " + (S.others === 1 ? "it" : "one") + "." : "");
  }

  // --- ranking -----------------------------------------------------------------
  function hw() {
    return S.hwOptions.find((o) => o.value === S.hardware) || null;
  }

  function mayNotFit(d) {
    const h = hw();
    if (!h || h.cpu || !d || !d.size_gb) return false;
    return d.size_gb + FIT_OVERHEAD_GB > h.gib;
  }

  function onCard(v) {
    return v && v.gpu_offload_pct != null ? v.gpu_offload_pct >= 99 : true;
  }

  // A model that passed Calibrate on this server and has a score to rank by.
  function ranked(v) {
    return v && v.status === "supported" && v.calibrated && v.quality_score != null;
  }

  function best(names) {
    const cands = names.filter((n) => ranked(S.validations[n]));
    if (!cands.length) return null;
    const top = Math.max(...cands.map((n) => S.validations[n].quality_score));
    const close = cands.filter((n) => S.validations[n].quality_score >= top - WITHIN);
    close.sort((a, b) => {
      const va = S.validations[a], vb = S.validations[b];
      if (onCard(va) !== onCard(vb)) return onCard(va) ? -1 : 1;
      return (va.total_s || 1e9) - (vb.total_s || 1e9);
    });
    return close[0];
  }

  function group(name) {
    const d = S.details[name], v = S.validations[name];
    if (d && d.can_score === false) return 5;          // embedding / decision models
    if (ranked(v)) return 1;
    if (v && v.status === "supported") return 2;        // older check, no score
    if (!v) return 3;                                    // not calibrated yet
    return 4;                                            // failed / forced
  }

  function ordered() {
    const names = S.models.slice();
    names.sort((a, b) => {
      const ga = group(a), gb = group(b);
      if (ga !== gb) return ga - gb;
      if (ga === 1) return S.validations[b].quality_score - S.validations[a].quality_score;
      return a.localeCompare(b);
    });
    return names;
  }

  // --- rendering -----------------------------------------------------------------
  function thinkWord(t) {
    if (t === true) return "thinking on";
    if (t === false) return "thinking off";
    if (t == null) return "";
    return "thinking " + t;
  }

  function speedText(v) {
    if (v.total_s && v.indexer_count) {
      return "about " + Math.round(v.total_s) + " s for " + v.indexer_count + " indexers";
    }
    if (v.sec_per_indexer) return v.sec_per_indexer.toFixed(1) + " s per indexer";
    return "";
  }

  function badges(name, d, v, isBest) {
    const b = [];
    if (name === S.current) b.push('<span class="badge badge-info">In use</span>');
    if (isBest) b.push('<span class="badge badge-ok" title="Best result on this server">Suggested</span>');
    if (ranked(v)) {
      const sp = speedText(v);
      b.push(`<span class="badge badge-neutral" title="Graded score on this server${v.quality_runs ? " (runs " +
        v.quality_runs.map((x) => Math.round(x)).join(" and ") + ")" : ""}">${Math.round(v.quality_score)}/100 here` +
        (sp ? " · " + esc(sp) : "") + "</span>");
    } else if (v && v.status === "supported") {
      b.push('<span class="badge badge-neutral" title="Checked by an older version, without a score">' +
        "✓ passed an older check</span>");
    } else if (v && v.status === "failed") {
      b.push('<span class="badge badge-danger">✗ not good enough here</span>');
    } else if (v && v.status === "forced") {
      b.push('<span class="badge badge-warn">⚠ used without passing</span>');
    } else if (!d || d.can_score !== false) {
      b.push('<span class="badge badge-neutral">Not calibrated</span>');
    }
    if (v && v.gpu_offload_pct != null) {
      b.push(`<span class="badge ${v.gpu_offload_pct >= 99 ? "badge-neutral" : "badge-warn"}" ` +
        `title="How much of the model sat in graphics memory during its test">` +
        (v.gpu_offload_pct === 0 ? "CPU only" : "GPU " + v.gpu_offload_pct + "%") + "</span>");
    }
    if (mayNotFit(d)) b.push('<span class="badge badge-warn">May not fit your graphics card</span>');
    if (v && v.follows_reference) {
      b.push('<span class="badge badge-warn" title="Its scores stay close to Inspectarr\'s own, so the AI adds little">' +
        "Mostly repeats Inspectarr's score</span>");
    }
    return b.join(" ");
  }

  function meta(d) {
    if (!d) return "";
    const bits = [];
    if (d.size_gb) bits.push(d.size_gb + " GB");
    if (d.parameter_size) bits.push(esc(d.parameter_size));
    if (d.thinking && d.thinking !== "none") bits.push("can think");
    if (d.max_context) bits.push("up to " + k(d.max_context) + " context");
    return bits.join(" · ");
  }

  function actions(name, d, v) {
    if (d && d.can_score === false) return "";
    const busy = S.running && S.running.model;
    const mine = busy === name;
    const a = [];
    if (v) a.push(`<button type="button" class="btn btn-ghost btn-sm" data-action="ai-results" data-model="${esc(name)}"` +
      ` aria-expanded="${S.open[name] ? "true" : "false"}">Results</button>`);
    a.push(`<button type="button" class="btn btn-ghost btn-sm" data-action="ai-calibrate" data-model="${esc(name)}"` +
      (busy ? ` disabled title="${mine ? "Calibrating now" : "Calibrating " + esc(busy) + " — one at a time"}"` : "") +
      `>${v ? "Recalibrate" : "Calibrate"}</button>`);
    if (name !== S.current) {
      a.push(`<button type="button" class="btn btn-primary btn-sm" data-action="ai-use" data-model="${esc(name)}">` +
        "Use this model</button>");
    }
    return a.join("");
  }

  function results(name, v) {
    if (!S.open[name] || !v) return "";
    const out = [];
    const tests = (v.tests || []).map((t) =>
      `<li>${t.passed ? "✓" : "✗"} <strong>${esc(t.name)}</strong> — ${esc(t.detail || "")}</li>`).join("");
    if (tests) out.push(`<ul class="reason-ul">${tests}</ul>`);
    const tried = (v.thinking_tried || []).map((t) =>
      `<li>${esc(thinkWord(t.think) || "no thinking")} at ${k(t.context_window)}: ` +
      (t.complete ? `${Math.round(t.avg)}/100 (runs ${t.runs.map((x) => Math.round(x)).join(", ")})`
                  : "did not finish") + "</li>").join("");
    if (tried) out.push(`<p class="m-0 mt-8">Tried:</p><ul class="reason-ul">${tried}</ul>`);
    const s = v.settings;
    if (s) {
      out.push(`<p class="m-0 mt-8">Settings it chose: ${k(s.context_window)} context, ` +
        `${s.max_indexers_per_call} per call, timeout ${s.timeout} s` +
        (thinkWord(s.think) ? ", " + thinkWord(s.think) : "") + ".</p>");
    }
    if (v.validated_at) out.push(`<p class="hint m-0 mt-8">Calibrated ${esc(when(v.validated_at))}` +
      (v.indexer_count ? ` with ${v.indexer_count} test indexers` : "") + ".</p>");
    const h = S.history[name];
    if (h && h.length > 1) {
      out.push('<p class="m-0 mt-8">Earlier runs:</p><ul class="reason-ul">' + h.slice(1).map((r) =>
        `<li>${esc(when(r.run_at))}: ` +
        (r.quality_score != null ? Math.round(r.quality_score) + "/100" : (r.passed ? "passed" : "failed")) +
        (r.settings ? `, ${k(r.settings.context_window)}, ${r.settings.max_indexers_per_call} per call` : "") +
        "</li>").join("") + "</ul>");
    }
    out.push(`<button type="button" class="btn btn-ghost btn-xs mt-8" data-action="ai-forget" data-model="${esc(name)}"` +
      ` title="Forget this result; the model shows as not calibrated">Forget this result</button>`);
    return `<div class="model-results panel-sm mt-8">${out.join("")}</div>`;
  }

  function progressLine(name) {
    if (!S.running || S.running.model !== name) return "";
    const r = S.running;
    return `<div class="model-progress mt-8" role="status"><span class="spinner" aria-hidden="true"></span> ` +
      `${esc(r.stage || "Starting")}${r.total ? " (" + (r.done || 0) + "/" + r.total + ")" : ""}` +
      ` — runs in the background; you can leave the page.</div>`;
  }

  function row(name, isBest) {
    const d = S.details[name], v = S.validations[name];
    return `<div class="model-row${name === S.current ? " model-row-current" : ""}" data-model-row="${esc(name)}">` +
      '<div class="model-row-main">' +
        `<div class="model-row-text"><div class="model-name">${esc(name)} ${badges(name, d, v, isBest)}</div>` +
        `<div class="hint model-meta">${meta(d)}</div></div>` +
        `<div class="model-actions">${actions(name, d, v)}</div>` +
      "</div>" + progressLine(name) + results(name, v) + "</div>";
  }

  function render() {
    const box = $("ai-model-list");
    if (!box) return;
    if (!S.loaded) { box.innerHTML = '<p class="hint">Loading models…</p>'; return; }
    if (S.error) { box.innerHTML = `<p class="hint">${esc(S.error)}</p>`; return; }
    if (!S.models.length) { box.innerHTML = '<p class="hint">No models on this server yet.</p>'; return; }
    const names = ordered();
    const top = best(names);
    const usable = names.filter((n) => group(n) < 5);
    const cannot = names.filter((n) => group(n) === 5);
    box.innerHTML = usable.map((n) => row(n, n === top)).join("") + (cannot.length
      ? `<details class="hint mt-12"><summary>${cannot.length} model${cannot.length === 1 ? "" : "s"} ` +
        "cannot score indexers (embedding or decision models)</summary><p class=\"m-0 mt-8\">" +
        cannot.map(esc).join(", ") + "</p></details>"
      : "");
    const note = $("ai-run-note");
    if (note) {
      note.textContent = S.running && S.running.model
        ? "Calibrating " + S.running.model + ". The other Calibrate buttons are paused until it finishes."
        : (top ? "Suggested here: " + top + "." : "");
    }
  }

  // --- calibrate ---------------------------------------------------------------
  function calibrate(name) {
    if (!confirm("Calibrate " + name + " on this server?\n\n" +
        "It scores 14 test indexers twice (with thinking off and on, if the model can think), " +
        "then finds how many of your indexers it handles per call and the context it needs. " +
        "Several minutes; on a slow or CPU-only server, longer. While it runs, the AI is not " +
        "used for anything else, so a scan in the meantime keeps Inspectarr's own scores.")) return;
    S.running = { model: name, stage: "Starting", done: 0, total: 0 };
    render();
    fetch("/config/ai/validate", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: name, calibration: "deep" }),
    })
      .then((r) => r.json().then((d) => ({ code: r.status, d })))
      .then((res) => {
        if (res.d.ok || res.code === 409) { poll(); return; }   // 409: follow the run in progress
        S.running = null;
        render();
        const note = $("ai-run-note");
        if (note) note.textContent = res.d.message || "Could not start";
      })
      .catch((e) => { S.running = null; render(); const n = $("ai-run-note"); if (n) n.textContent = "Request failed: " + e; });
  }

  function poll() {
    if (S.poll) clearInterval(S.poll);
    const tick = () => fetch("/config/ai/validate/status").then((r) => r.json()).then((s) => {
      if (s.running) {
        S.running = { model: s.model, stage: s.stage, done: s.done, total: s.total,
                      position: s.position, of: s.of, run_id: s.run_id };
        render();
        loadRuns();
        return;
      }
      loadRuns();
      clearInterval(S.poll); S.poll = null;
      const finished = S.running && S.running.model;
      S.running = null;
      if (finished) { S.open[finished] = true; delete S.history[finished]; }
      if (s.interrupted && s.error) { const n = $("ai-run-note"); if (n) n.textContent = s.error; }
      loadValidations().then(() => { if (finished) loadHistory(finished); });
      if (typeof loadBatchCalibration === "function") loadBatchCalibration();
    }).catch(() => { clearInterval(S.poll); S.poll = null; S.running = null; render(); });
    S.poll = setInterval(tick, 2000);
    tick();
  }

  // Rejoin a run already in progress (a calibration survives a page reload).
  function resumeValidationIfRunning() {
    fetch("/config/ai/validate/status").then((r) => r.json()).then((s) => {
      if (s && s.running) { S.running = { model: s.model, stage: s.stage, done: s.done, total: s.total }; render(); poll(); }
    }).catch(() => {});
  }

  // --- results ---------------------------------------------------------------------
  function loadHistory(name) {
    return fetch("/config/ai/history?model=" + encodeURIComponent(name)).then((r) => r.json())
      .then((d) => { S.history[name] = d.history || []; render(); }).catch(() => {});
  }

  function toggleResults(name) {
    S.open[name] = !S.open[name];
    render();
    if (S.open[name] && !S.history[name]) loadHistory(name);
  }

  function forget(name) {
    if (!confirm("Forget the result for " + name + " on this server? It will show as not calibrated.")) return;
    fetch("/config/ai/validation/delete", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: name }),
    }).then(() => { delete S.history[name]; S.open[name] = false; loadValidations(); });
  }

  // --- use this model ----------------------------------------------------------------
  function useModel(name) {
    _applyModel(name, false).then((res) => {
      if (res.code === 409 && res.d.needs_validation) { showApplyAnywayDialog(name, res.d); return; }
      const note = $("ai-run-note");
      if (!res.d.ok) { if (note) note.textContent = "✗ " + (res.d.message || "Could not apply"); return; }
      afterApply(name, res.d.applied || {});
      if (note) {
        const a = res.d.applied || {};
        note.textContent = "✓ " + name + " is in use" + (a.calibrated
          ? ", with the settings Calibrate chose."
          : ". It has not been calibrated here, so its context is " + k(a.context_window || 8192) +
            " and the other settings are unchanged. Run Calibrate to tune it.");
      }
    });
  }

  // Called by the page after a model is applied (also from Apply anyway).
  function afterApply(name, applied) {
    S.current = name;
    const inUse = $("ollama-in-use");
    if (inUse) inUse.textContent = name;
    applied = applied || {};
    const ctx = $("ollama_context_window");
    if (ctx && applied.context_window) ctx.value = applied.context_window;
    const batch = $("ollama_max_indexers_per_call");
    if (batch && applied.max_indexers_per_call) {
      batch.value = applied.max_indexers_per_call;
      if (typeof onBatchInput === "function") onBatchInput();
    }
    const to = $("ollama_timeout");
    if (to && applied.timeout) to.value = applied.timeout;
    const th = $("ollama-think");
    if (th) {
      const t = applied.think;
      th.value = t === true ? "on" : t === false ? "off" : (t || "");
    }
    syncInUseControls();
    loadValidations();
    if (typeof loadBatchCalibration === "function") loadBatchCalibration();
  }

  // --- the in-use model's settings: context slider + thinking choices --------
  const STOPS = [2048, 4096, 8192, 16384, 32768, 65536, 131072, 262144];

  function contextStops(max) {
    if (!max) return STOPS.slice();
    const s = STOPS.filter((x) => x <= max);
    if (!s.length || s[s.length - 1] !== max) s.push(max);
    return s;
  }

  function syncInUseControls() {
    const d = S.details[S.current] || {};
    const hidden = $("ollama_context_window"), slider = $("ollama-context-slider"), label = $("ollama-context-value");
    if (hidden && slider) {
      let value = parseInt(hidden.value, 10) || 8192;
      const stops = contextStops(d.max_context);
      const hint = $("ollama-context-hint");
      if (d.max_context && value > d.max_context) {
        value = d.max_context;
        hidden.value = value;
        if (hint) hint.textContent = "Lowered to " + k(value) + ": that is the most " + S.current +
          " supports. Save to keep it.";
        if (typeof markDirty === "function") { const sb = $("savebar"); if (sb) sb.classList.add("dirty"); }
      }
      if (stops.indexOf(value) < 0) { stops.push(value); stops.sort((a, b) => a - b); }
      slider.dataset.stops = JSON.stringify(stops);
      slider.max = String(stops.length - 1);
      slider.value = String(stops.indexOf(value));
      if (label) label.textContent = k(value);
    }
    const th = $("ollama-think");
    if (th) {
      const mode = d.thinking || "none", levels = d.think_levels || [];
      Array.from(th.options).forEach((o) => {
        const v = o.value;
        let ok = v === "";
        if (mode === "toggle") ok = ok || v === "on" || v === "off";
        if (mode === "levels") ok = ok || levels.indexOf(v) >= 0 || (v === "off" && levels.indexOf("off") >= 0);
        if (!S.current || !S.details[S.current]) ok = true;      // unknown model: offer everything
        o.hidden = !ok;
        o.disabled = !ok;
      });
    }
  }

  function onContextSlide() {
    const slider = $("ollama-context-slider"), hidden = $("ollama_context_window"), label = $("ollama-context-value");
    if (!slider || !hidden) return;
    let stops = STOPS;
    try { stops = JSON.parse(slider.dataset.stops || "null") || STOPS; } catch (_) {}
    const v = stops[parseInt(slider.value, 10)] || 8192;
    hidden.value = v;
    if (label) label.textContent = k(v);
  }

  // --- pull a newer build of the model in use ----------------------------------------
  function pullModel() {
    const model = S.current;
    if (!model) return;
    if (!confirm("Download the newer build of " + model + "?\n\nThis can be several gigabytes and " +
                 "replaces the installed copy. Scoring keeps using the current build until it finishes.")) return;
    setPullUI("Starting…", 0, true);
    fetch("/config/ollama/pull", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model }),
    })
      .then((r) => r.json().then((d) => ({ status: r.status, d })))
      .then((res) => {
        if (res.status === 409) setPullUI(res.d.message, 0, true);
        else if (!res.d.ok) { setPullUI(res.d.message, 0, false); return; }
        pollPull();
      })
      .catch((e) => setPullUI("Could not start: " + e, 0, false));
  }


  // --- runs: Calibrate all, overnight, pause / resume / cancel ------------------
  function loadRuns() {
    return fetch("/config/ai/runs").then((r) => r.json()).then((d) => {
      S.runs = d.runs || [];
      S.last = d.last || null;
      S.banner = d.banner || null;
      renderRunBar();
      renderBanner();
      if (S.runs.some((r) => r.status === "running") && !S.poll) poll();
    }).catch(() => {});
  }

  function localTime(iso) {
    const d = new Date(iso);
    return isNaN(d) ? iso : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function itemMark(i) {
    if (i.status === "done") return "✓ " + esc(i.model) + (i.score != null ? " (" + Math.round(i.score) + ")" : "");
    if (i.status === "failed") return "✗ " + esc(i.model);
    if (i.status === "running") return "▶ " + esc(i.model);
    if (i.status === "cancelled") return "– " + esc(i.model);
    return esc(i.model);
  }

  function renderRunBar() {
    const bar = $("ai-run-bar");
    if (!bar) return;
    if (!S.runs.length) { bar.style.display = "none"; bar.innerHTML = ""; return; }
    bar.style.display = "";
    bar.innerHTML = S.runs.map((r) => {
      const total = r.items.length;
      const done = r.items.filter((i) => i.status === "done" || i.status === "failed").length;
      let head;
      if (r.status === "running") {
        const cur = r.items.find((i) => i.status === "running");
        head = `Calibrating ${done + 1} of ${total}` + (cur ? ": <strong>" + esc(cur.model) + "</strong>" : "");
      } else if (r.status === "paused") {
        head = `Paused after ${done} of ${total}`;
      } else {
        const later = new Date(r.start_after) > new Date();
        head = later ? `Waiting until ${esc(localTime(r.start_after))}: ${total} model${total === 1 ? "" : "s"}`
                     : `Starting: ${total} model${total === 1 ? "" : "s"}`;
      }
      const btns = [];
      if (r.status === "paused") btns.push(`<button type="button" class="btn btn-ghost btn-xs" data-action="ai-run" data-run="${r.id}" data-what="resume">Resume</button>`);
      else btns.push(`<button type="button" class="btn btn-ghost btn-xs" data-action="ai-run" data-run="${r.id}" data-what="pause">Pause</button>`);
      btns.push(`<button type="button" class="btn btn-ghost btn-xs" data-action="ai-run" data-run="${r.id}" data-what="cancel">Cancel</button>`);
      return `<div class="panel-sm mb-8"><div class="flex items-center justify-between gap-8 flex-wrap">` +
        `<span>${head}${r.note ? ' <span class="hint">(' + esc(r.note) + ")</span>" : ""}</span>` +
        `<span class="flex gap-8">${btns.join("")}</span></div>` +
        `<div class="hint mt-4">${r.items.map(itemMark).join(" · ")}</div></div>`;
    }).join("");
  }

  function runAction(id, what) {
    if (what === "cancel" && !confirm("Cancel this run? The model in progress stops at its next step; " +
        "results already finished are kept.")) return;
    fetch(`/config/ai/runs/${id}/${what}`, { method: "POST" }).then(() => setTimeout(loadRuns, 300));
  }

  function renderBanner() {
    const box = $("ai-banner");
    if (!box) return;
    const b = S.banner;
    if (!b || !b.model || b.model === S.current) { box.style.display = "none"; box.innerHTML = ""; return; }
    box.style.display = "";
    box.innerHTML = `<strong>${esc(b.model)}</strong> did clearly better here: ${Math.round(b.score)}/100` +
      (b.current_score != null ? ` against ${Math.round(b.current_score)} for ${esc(b.current)}, the model in use` : "") +
      ".<div class=\"flex gap-8 mt-8\">" +
      `<button type="button" class="btn btn-ghost btn-sm" data-action="ai-banner" data-what="review" data-model="${esc(b.model)}">Review results</button>` +
      `<button type="button" class="btn btn-primary btn-sm" data-action="ai-banner" data-what="switch" data-model="${esc(b.model)}">Switch to it</button>` +
      `<button type="button" class="btn btn-ghost btn-sm" data-action="ai-banner" data-what="keep">Keep current</button></div>`;
  }

  function bannerAction(what, model) {
    if (what === "review") {
      S.open[model] = true; render();
      const row = document.querySelector(`[data-model-row="${CSS.escape(model)}"]`);
      if (row) row.scrollIntoView({ block: "center" });
      if (!S.history[model]) loadHistory(model);
      return;
    }
    const done = () => fetch("/config/ai/banner/dismiss", { method: "POST" }).then(loadRuns);
    if (what === "switch") useModel(model);
    done();
  }

  // Rough time for one model, for the estimate: the last calibration of it
  // here if there is one, else by size (big models load and think slower).
  function estimateMinutes(name) {
    const d = S.details[name] || {};
    return d.size_gb > 10 ? 15 : 7;
  }

  function overnightHour() {
    const el = $("ollama_overnight_hour");
    return el ? parseInt(el.value, 10) || 0 : 1;
  }

  function renderAllPanel() {
    const box = $("ai-all-panel");
    if (!box) return;
    if (!S.allOpen) { box.style.display = "none"; box.innerHTML = ""; return; }
    box.style.display = "";
    const usable = ordered().filter((n) => group(n) < 5);
    box.innerHTML = '<p class="m-0 mb-8">Calibrate these models one after another. Each is loaded in turn, ' +
      "so other apps using this Ollama server will be slower while it runs.</p>" +
      '<div class="check-list">' + usable.map((n) =>
        `<label class="block"><input type="checkbox" class="ai-all-pick" value="${esc(n)}" checked> ${esc(n)}` +
        (mayNotFit(S.details[n]) ? ' <span class="badge badge-warn">may not fit</span>' : "") + "</label>").join("") +
      "</div>" +
      '<p class="hint mt-8 mb-8" id="ai-all-estimate"></p>' +
      '<div class="flex gap-8 flex-wrap">' +
      '<button type="button" class="btn btn-primary btn-sm" data-action="ai-all-start" data-when="now">Run now</button>' +
      `<button type="button" class="btn btn-ghost btn-sm" data-action="ai-all-start" data-when="tonight">Tonight at ${String(overnightHour()).padStart(2, "0")}:00</button>` +
      '<button type="button" class="btn btn-ghost btn-sm" data-action="ai-all-close">Cancel</button></div>';
    updateEstimate();
  }

  function picked() {
    return Array.from(document.querySelectorAll(".ai-all-pick")).filter((c) => c.checked).map((c) => c.value);
  }

  function updateEstimate() {
    const el = $("ai-all-estimate");
    if (!el) return;
    const p = picked();
    const mins = p.reduce((a, n) => a + estimateMinutes(n), 0);
    el.textContent = p.length ? `${p.length} model${p.length === 1 ? "" : "s"}, roughly ` +
      (mins >= 90 ? Math.round(mins / 60) + " hours" : mins + " minutes") + "." : "Tick at least one model.";
  }

  function startAll(when) {
    const models = picked();
    if (!models.length) return;
    fetch("/config/ai/runs", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ models, when }),
    }).then((r) => r.json()).then((d) => {
      const note = $("ai-run-note");
      if (!d.ok) { if (note) note.textContent = d.message || "Could not start"; return; }
      S.allOpen = false; renderAllPanel();
      if (note) note.textContent = when === "tonight"
        ? "Queued for " + localTime(d.start_after) + "." : "Calibration started.";
      loadRuns().then(() => { if (when !== "tonight") poll(); });
    });
  }

  // --- wiring --------------------------------------------------------------------------
  Actions["ai-refresh"] = () => loadOllamaModels();
  Actions["ai-calibrate"] = (el) => calibrate(el.dataset.model);
  Actions["ai-use"] = (el) => useModel(el.dataset.model);
  Actions["ai-results"] = (el) => toggleResults(el.dataset.model);
  Actions["ai-forget"] = (el) => forget(el.dataset.model);
  Actions["ai-run"] = (el) => runAction(el.dataset.run, el.dataset.what);
  Actions["ai-banner"] = (el) => bannerAction(el.dataset.what, el.dataset.model);
  Actions["ai-all-open"] = () => { S.allOpen = true; renderAllPanel(); };
  Actions["ai-all-close"] = () => { S.allOpen = false; renderAllPanel(); };
  Actions["ai-all-start"] = (el) => startAll(el.dataset.when);

  document.addEventListener("change", (e) => {
    if (e.target && e.target.id === "ai-hardware") saveHardware(e.target.value);
    if (e.target && e.target.classList && e.target.classList.contains("ai-all-pick")) updateEstimate();
  });
  document.addEventListener("input", (e) => {
    if (e.target && e.target.id === "ollama-context-slider") onContextSlide();
  });

  window.loadOllamaModels = loadOllamaModels;
  window.loadValidations = loadValidations;
  window.resumeValidationIfRunning = resumeValidationIfRunning;
  window.pullModel = pullModel;
  window.aiAfterApply = afterApply;
})();
