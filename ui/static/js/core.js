/* core.js — loaded on every page.
 *
 * - One delegated click listener: markup says data-action="name", pages
 *   register Actions.name = (el, event) => {...}. No inline handler attributes.
 * - api(): fetch wrapper that always resolves to an object with .ok.
 * - confirmDialog(): styled, keyboard-accessible replacement for confirm().
 * - toasts, relative times, theme toggle, mobile sidebar.
 *
 * Older pages still call showToast/esc/formatDate/formatRelative/setInner as
 * globals, so those names are kept.
 */
"use strict";

const Actions = Object.create(null);
window.Actions = Actions;

document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-action]");
  if (!el) return;
  const fn = Actions[el.dataset.action];
  if (!fn) { console.warn("No handler for data-action", el.dataset.action); return; }
  e.preventDefault();
  fn(el, e);
});

async function api(url, { method = "GET", body } = {}) {
  let res;
  try {
    res = await fetch(url, {
      method,
      headers: body !== undefined ? { "Content-Type": "application/json" } : {},
      body: body !== undefined ? JSON.stringify(body) : undefined,
      credentials: "same-origin",
    });
  } catch (err) {
    return { ok: false, status: 0, message: "Network error — is Inspectarr running?" };
  }
  let data = {};
  try { data = await res.json(); } catch (_) { /* non-JSON response */ }
  if (data.ok === undefined) data.ok = res.ok;
  return { status: res.status, ...data };
}
window.api = api;

function confirmDialog({ title, body, confirm = "Confirm", tone = "danger" }) {
  return new Promise((resolve) => {
    const d = document.getElementById("confirm-dialog");
    if (!d || typeof d.showModal !== "function") { resolve(window.confirm(title + "\n\n" + body)); return; }
    d.querySelector("[data-title]").textContent = title;
    d.querySelector("[data-body]").textContent = body;
    const ok = d.querySelector("[data-ok]");
    ok.textContent = confirm;
    ok.className = "btn btn-" + tone;
    d.returnValue = "";
    d.addEventListener("close", () => resolve(d.returnValue === "ok"), { once: true });
    d.showModal();
  });
}
window.confirmDialog = confirmDialog;

function pageData() {
  const el = document.getElementById("page-data");
  if (!el) return {};
  try { return JSON.parse(el.textContent); } catch (_) { return {}; }
}
window.pageData = pageData;

/* ---------------- helpers kept as globals for older pages ---------------- */

function esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#x27;").replace(/`/g, "&#x60;");
}
function setInner(id, val) { const el = document.getElementById(id); if (el) el.textContent = val; }
function formatDate(iso) { try { return new Date(iso).toLocaleString(); } catch (_) { return iso; } }

/* Time until a future instant ("4m 10s"); used for next-run countdowns. */
function formatRelative(iso) {
  try {
    const diff = Math.round((new Date(iso) - Date.now()) / 1000);
    if (diff <= 0) return "now";
    const m = Math.floor(diff / 60), s = diff % 60;
    return m > 0 ? `${m}m ${s}s` : `${s}s`;
  } catch (_) { return iso; }
}

/* Human "x ago" / "in x" for any instant. */
function timeAgo(iso) {
  const t = new Date(iso);
  if (isNaN(t)) return null;
  const sec = Math.round((Date.now() - t) / 1000);
  const abs = Math.abs(sec);
  const fmt = (n, unit) => `${n} ${unit}${n === 1 ? "" : "s"}`;
  let txt;
  if (abs < 45) txt = "just now";
  else if (abs < 3600) txt = fmt(Math.round(abs / 60), "min");
  else if (abs < 86400) txt = fmt(Math.round(abs / 3600), "hour");
  else if (abs < 86400 * 30) txt = fmt(Math.round(abs / 86400), "day");
  else return t.toLocaleDateString();
  if (txt === "just now") return txt;
  return sec >= 0 ? `${txt} ago` : `in ${txt}`;
}
window.timeAgo = timeAgo;

