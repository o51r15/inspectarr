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

function ruleTemplate(idx, rule, exts) {
  const name     = rule.name     || "";
  const category = rule.category || "";
  const app      = rule.app      || "sonarr";
  const mode     = rule.match_mode || "any";
  const extTags  = exts.map(e => extTagHtml(idx, e)).join("");
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
          <option value="radarr"${app==="radarr"?" selected":""} disabled>Radarr (v2)</option>
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
// Init                                                                 //
// ------------------------------------------------------------------ //

document.addEventListener("DOMContentLoaded", () => {
  startDashboardPolling();
  initRules();
});
