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
      .then(data => {        // Scheduler state
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
          const r = data.last_result;          setInner("stat-checked",  r.torrents_checked ?? "—");
          setInner("stat-last-run", r.scan_end ? formatDate(r.scan_end) : "—");
        }

        // Total historical stats
        setInner("stat-flagged",  data.total_flagged ?? 0);
        setInner("stat-actioned", data.total_actioned ?? 0);

        // Last detection (persisted across clean scans)
        if (data.last_detection) {
          const lf = data.last_detection;
          setInner("last-flagged-name", lf.torrent_name || "—");
          setInner("last-flagged-rule", lf.rule || "—");
          setInner("last-flagged-time", lf.timestamp ? formatDate(lf.timestamp) : "—");
        }
      })
      .catch(() => {});
  }

  update();
  setInterval(update, 5000);
}

// ------------------------------------------------------------------ //
// Helpers                                                              //
// ------------------------------------------------------------------ //

function setInner(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}

function esc(s) {
  return String(s ?? "")
    .replace(/&/g,"&amp;")
    .replace(/</g,"&lt;")
    .replace(/>/g,"&gt;")
    .replace(/"/g,"&quot;")
    .replace(/'/g,"&#x27;")
    .replace(/`/g,"&#x60;");
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
  _consumeToastParam();
});