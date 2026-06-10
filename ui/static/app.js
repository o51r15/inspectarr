/* inspectarr — frontend helpers */

// ------------------------------------------------------------------ //
// Dashboard live polling                                               //
// ------------------------------------------------------------------ //

function startDashboardPolling() {
  const el = document.getElementById("dashboard-status");
  if (!el) return;

  function update() {
    fetch("/status")
      .then(r => r.json())
      .then(data => {
        // Scheduler state
        const dot    = document.getElementById("status-dot");
        const label  = document.getElementById("status-label");
        const nextEl = document.getElementById("next-run");

        if (dot && label) {
          if (data.scanning) {
            dot.className   = "status-dot scanning";
            label.textContent = "Scanning...";
          } else if (data.running) {
            dot.className   = "status-dot running";
            label.textContent = "Running";
          } else {
            dot.className   = "status-dot stopped";
            label.textContent = "Stopped";
          }
        }

        if (nextEl) {
          nextEl.textContent = data.next_run
            ? formatRelative(data.next_run)
            : "—";
        }

        // Last result stats
        if (data.last_result) {
          const r = data.last_result;
          setInner("stat-checked",  r.torrents_checked ?? "—");
          setInner("stat-flagged",  r.flagged ?? "—");
          setInner("stat-actioned", r.actioned ?? "—");
          setInner("stat-last-run", r.scan_end ? formatDate(r.scan_end) : "—");

          if (r.last_flagged) {
            const lf = r.last_flagged;
            setInner("last-flagged-name", lf.torrent_name || "—");
            setInner("last-flagged-rule", lf.rule || "—");
            setInner("last-flagged-time", lf.timestamp ? formatDate(lf.timestamp) : "—");
          }
        }
      })
      .catch(() => {});
  }

  update();
  setInterval(update, 5000);
}

// ------------------------------------------------------------------ //
// Log auto-refresh                                                     //
// ------------------------------------------------------------------ //

let logRefreshTimer = null;

function toggleLogRefresh(btn) {
  if (logRefreshTimer) {
    clearInterval(logRefreshTimer);
    logRefreshTimer = null;
    btn.textContent = "Auto-refresh: Off";
    btn.classList.remove("btn-success");
    btn.classList.add("btn-ghost");
  } else {
    btn.textContent = "Auto-refresh: On";
    btn.classList.remove("btn-ghost");
    btn.classList.add("btn-success");
    logRefreshTimer = setInterval(refreshLogs, 10000);
  }
}

function refreshLogs() {
  const level  = document.getElementById("level-filter")?.value || "ALL";
  const tbody  = document.getElementById("log-tbody");
  if (!tbody) return;

  fetch(`/logs/data?level=${level}`)
    .then(r => r.json())
    .then(data => {
      tbody.innerHTML = data.entries.map(renderLogRow).join("");
    })
    .catch(() => {});
}

function renderLogRow(e) {
  const badge = levelBadge(e.level);
  const detail = e.torrent_name
    ? `<span class="text-muted">${esc(e.torrent_name)}</span>`
    : (e.reason ? `<span class="text-error">${esc(e.reason)}</span>` : "");
  const files = e.bad_files
    ? e.bad_files.map(f => `<code>${esc(f)}</code>`).join(", ")
    : "";
  return `<tr>
    <td class="mono text-muted">${esc(e.timestamp?.slice(0,19).replace("T"," ") || "")}</td>
    <td>${badge}</td>
    <td>${esc(e.event || "")}</td>
    <td>${detail}</td>
    <td>${files}</td>
  </tr>`;
}

// ------------------------------------------------------------------ //
// Config form — rules builder                                          //
// ------------------------------------------------------------------ //

let ruleCount = 0;

function initRules() {
  const container = document.getElementById("rules-container");
  if (!container) return;
  ruleCount = container.querySelectorAll(".rule-card").length;
}

function addRule() {
  const container = document.getElementById("rules-container");
  const idx = ruleCount++;
  const card = document.createElement("div");
  card.className = "rule-card";
  card.id = `rule-${idx}`;
  card.innerHTML = ruleTemplate(idx, {}, []);
  container.appendChild(card);
}

function removeRule(idx) {
  document.getElementById(`rule-${idx}`)?.remove();
}