function upgradeTimes(root = document) {
  root.querySelectorAll("time[data-relative]").forEach((el) => {
    const iso = el.getAttribute("datetime");
    const rel = timeAgo(iso);
    if (!rel) return;
    el.textContent = rel;
    try { el.title = new Date(iso).toLocaleString(); } catch (_) { /* keep fallback */ }
  });
}
window.upgradeTimes = upgradeTimes;

/* ---------------- toasts ---------------- */

function showToast(message, level = "info", duration = 3500) {
  const container = document.getElementById("toast-container");
  if (!container) return;
  const toast = document.createElement("div");
  toast.className = `toast toast-${level}`;
  toast.setAttribute("role", level === "error" ? "alert" : "status");
  toast.textContent = message;
  container.appendChild(toast);
  requestAnimationFrame(() => requestAnimationFrame(() => toast.classList.add("toast-show")));
  setTimeout(() => {
    toast.classList.remove("toast-show");
    setTimeout(() => toast.remove(), 300);
  }, duration);
}
window.showToast = showToast;

function consumeToastParam() {
  const params = new URLSearchParams(window.location.search);
  const message = params.get("toast");
  if (!message) return;
  showToast(message, params.get("level") || "info");
  params.delete("toast");
  params.delete("level");
  const qs = params.toString();
  history.replaceState(null, "", window.location.pathname + (qs ? "?" + qs : "") + window.location.hash);
}

/* ---------------- shell: theme + mobile sidebar ---------------- */

function currentTheme() { return document.documentElement.getAttribute("data-theme") || "dark"; }

function syncThemeButton() {
  const use = document.querySelector("#theme-btn use");
  if (!use) return;
  const href = use.getAttribute("href") || "";
  // Show the theme you would switch TO.
  use.setAttribute("href", href.replace(/#.*$/, currentTheme() === "dark" ? "#sun" : "#moon"));
}

Actions["toggle-theme"] = () => {
  const next = currentTheme() === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  try { localStorage.setItem("inspectarr-theme", next); } catch (_) { /* storage blocked */ }
  syncThemeButton();
};

Actions["open-sidebar"] = () => {
  document.querySelector(".sidebar")?.classList.add("open");
  document.getElementById("sidebar-overlay")?.classList.add("visible");
};
Actions["close-sidebar"] = () => {
  document.querySelector(".sidebar")?.classList.remove("open");
  document.getElementById("sidebar-overlay")?.classList.remove("visible");
};

/* Scheduler buttons in the topbar work on every page. */
Actions["run-now"] = async (el) => {
  el.disabled = true;
  const r = await api("/scheduler/run.json", { method: "POST" });
  showToast(r.message || (r.ok ? "Scan started" : "Could not start a scan"), r.ok ? "success" : "warning");
  setTimeout(() => { el.disabled = false; }, 3000);
  document.dispatchEvent(new CustomEvent("inspectarr:refresh"));
};

Actions["toggle-scheduler"] = async (el) => {
  el.disabled = true;
  const r = await api("/scheduler/toggle.json", { method: "POST" });
  if (r.ok) {
    showToast(r.running ? "Scanning resumed" : "Scanning paused", r.running ? "success" : "info");
    setTimeout(() => location.reload(), 400);
  } else {
    showToast(r.message || "Could not change the scheduler", "error");
    el.disabled = false;
  }
};

/* Widths that used to be inline width styles: markup carries
   data-pct="NN" and this sets the --pct custom property through CSSOM,
   which a Content-Security-Policy permits (style attributes it does not). */
function applyPercents(root = document) {
  root.querySelectorAll("[data-pct]").forEach((el) => {
    const n = Math.max(0, Math.min(100, parseFloat(el.dataset.pct) || 0));
    el.style.setProperty("--pct", n + "%");
  });
}
window.applyPercents = applyPercents;

document.addEventListener("DOMContentLoaded", () => {
  applyPercents();
  syncThemeButton();
  upgradeTimes();
  consumeToastParam();
  document.querySelectorAll(".sidebar .nav-item").forEach((a) =>
    a.addEventListener("click", Actions["close-sidebar"]));
  setInterval(upgradeTimes, 60000);
});
