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

  function renderTable(indexers) {
    const tbody = $("indexer-tbody");
    tbody.replaceChildren();
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
      const tdScore = el("td", "num"); tdScore.append(scoreCell(idx.health_score));

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
      tbody.append(tr);
    });
    $("apply-btn").disabled = !Object.keys(pending).length;
  }

  async function loadIndexers({ withAi = false } = {}) {
    const msg = $("indexer-msg");
    const r = await api("/config/prowlarr/indexers");
    if (!r.ok) { msg.hidden = false; msg.textContent = "Couldn't load indexers: " + (r.message || "Prowlarr did not answer"); return; }
    msg.hidden = true;
    renderTable(r.indexers || []);
    $("indexer-table").hidden = false;
    if (withAi && r.ai_available) {
      showToast("Getting AI scores…", "info");
      const ai = await api("/config/prowlarr/indexers/ai");
      if (ai.ok) { renderTable(ai.indexers || []); showToast("AI scores applied", "success"); }
      else showToast("AI scoring unavailable right now", "warning");
    }
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

  Actions["refresh-indexers"] = () => loadIndexers({ withAi: true });

  Actions["rescore-reorder"] = async (btn) => {
    btn.disabled = true;
    const r = await post("/indexers/rescore-reorder");
    btn.disabled = false;
    if (report(r, "Rescored and reordered")) loadIndexers();
  };

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

  Actions["trigger-scoring"] = async (btn) => {
    btn.disabled = true;
    showToast("Scoring started — this can take a minute with AI on.", "info");
    const r = await post("/indexers/trigger-scoring");
    btn.disabled = false;
    if (report(r, "Scoring finished")) { loadIndexers(); loadSchedule(); loadHealthChart(); }
  };

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
