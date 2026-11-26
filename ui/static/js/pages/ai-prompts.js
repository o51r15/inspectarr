/* ai-prompts.js -- Settings -> AI -> Scoring instructions.
 *
 * The Default (read-only), people's own prompts, which one scores, and
 * "Test this prompt". Loaded before settings.js, which calls
 * loadSystemPrompt() when the AI tab opens; that name is exported for it.
 *
 * Nothing here is part of the Settings form: prompts save through their own
 * buttons, so typing in the editor must not mark the page as unsaved.
 */
"use strict";

(function () {
  const S = { data: null, selected: 0, testOpen: false };
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function status(text, ok) {
    const el = $("system-prompt-status");
    if (!el) return;
    el.textContent = text || "";
    el.className = "hint" + (ok === true ? " ms-good" : ok === false ? " ms-bad" : "");
  }

  function selectedRow() {
    return (S.data && S.data.prompts || []).find((p) => p.id === S.selected) || null;
  }

  function loadPrompts(keepSelection) {
    return fetch("/config/ai/prompts").then((r) => r.json()).then((d) => {
      if (!d.ok) { status(d.message || "Could not load the prompts", false); return; }
      const first = !S.data;
      S.data = d;
      // First load, or the chosen prompt is gone: show the one in use.
      if (first || !keepSelection || (S.selected && !selectedRow())) S.selected = d.active_id || 0;
      render();
      loadTests();
    }).catch(() => status("Could not load the prompts", false));
  }

  function render() {
    const d = S.data;
    if (!d) return;
    const sel = $("ai-prompt-select");
    if (sel) {
      sel.innerHTML = [{ id: 0, label: "Default" }].concat(d.prompts).map((p) =>
        `<option value="${p.id}"${p.id === S.selected ? " selected" : ""}>${esc(p.label)}` +
        `${p.id === (d.active_id || 0) ? " — in use" : ""}</option>`).join("");
    }
    const inUse = $("ai-prompt-in-use");
    if (inUse) inUse.textContent = "In use: " + d.active_label;
    const row = selectedRow();
    const box = $("ollama-system-prompt");
    if (box) {
      box.value = row ? row.text : d.default_instructions;
      box.readOnly = !row;
      box.title = row ? "" : "The Default can't be edited. Use New prompt to make your own copy.";
    }
    const nameRow = $("ai-prompt-name-row");
    if (nameRow) nameRow.style.display = row ? "" : "none";
    const name = $("ai-prompt-name");
    if (name) name.value = row ? row.name : "";
    const fmt = $("ai-answer-format");
    if (fmt) fmt.textContent = d.answer_format;
    const isActive = S.selected === (d.active_id || 0);
    document.querySelectorAll('[data-action="ai-prompt-save"], [data-action="ai-prompt-delete"], ' +
      '[data-action="ai-prompt-test-open"]').forEach((b) => { b.disabled = !row; });
    const use = document.querySelector('[data-action="ai-prompt-use"]');
    if (use) {
      use.disabled = isActive;
      use.textContent = isActive ? "In use for scoring" : "Use for scoring";
    }
    renderTestPanel();
  }

  function post(url, body) {
    return fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                        body: JSON.stringify(body || {}) })
      .then((r) => r.json().then((d) => ({ code: r.status, d })));
  }

  function newPrompt() {
    post("/config/ai/prompts", {}).then((res) => {
      if (!res.d.ok) { status(res.d.message, false); return; }
      S.selected = res.d.id;
      loadPrompts(true).then(() => { status("New prompt: a copy of the Default. Change it, then Save.", true);
                                     const b = $("ollama-system-prompt"); if (b) b.focus(); });
    });
  }

  function save() {
    const row = selectedRow();
    if (!row) return;
    post(`/config/ai/prompts/${row.id}`, { text: $("ollama-system-prompt").value, name: $("ai-prompt-name").value })
      .then((res) => {
        status(res.d.ok ? "✓ Saved" + (row.id === S.data.active_id ? " — used from the next scoring run" : "")
                        : "✗ " + res.d.message, res.d.ok);
        if (res.d.ok) loadPrompts(true);
      });
  }

  function useIt() {
    post("/config/ai/prompts/use", { id: S.selected }).then((res) => {
      status(res.d.ok ? "✓ " + res.d.active_label + " is used for scoring from the next run"
                      : "✗ " + res.d.message, res.d.ok);
      if (res.d.ok) loadPrompts(true);
    });
  }

  function del() {
    const row = selectedRow();
    if (!row) return;
    if (!confirm("Delete " + row.label + "? Its test results go with it." +
                 (row.in_use ? "\n\nIt is in use: the Default will be used instead." : ""))) return;
    post(`/config/ai/prompts/${row.id}/delete`).then(() => {
      S.selected = 0; status("Deleted", true); loadPrompts(false);
    });
  }

  // --- Test this prompt ------------------------------------------------------------
  function calibratedModels() {
    return fetch("/config/ai/validations").then((r) => r.json())
      .then((d) => (d.validations || []).filter((v) => v.calibrated && v.quality_score != null)
        .sort((a, b) => b.quality_score - a.quality_score));
  }

  function renderTestPanel() {
    const box = $("ai-prompt-test-panel");
    if (!box) return;
    if (!S.testOpen || !selectedRow()) { box.style.display = "none"; box.innerHTML = ""; return; }
    box.style.display = "";
    box.innerHTML = '<p class="hint">Loading models…</p>';
    calibratedModels().then((list) => {
      if (!list.length) {
        box.innerHTML = '<p class="m-0">Calibrate a model first: a prompt is tested on models ' +
          "that have a Default score here, so you can compare the two.</p>";
        return;
      }
      box.innerHTML = '<p class="m-0 mb-8">Score the 14 test indexers with this prompt, once per model, ' +
        "with each model's calibrated settings. Results show below, next to the model's Default score.</p>" +
        '<p class="hint m-0 mb-8">The test grades answers against the Default\'s points table. If your prompt ' +
        "changes the points on purpose, a lower score means it disagrees with the built-in table, not that " +
        "it is worse.</p>" +
        list.slice(0, 12).map((v, i) => `<label class="block"><input type="checkbox" class="ai-prompt-pick" ` +
          `value="${esc(v.model)}"${i < 3 ? " checked" : ""}> ${esc(v.model)} ` +
          `<span class="hint">(Default ${Math.round(v.quality_score)})</span></label>`).join("") +
        '<div class="flex gap-8 mt-8"><button type="button" class="btn btn-primary btn-sm" ' +
        'data-action="ai-prompt-test-run">Test</button><button type="button" class="btn btn-ghost btn-sm" ' +
        'data-action="ai-prompt-test-close">Cancel</button></div>';
    });
  }

  function runTest() {
    const row = selectedRow();
    const models = Array.from(document.querySelectorAll(".ai-prompt-pick")).filter((c) => c.checked).map((c) => c.value);
    if (!row || !models.length) return;
    post(`/config/ai/prompts/${row.id}/test`, { models }).then((res) => {
      if (!res.d.ok) { status("✗ " + res.d.message, false); return; }
      S.testOpen = false; renderTestPanel();
      status("Testing on " + models.length + " model" + (models.length === 1 ? "" : "s") +
             ". It runs in the background with other AI work; results appear below.", true);
      pollTests();
    });
  }

  let poll = null;
  function pollTests() {
    if (poll) clearInterval(poll);
    poll = setInterval(() => {
      loadTests();
      fetch("/config/ai/validate/status").then((r) => r.json()).then((s) => {
        if (!s.running) { clearInterval(poll); poll = null; loadTests(); }
      }).catch(() => { clearInterval(poll); poll = null; });
    }, 5000);
  }

  function loadTests() {
    const box = $("ai-prompt-tests");
    const row = selectedRow();
    if (!box) return;
    if (!row) { box.innerHTML = ""; return; }
    fetch(`/config/ai/prompts/${row.id}/tests`).then((r) => r.json()).then((d) => {
      const tests = d.tests || [];
      if (!tests.length) { box.innerHTML = '<p class="hint m-0">Not tested yet.</p>'; return; }
      box.innerHTML = '<table class="data-table"><caption class="sr-only">Test results for this prompt</caption>' +
        "<thead><tr><th>Model</th><th>This prompt</th><th>Default</th><th></th></tr></thead><tbody>" +
        tests.map((t) => {
          const diff = t.score != null && t.default_score != null ? t.score - t.default_score : null;
          return `<tr><td class="mono">${esc(t.model)}</td>` +
            `<td>${t.score != null ? Math.round(t.score) + "/100" : "—"}${t.complete ? "" : " ✗ left indexers out"}</td>` +
            `<td>${t.default_score != null ? Math.round(t.default_score) + "/100" : "—"}` +
            (diff != null ? ` <span class="hint">(${diff >= 0 ? "+" : ""}${Math.round(diff)})</span>` : "") + "</td>" +
            `<td class="hint">${t.outdated ? "earlier version of this prompt" : ""}</td></tr>`;
        }).join("") + "</tbody></table>";
    }).catch(() => {});
  }

  // --- wiring ----------------------------------------------------------------------
  Actions["ai-prompt-new"] = () => newPrompt();
  Actions["ai-prompt-save"] = () => save();
  Actions["ai-prompt-use"] = () => useIt();
  Actions["ai-prompt-delete"] = () => del();
  Actions["ai-prompt-test-open"] = () => { S.testOpen = true; renderTestPanel(); };
  Actions["ai-prompt-test-close"] = () => { S.testOpen = false; renderTestPanel(); };
  Actions["ai-prompt-test-run"] = () => runTest();

  function choose(value) {
    S.selected = parseInt(value, 10) || 0;
    S.testOpen = false;
    status("");
    render();
    loadTests();
  }
  // The prompt editor is not part of the Settings form: keep its typing from
  // marking the page unsaved (the form listens for input/change events).
  // Stopping in the capture phase also hides the events from any other
  // listener, so the prompt picker is handled here, before stopping.
  ["input", "change"].forEach((evt) => document.addEventListener(evt, (e) => {
    const sec = $("ai-prompts-section");
    if (!sec || !sec.contains(e.target)) return;
    if (evt === "change" && e.target.id === "ai-prompt-select") choose(e.target.value);
    e.stopPropagation();
  }, true));

  window.loadSystemPrompt = () => loadPrompts(true);
})();
