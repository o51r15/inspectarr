/* logs.js — System > Events: live stream (SSE) and clearing the log. */
"use strict";

(function () {
  const TONES = { ACTION: "ok", ERROR: "danger", WARNING: "warn", DRY_RUN: "violet", DEBUG: "neutral" };
  const tbody = document.getElementById("log-tbody");
  let source = null;
  let paused = false;

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  /* Every value from the log is set with textContent, never innerHTML. */
  function row(entry) {
    const tr = el("tr", "row-new");
    const level = String(entry.level || "INFO");

    const when = el("td", "text-sm nowrap");
    const t = el("time", null, (entry.timestamp || "").slice(0, 16).replace("T", " ") || "—");
    if (entry.timestamp) { t.setAttribute("datetime", entry.timestamp); t.dataset.relative = ""; }
    when.append(t);

    const lv = el("td");
    lv.append(el("span", "badge badge-" + (TONES[level] || "info"), level.replace(/_/g, " ")));

    const ev = el("td", "hide-sm mono text-xs", entry.event || "");

    const detail = el("td", "cell-truncate");
    if (entry.torrent_name) detail.append(el("span", null, entry.torrent_name));
    else if (entry.reason) detail.append(el("span", "text-danger", entry.reason));
    else if (entry.category) detail.append(el("span", "text-muted", "category: " + entry.category));

    const files = el("td", "hide-sm");
    (Array.isArray(entry.bad_files) ? entry.bad_files : []).forEach((f) => files.append(el("code", "chip-code", f)));

    tr.append(when, lv, ev, detail, files);
    upgradeTimes(tr);
    setTimeout(() => tr.classList.remove("row-new"), 2000);
    return tr;
  }

  function setLive(on, label) {
    const btn = document.getElementById("stream-btn");
    document.getElementById("live-dot").className = "status-dot " + (on ? "running" : "stopped");
    document.getElementById("live-label").textContent = label || (on ? "Live on" : "Live off");
    btn.setAttribute("aria-pressed", String(on));
  }

  Actions["toggle-live"] = () => {
    if (source) { source.close(); source = null; setLive(false); return; }
    const level = document.getElementById("level-filter").value;
    source = new EventSource("/logs/stream?level=" + encodeURIComponent(level));
    setLive(true);
    source.onmessage = (e) => {
      setLive(true);
      if (paused) return;
      let entry;
      try { entry = JSON.parse(e.data); } catch (_) { return; }
      tbody.insertBefore(row(entry), tbody.firstChild);
      while (tbody.children.length > 200) tbody.removeChild(tbody.lastChild);
    };
    source.onerror = () => setLive(true, "Reconnecting…");
  };

  Actions["clear-log"] = async () => {
    const ok = await confirmDialog({
      title: "Clear the event log?",
      body: "Every entry in the log file is deleted. Catches in Activity are not affected. This can't be undone.",
      confirm: "Clear log",
      tone: "danger",
    });
    if (ok) document.getElementById("clear-log-form").submit();
  };

  tbody.addEventListener("mouseenter", () => { paused = true; });
  tbody.addEventListener("mouseleave", () => { paused = false; });
})();