function ruleTemplate(idx, rule, exts, patterns, minSize) {
  const name     = rule.name     || "";
  const category = rule.category || "";
  const app      = rule.app      || "sonarr";
  const mode     = rule.match_mode || "any";
  const extTags  = exts.map(e => extTagHtml(idx, e)).join("");
  const patTags  = (patterns || []).map(p => patTagHtml(idx, p)).join("");
  const minSizeVal = minSize || "";
  return `
    <div class="rule-header">
      <strong style="color:var(--text)">Rule #${idx + 1}</strong>
      <button type="button" class="rule-remove" onclick="removeRule(${idx})" title="Remove">×</button>
    </div>
    <div class="form-row">
      <div class="form-group">
        <label>Rule Name</label>
        <input type="text" name="rule_name_${idx}" value="${esc(name)}" placeholder="TV Bad Extensions">
      </div>
      <div class="form-group">
        <label>qBit Category</label>
        <input type="text" name="rule_category_${idx}" value="${esc(category)}" placeholder="tv-sonarr">
      </div>
    </div>
    <div class="form-row">
      <div class="form-group">
        <label>App</label>
        <select name="rule_app_${idx}">
          <option value="sonarr"${app==="sonarr"?" selected":""}>Sonarr</option>
          <option value="radarr"${app==="radarr"?" selected":""}>Radarr</option>
          <option value="lidarr"${app==="lidarr"?" selected":""}>Lidarr</option>
        </select>
      </div>
      <div class="form-group">
        <label>Match Mode</label>
        <select name="rule_match_mode_${idx}">
          <option value="any"${mode==="any"?" selected":""}>Any bad file</option>
          <option value="primary"${mode==="primary"?" selected":""}>Primary file only</option>
        </select>
      </div>
    </div>
    <div class="form-group">
      <label>Bad Extensions</label>
      <input type="hidden" name="rule_extensions_${idx}" id="rule_ext_hidden_${idx}"
             value="${esc(exts.join(","))}">
      <div class="input-with-btn">
        <input type="text" id="rule_ext_input_${idx}" placeholder=".exe" style="max-width:160px">
        <button type="button" class="btn btn-ghost btn-sm" onclick="addExt(${idx})">+ Add</button>
      </div>
      <div class="ext-tags" id="rule_ext_tags_${idx}">${extTags}</div>
    </div>
    <div class="form-row">
      <div class="form-group" style="max-width:220px">
        <label>Min Primary File Size (MB)</label>
        <input type="number" name="rule_min_size_mb_${idx}" value="${esc(minSizeVal)}"
               placeholder="Leave blank to disable" min="1">
        <div class="hint">Flag if primary file is smaller than this</div>
      </div>
    </div>
    <div class="form-group">
      <label>Bad Filename Patterns (regex)</label>
      <input type="hidden" name="rule_patterns_${idx}" id="rule_pat_hidden_${idx}"
             value="${esc((patterns||[]).join(","))}">
      <div class="input-with-btn">
        <input type="text" id="rule_pat_input_${idx}" placeholder="e.g. sample\\." style="max-width:200px">
        <button type="button" class="btn btn-ghost btn-sm" onclick="addPattern(${idx})">+ Add</button>
      </div>
      <div class="ext-tags" id="rule_pat_tags_${idx}">${patTags}</div>
    </div>`;
}

function extTagHtml(ruleIdx, ext) {
  return `<span class="ext-tag">${esc(ext)}
    <button type="button" onclick="removeExt(${ruleIdx},'${esc(ext)}')">×</button>
  </span>`;
}

function addExt(ruleIdx) {
  const input   = document.getElementById(`rule_ext_input_${ruleIdx}`);
  const hidden  = document.getElementById(`rule_ext_hidden_${ruleIdx}`);
  const tagsCon = document.getElementById(`rule_ext_tags_${ruleIdx}`);
  let val = input.value.trim().toLowerCase();
  if (!val) return;
  if (!val.startsWith(".")) val = "." + val;
  const existing = hidden.value ? hidden.value.split(",") : [];
  if (existing.includes(val)) { input.value = ""; return; }
  existing.push(val);
  hidden.value = existing.join(",");
  tagsCon.innerHTML += extTagHtml(ruleIdx, val);
  input.value = "";
}

function removeExt(ruleIdx, ext) {
  const hidden  = document.getElementById(`rule_ext_hidden_${ruleIdx}`);
  const tagsCon = document.getElementById(`rule_ext_tags_${ruleIdx}`);
  const existing = hidden.value ? hidden.value.split(",") : [];
  hidden.value = existing.filter(e => e !== ext).join(",");
  tagsCon.querySelectorAll(".ext-tag").forEach(el => {
    if (el.textContent.trim().startsWith(ext)) el.remove();
  });
}

function patTagHtml(ruleIdx, pat) {
  return `<span class="ext-tag" data-pattern="${esc(pat)}">${esc(pat)}
    <button type="button" onclick="removePattern(${ruleIdx}, this)">×</button>
  </span>`;
}

function addPattern(ruleIdx) {
  const input   = document.getElementById(`rule_pat_input_${ruleIdx}`);
  const hidden  = document.getElementById(`rule_pat_hidden_${ruleIdx}`);
  const tagsCon = document.getElementById(`rule_pat_tags_${ruleIdx}`);
  const val = input.value.trim();
  if (!val) return;
  const existing = hidden.value ? hidden.value.split(",") : [];
  if (existing.includes(val)) { input.value = ""; return; }
  existing.push(val);
  hidden.value = existing.join(",");
  tagsCon.innerHTML += patTagHtml(ruleIdx, val);
  input.value = "";
}

