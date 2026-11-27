/* indexers.js — Indexers page: health table, scoring actions, charts.
 * Endpoints are unchanged from the inline script this replaces. */
"use strict";

(function () {
  const data = pageData();
  const $ = (id) => document.getElementById(id);
  let pending = {};            // indexer id -> new priority
  let healthChart = null;
  let trendChart = null;

  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };

  /* Chart colours come from the theme tokens, so charts follow light/dark. */
  function themeColors() {
    const cs = getComputedStyle(document.documentElement);
    const v = (name) => cs.getPropertyValue(name).trim();
    return {
      text: v("--text-muted"), grid: v("--border"), tooltipBg: v("--card"), tooltipText: v("--text"),
      series: [v("--accent"), v("--info-fg"), v("--ok-fg"), v("--warn-fg"), v("--danger-fg"),
               v("--violet-fg"), "#e879f9", "#fb923c", "#38bdf8", "#a3e635"],
    };
  }

  async function post(url, body) {
    return api(url, { method: "POST", body: body === undefined ? {} : body });
  }

  function report(r, okText) {
    const msg = r.msg || r.message || (r.ok ? okText : "Request failed");
    showToast(msg, r.ok ? (r.synced === false ? "warning" : "success") : "error", 5000);
    return r.ok;
  }

  /* ---------------- Health table ---------------- */

  function scoreCell(v) {
    if (v === null || v === undefined) return el("span", "text-dim", "—");
    const tone = v >= 80 ? "ok" : v >= 50 ? "warn" : "danger";
    return el("span", "font-semibold text-" + tone, v.toFixed(1) + "%");
  }

  /* What the health score is made of, in plain words (C-3), and what the AI
     changed (C-5). Shown under the row when its score is clicked. */
  const PARTS = [
    ["speed", "Speed", "how fast it answers searches"],
    ["searches", "Searches", "searches that work"],
    ["downloads", "Downloads", "downloads that work"],
    ["safety", "Safety", "no bad releases caught from it"],
  ];
  const signed = (n) => (n > 0 ? "+" : n < 0 ? "−" : "") + Math.abs(Math.round(n * 10) / 10);
  const open = new Set();                       // indexer ids whose breakdown is showing

  function breakdown(idx) {
    const box = el("div", "score-breakdown");
    const p = idx.parts;
    if (!p) {
      box.append(el("p", "m-0 text-muted",
        "Not scored yet: Inspectarr waits until an indexer has handled enough searches to judge it fairly."));
      return box;
    }
    const grid = el("div", "score-parts");
    PARTS.forEach(([key, name, what]) => {
      const part = p[key];
      const cell = el("div", "score-part");
      const bar = el("div", "score-bar");
      const fill = el("span", "score-bar-fill tone-" + (part.score >= 80 ? "ok" : part.score >= 50 ? "warn" : "danger"));
      fill.style.width = Math.max(0, Math.min(100, part.score)) + "%";
      bar.append(fill);
      cell.append(el("div", "score-part-head"), bar, el("div", "hint", what + " · counts for " + part.weight_pct + "%"));
      cell.firstChild.append(el("strong", null, name), el("span", "font-semibold", part.score + "/100"));
      grid.append(cell);
    });
    box.append(grid);

    const notes = el("ul", "score-notes");
    if (p.paused) notes.append(el("li", null, "Prowlarr has paused it after failures: " + signed(p.paused) + " points"));
    if (p.trend) notes.append(el("li", null, (p.trend > 0 ? "Getting better lately: " : "Getting worse lately: ") + signed(p.trend) + " points"));
    if (idx.ai_scored && idx.rule_score != null && idx.health_score != null) {
      const diff = idx.health_score - idx.rule_score;
      const li = el("li", null, Math.abs(diff) < 0.05
        ? "The AI agreed with Inspectarr's own score (" + idx.rule_score + ")."
        : "The AI changed it by " + signed(diff) + " points (Inspectarr's own score: " + idx.rule_score + ").");
      if (idx.ai_wanted != null) li.append(" It wanted " + idx.ai_wanted + "; the limit in Settings → AI held it back.");
      if (idx.ai_reasoning) li.append(el("div", "hint", "AI: " + idx.ai_reasoning));
      notes.append(li);
    }
    if (notes.childElementCount) box.append(notes);
    return box;
  }

  function change(idx) {
    if (idx.previous_score == null || idx.health_score == null) return null;
    const d = Math.round((idx.health_score - idx.previous_score) * 10) / 10;
    if (Math.abs(d) < 0.1) return null;
    const up = d > 0;
    const span = el("span", "score-change " + (up ? "text-ok" : "text-danger"), (up ? "▲ " : "▼ ") + Math.abs(d));
    span.title = "Was " + idx.previous_score + (idx.previous_at ? " on " + new Date(idx.previous_at).toLocaleString() : "");
    span.setAttribute("aria-label", (up ? "Up " : "Down ") + Math.abs(d) + " from " + idx.previous_score);
    return span;
  }

  function renderTable(indexers) {
    const tbody = $("indexer-tbody");
    tbody.replaceChildren();
    rowsInOrder = indexers;
    indexers.forEach((idx) => {
      const tr = el("tr", idx.ignored ? "is-pinned" : "");

      const prio = el("input", "priority-input");
      prio.type = "number"; prio.min = 1; prio.max = 999;
      prio.value = pending[idx.id] !== undefined ? pending[idx.id] : idx.priority;
      prio.setAttribute("aria-label", "Priority for " + idx.name);
      prio.addEventListener("change", () => {
        const n = parseInt(prio.value, 10);
        if (isNaN(n) || n === idx.priority) delete pending[idx.id];
        else pending[idx.id] = n;
        $("apply-btn").disabled = !Object.keys(pending).length;
      });
      const tdPrio = el("td"); tdPrio.append(prio);

      const tdName = el("td", "font-medium", idx.name);
      const tdResp = el("td", "num hide-sm text-muted", idx.avg_response_ms > 0 ? idx.avg_response_ms + " ms" : "—");
      const tdSucc = el("td", "num hide-sm text-muted", idx.total_records > 0 ? idx.success_rate + "%" : "—");
      const tdBad = el("td", "num");
      tdBad.append(idx.malicious_hits > 0 ? el("span", "text-danger font-semibold", String(idx.malicious_hits)) : el("span", "text-dim", "0"));
      const tdScore = el("td", "num");
      const why = el("button", "btn-link score-toggle");
      why.type = "button";
      why.title = "What this score is made of";
      why.setAttribute("aria-expanded", open.has(idx.id) ? "true" : "false");
      why.append(scoreCell(idx.health_score));
      if (idx.ai_scored) why.append(el("span", "badge badge-neutral ml-4", "AI"));
      tdScore.append(why);
      const moved = change(idx);
      if (moved) tdScore.append(moved);
      if (outOfOrder(idx)) {
        const up = el("span", "badge badge-warn ml-4", "↑");
        up.title = "Scores higher than an indexer above it. It moves up at the next reorder.";
        tdScore.append(up);
      }
      const detail = el("tr", "score-detail-row");
      const detailCell = el("td"); detailCell.colSpan = 9;
      detailCell.append(breakdown(idx));
      detail.append(detailCell);
      detail.hidden = !open.has(idx.id);
      why.addEventListener("click", () => {
        const show = detail.hidden;
        detail.hidden = !show;
        why.setAttribute("aria-expanded", show ? "true" : "false");
        if (show) open.add(idx.id); else open.delete(idx.id);
      });

      const tdStatus = el("td");
      if (idx.in_backoff) tdStatus.append(el("span", "badge badge-danger", "Backing off"));
      else if (idx.enabled === false) tdStatus.append(el("span", "badge badge-warn", "Disabled"));
      else if (idx.ignored) tdStatus.append(el("span", "badge badge-neutral", "Pinned"));

      const pin = el("input"); pin.type = "checkbox"; pin.checked = !!idx.ignored;
      pin.title = "Keep this indexer at its current priority";
      pin.setAttribute("aria-label", "Pin " + idx.name);
      pin.addEventListener("change", async () => {
        const r = await post("/config/prowlarr/set-ignored", {
          indexer_id: idx.id, indexer_name: idx.name, ignored: pin.checked, pinned_position: null,
        });
        if (!r.ok) { pin.checked = !pin.checked; showToast("Couldn't change the pin: " + (r.message || "failed"), "error"); }
      });
      const tdPin = el("td"); tdPin.append(pin);

      const tdAct = el("td", "nowrap text-right");
      const reset = el("button", "btn btn-ghost btn-sm", "Reset");
      reset.type = "button";
      reset.title = "Forget grabs, bad releases and cached scores for this indexer";
      reset.addEventListener("click", () => resetIndexer(idx));
      const toggle = el("button", "btn btn-ghost btn-sm" + (idx.enabled === false ? " text-ok" : ""), idx.enabled === false ? "Enable" : "Disable");
      toggle.type = "button";
      toggle.title = (idx.enabled === false ? "Enable" : "Disable") + " this indexer in Prowlarr";
      toggle.addEventListener("click", () => toggleEnabled(idx));
      tdAct.append(reset, toggle);

      tr.append(tdPrio, tdName, tdResp, tdSucc, tdBad, tdScore, tdStatus, tdPin, tdAct);
      tbody.append(tr, detail);
    });
    $("apply-btn").disabled = !Object.keys(pending).length;
  }

  /* The page shows stored scores only. It used to start its own AI pass on
     Refresh, which could finish after a reorder and leave scores on screen
     that the order wasn't built from. AI scoring happens in Rescore & reorder
     and the scheduled reorder, one at a time. */
  async function loadIndexers() {
    const msg = $("indexer-msg");
    const r = await api("/config/prowlarr/indexers");
    if (!r.ok) { msg.hidden = false; msg.textContent = "Couldn't load indexers: " + (r.message || "Prowlarr did not answer"); return; }
    msg.hidden = true;
    renderTable(r.indexers || []);
    $("indexer-table").hidden = false;
    renderOrderBasis(r);
  }

  /* What the order is, and when it was last set from the scores. */
  function renderOrderBasis(r) {
    const box = $("order-basis");
    if (!box) return;
    const parts = ["Listed in Prowlarr priority order: the lowest number is searched first."];
    if (r.last_reorder) {
      const last = new Date(r.last_reorder);
      const next = new Date(last.getTime() + (r.reorder_interval_hours || 24) * 3600e3);
      parts.push("Inspectarr last set this order from the health scores " + (timeAgo(r.last_reorder) || "") +
                 " (" + last.toLocaleString() + "); the next automatic reorder is " + next.toLocaleString() + ".");
    } else {
      parts.push("Inspectarr hasn't reordered them yet.");
    }
    const behind = (r.indexers || []).filter(outOfOrder).length;
    if (behind) parts.push(behind + " indexer(s) now score higher than one above them and move up at the next reorder (marked ↑).");
    box.textContent = parts.join(" ");
  }

  /* Marked ↑ when its score is higher than an unpinned indexer listed above
     it: the order is from an earlier reorder, and it will move up. */
  let rowsInOrder = [];
  function outOfOrder(idx) {
    if (idx.ignored || idx.health_score == null) return false;
    for (const other of rowsInOrder) {
      if (other.id === idx.id) return false;
      if (!other.ignored && other.health_score != null && other.health_score + 0.5 < idx.health_score) return true;
    }
    return false;
  }

  async function resetIndexer(idx) {
    const ok = await confirmDialog({
      title: `Reset stats for ${idx.name}?`,
      body: "Its grab count, bad-release count and cached health scores start again from zero. Its Prowlarr settings are not touched.",
      confirm: "Reset", tone: "danger",
    });
    if (!ok) return;
    if (report(await post("/indexers/reset", { indexer_id: idx.id }), "Stats reset")) loadIndexers();
  }

  async function toggleEnabled(idx) {
    const enable = idx.enabled === false;
    const ok = await confirmDialog({
      title: `${enable ? "Enable" : "Disable"} ${idx.name} in Prowlarr?`,
      body: enable ? "Prowlarr will start searching this indexer again."
                   : "Prowlarr will stop searching this indexer until you enable it again.",
      confirm: enable ? "Enable" : "Disable", tone: enable ? "primary" : "danger",
    });
    if (!ok) return;
    const r = await post("/config/prowlarr/toggle-indexer", { indexer_id: idx.id, enabled: enable });
    if (report(r, enable ? "Enabled" : "Disabled")) loadIndexers();
  }

  async function loadSchedule() {
    const dl = $("scoring-info");
    const r = await api("/indexers/scoring-status");
    dl.replaceChildren();
    const add = (k, v) => { dl.append(el("dt", null, k), el("dd", null, v)); };
    if (!r.ok) { add("Schedule", r.msg || "Unavailable"); return; }
    const fmt = (iso, fallback) => {
      if (!iso) return fallback;
      const rel = timeAgo(iso);
      return rel ? `${rel} (${new Date(iso).toLocaleString()})` : iso;
    };
    add("Scanning", r.scheduler_running ? "Running" : "Paused");
    add("Every", r.interval_hours + " hours");
    add("Last run", fmt(r.last_reorder, "Never"));
    add("Next run", fmt(r.next_reorder, "After the next scan"));
  }

  async function loadHealthChart() {
    const msg = $("chart-msg");
    const r = await api("/indexers/history");
    if (!r.ok) { msg.textContent = r.msg || "No history yet."; return; }
    const names = Object.keys(r.datasets || {});
    if (!names.length) { msg.textContent = "No history yet. It builds up as scoring runs."; return; }
    msg.hidden = true;
    $("health-chart-box").hidden = false;
    const c = themeColors();
    const datasets = names.map((name, i) => ({
      label: name, data: r.datasets[name],
      borderColor: c.series[i % c.series.length], backgroundColor: c.series[i % c.series.length],
      borderWidth: 2, pointRadius: 2, pointHoverRadius: 5, tension: 0.3, fill: false,
    }));
    if (healthChart) healthChart.destroy();
    healthChart = new Chart($("health-chart"), {
      type: "line",
      data: { datasets },
      options: {
        responsive: true, maintainAspectRatio: false,
        interaction: { mode: "nearest", intersect: false },
        scales: {
          x: { type: "time", time: { tooltipFormat: "MMM d, HH:mm" }, grid: { color: c.grid }, ticks: { color: c.text } },
          y: { min: 0, max: 100, grid: { color: c.grid }, ticks: { color: c.text, callback: (v) => v + "%" } },
        },
        plugins: {
          // Bottom: with dozens of indexers a top legend pushes the chart down.
          legend: { position: "bottom", labels: { color: c.text, usePointStyle: true, pointStyle: "circle", boxWidth: 8 } },
          tooltip: { backgroundColor: c.tooltipBg, titleColor: c.tooltipText, bodyColor: c.text, borderColor: c.grid, borderWidth: 1 },
        },
      },
    });
  }

  Actions["refresh-indexers"] = () => loadIndexers();

  /* ---------------- Rescore & reorder: a background job ---------------- */
  let jobTimer = null;
  const JOB_BUTTONS = '[data-action="rescore-reorder"], [data-action="trigger-scoring"]';

  function mins(s) {
    s = Math.max(0, Math.round(s));
    return s < 60 ? s + " s" : Math.floor(s / 60) + " min " + (s % 60) + " s";
  }

  function renderJob(job) {
    const box = $("rescore-panel");
    if (!box) return;
    const running = job && job.state === "running";
    document.querySelectorAll(JOB_BUTTONS).forEach((b) => { b.disabled = running; });
    if (!job || !job.state) { box.hidden = true; return; }
    const finished = job.finished_at ? new Date(job.finished_at) : null;
    // A finished job stays on show for 30 minutes, then the panel goes.
    if (!running && (!finished || Date.now() - finished.getTime() > 30 * 60e3)) { box.hidden = true; return; }
    box.hidden = false;
    box.className = "rescore-panel " + (running ? "is-running" : job.state === "done" ? "is-done" : "is-failed");
    const title = el("div", "rescore-title");
    if (running) title.append(el("span", "spinner"));
    title.append(el("strong", null, running ? "Rescore & reorder is running" :
                     job.state === "done" ? "Rescore & reorder finished" : "Rescore & reorder didn't finish"));
    const lines = [el("div", null, job.stage || "")];
    const started = job.started_at ? new Date(job.started_at) : null;
    if (running && started) {
      let t = "Running for " + mins((Date.now() - started.getTime()) / 1000);
      if (job.ai_estimate_s) t += "; the AI part took about " + mins(job.ai_estimate_s) + " when this model was calibrated";
      lines.push(el("div", "hint", t + ". You can leave this page; it carries on, and this panel shows its progress when you come back."));
    } else if (finished) {
      lines.push(el("div", "hint", finished.toLocaleString() + (started ? ", took " + mins((finished - started) / 1000) : "")));
    }
    if (job.message) lines.push(el("div", "hint", job.message));
    box.replaceChildren(title, ...lines);
  }

  async function watchJob(announce) {
    const r = await api("/indexers/rescore/status");
    const job = r.ok ? r.job : null;
    renderJob(job);
    clearTimeout(jobTimer);
    if (job && job.state === "running") {
      jobTimer = setTimeout(() => watchJob(true), 3000);
    } else if (announce && job && job.state) {
      showToast(job.stage || "Finished", job.state === "done" ? "success" : "error", 6000);
      loadIndexers(); loadSchedule(); loadHealthChart();
    }
  }

  async function startJob(url) {
    document.querySelectorAll(JOB_BUTTONS).forEach((b) => { b.disabled = true; });
    const r = await post(url);
    if (!r.ok && !r.started) showToast(r.msg || r.message || "Couldn't start", "warning", 5000);
    watchJob(true);
  }

  Actions["rescore-reorder"] = () => startJob("/indexers/rescore-reorder");

  Actions["sync-apps"] = async (btn) => {
    btn.disabled = true;
    report(await post("/indexers/sync-only"), "Synced");
    btn.disabled = false;
  };

  Actions["apply-order"] = async (btn) => {
    if (!Object.keys(pending).length) return;
    btn.disabled = true;
    const r = await post("/indexers/apply-priorities", { changes: pending });
    if (report(r, "Order saved")) { pending = {}; loadIndexers(); }
    else btn.disabled = false;
  };

  Actions["trigger-scoring"] = () => startJob("/indexers/trigger-scoring");

  /* ---------------- AI scoring tab ---------------- */

  Actions["load-trend"] = async (btn) => {
    document.querySelectorAll(".indexer-chip").forEach((b) => b.classList.toggle("btn-primary", b === btn));
    // This endpoint returns a bare JSON list (or {error}), so plain fetch.
    let rows = [];
    try {
      const res = await fetch("/api/llm-logs/history/" + encodeURIComponent(btn.dataset.id), { credentials: "same-origin" });
      const body = await res.json();
      if (Array.isArray(body)) rows = body;
    } catch (_) { /* treated as no history */ }
    const placeholder = $("trend-placeholder");
    if (!rows.length) { placeholder.hidden = false; placeholder.textContent = "No history for this indexer yet."; $("trend-box").hidden = true; return; }
    placeholder.hidden = true;
    $("trend-box").hidden = false;
    const c = themeColors();
    if (trendChart) trendChart.destroy();
    trendChart = new Chart($("trend-canvas"), {
      type: "line",
      data: {
        labels: rows.map((d) => d.scored_at.slice(5, 16).replace("T", " ")),
        datasets: [
          { label: "AI score", data: rows.map((d) => d.ai_score), borderColor: c.series[5], backgroundColor: c.series[5], tension: 0.3, pointRadius: 3 },
          { label: "Rules score", data: rows.map((d) => d.deterministic_score), borderColor: c.text, borderDash: [5, 3], tension: 0.3, pointRadius: 2 },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        scales: {
          y: { min: 0, max: 100, grid: { color: c.grid }, ticks: { color: c.text } },
          x: { grid: { color: c.grid }, ticks: { color: c.text, maxRotation: 45 } },
        },
        plugins: { legend: { labels: { color: c.text } } },
      },
    });
  };

  /* ---------------- start ---------------- */

  if (data.tab === "health" && data.prowlarr_enabled) {
    loadIndexers();
    loadSchedule();
    loadHealthChart();
    watchJob(false);          // a job started elsewhere, or before this page loaded
  }
  if (data.tab === "ai-scoring") {
    api("/config/ollama/update-check").then((d) => {
      if (d.ok && d.update_available) {
        $("ollama-update-model").textContent = d.model || "";
        $("ollama-update-banner").hidden = false;
      }
    });
  }
})();
