/* system_status.js — connection health, fetched after render so the page is instant. */
"use strict";

(async function () {
  const list = document.getElementById("conn-list");
  if (!list) return;
  const r = await api("/system/status/data");
  list.replaceChildren();

  const row = (name, dotClass, text, textClass) => {
    const dt = document.createElement("dt");
    dt.textContent = name;
    const dd = document.createElement("dd");
    const s = document.createElement("span");
    s.className = "state";
    const dot = document.createElement("span");
    dot.className = "status-dot " + dotClass;
    const label = document.createElement("span");
    label.textContent = " " + text;
    if (textClass) label.className = textClass;
    s.append(dot, label);
    dd.append(s);
    list.append(dt, dd);
  };

  if (!r.ok || !Array.isArray(r.connections)) {
    row("Connections", "danger", r.message || "Check failed", "text-danger");
    return;
  }
  r.connections.forEach((c) => {
    if (!c.configured) row(c.name, "stopped", "Not configured", "text-muted");
    else if (c.ok) row(c.name, "running", "Connected");
    else row(c.name, "danger", "Unreachable", "text-danger");
  });
})();