function removePattern(ruleIdx, btn) {
  const tag = btn.closest(".ext-tag");
  const pat = tag.dataset.pattern;
  const hidden  = document.getElementById(`rule_pat_hidden_${ruleIdx}`);
  const existing = hidden.value ? hidden.value.split(",") : [];
  hidden.value = existing.filter(e => e !== pat).join(",");
  tag.remove();
}

// ------------------------------------------------------------------ //
// Config — edit mode toggle                                            //
// ------------------------------------------------------------------ //

function toggleEditMode(mode) {
  document.getElementById("form-view").style.display = mode === "form" ? "" : "none";
  document.getElementById("yaml-view").style.display = mode === "yaml" ? "" : "none";
  document.getElementById("edit_mode").value = mode;
  document.querySelectorAll(".mode-tab").forEach(t => {
    t.classList.toggle("active", t.dataset.mode === mode);
  });
}

// ------------------------------------------------------------------ //
// Test connection                                                      //
// ------------------------------------------------------------------ //

function testConnection(type) {
  const resultEl = document.getElementById(`test-${type}-result`);
  resultEl.textContent = "Testing...";
  resultEl.className   = "hint";

  let payload = {};
  if (type === "qbit") {
    payload = {
      url:      document.querySelector('[name=qbit_url]')?.value,
      username: document.querySelector('[name=qbit_username]')?.value,
      password: document.querySelector('[name=qbit_password]')?.value,
    };
  } else if (type === "sonarr") {
    payload = {
      url:     document.querySelector('[name=sonarr_url]')?.value,
      api_key: document.querySelector('[name=sonarr_api_key]')?.value,
    };
  } else if (type === "radarr") {
    payload = {
      url:     document.querySelector('[name=radarr_url]')?.value,
      api_key: document.querySelector('[name=radarr_api_key]')?.value,
    };
  } else if (type === "lidarr") {
    payload = {
      url:     document.querySelector('[name=lidarr_url]')?.value,
      api_key: document.querySelector('[name=lidarr_api_key]')?.value,
    };
  }

  fetch(`/config/test/${type}`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload),
  })
    .then(r => r.json())
    .then(data => {
      resultEl.textContent = data.message;
      resultEl.className   = data.ok ? "hint text-success" : "hint text-error";
    })
    .catch(() => {
      resultEl.textContent = "Request failed";
      resultEl.className   = "hint text-error";
    });
}

// ------------------------------------------------------------------ //
// Helpers                                                              //
// ------------------------------------------------------------------ //

function setInner(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}

function esc(s) {
  return String(s || "")
    .replace(/&/g,"&amp;")
    .replace(/</g,"&lt;")
    .replace(/>/g,"&gt;")
    .replace(/"/g,"&quot;");
}

function formatDate(iso) {
  try { return new Date(iso).toLocaleString(); } catch { return iso; }
}

function formatRelative(iso) {
  try {
    const diff = Math.round((new Date(iso) - Date.now()) / 1000);
    if (diff <= 0) return "now";
    const m = Math.floor(diff / 60), s = diff % 60;
    return m > 0 ? `${m}m ${s}s` : `${s}s`;
  } catch { return iso; }
}

function levelBadge(level) {
  const map = {
    ACTION:  "badge-action",
    ERROR:   "badge-error",
    DRY_RUN: "badge-dry-run",
    INFO:    "badge-info",
    DEBUG:   "badge-debug",
  };
  return `<span class="badge ${map[level] || "badge-info"}">${level || "INFO"}</span>`;
}

// ------------------------------------------------------------------ //
// Toasts                                                               //
// ------------------------------------------------------------------ //

function showToast(message, level = "info", duration = 3500) {
  const container = document.getElementById("toast-container");
  if (!container) return;
  const toast = document.createElement("div");
  toast.className = `toast toast-${level}`;
  toast.textContent = message;
  container.appendChild(toast);
  requestAnimationFrame(() => {
    requestAnimationFrame(() => toast.classList.add("toast-show"));
  });
  setTimeout(() => {
    toast.classList.remove("toast-show");
    toast.addEventListener("transitionend", () => toast.remove(), { once: true });
  }, duration);
}

function _consumeToastParam() {
  const params = new URLSearchParams(window.location.search);
  const message = params.get("toast");
  const level   = params.get("level") || "info";
  if (!message) return;
  showToast(message, level);
  // Strip the toast params from the URL so a refresh doesn't re-fire
  params.delete("toast");
  params.delete("level");
  const newSearch = params.toString();
  const newUrl = window.location.pathname + (newSearch ? "?" + newSearch : "");
  history.replaceState(null, "", newUrl);
}

// ------------------------------------------------------------------ //
// Init                                                                 //
// ------------------------------------------------------------------ //

document.addEventListener("DOMContentLoaded", () => {
  startDashboardPolling();
  initRules();
  _consumeToastParam();
});
