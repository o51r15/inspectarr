/* overview.js — Overview page: connection chips and light live polling.
 *
 * The server renders the page assuming every service is reachable, so it
 * appears instantly. This script then asks /system/status/data (all checks
 * run in parallel server-side) and, if anything configured is down, the
 * hero switches to the outage message: an outage always wins the headline.
 */
"use strict";

(function () {
  const chips = document.getElementById("connections");
  const data = pageData();

  function chip(name, state) {
    const el = document.createElement("span");
    el.className = "chip";
    const dot = document.createElement("span");
    dot.className = "status-dot " + (state === "ok" ? "ok" : state === "down" ? "danger" : "stopped");
    const label = document.createElement("span");
    label.textContent = name;
    el.append(dot, label);
    if (state === "down") el.title = name + " is not responding";
    if (state === "off") el.title = name + " is not set up";
    return el;
  }

  function showOutage(down) {
    const hero = document.getElementById("hero");
    if (!hero) return;
    hero.className = "hero hero-danger";
    document.getElementById("hero-title").textContent = "Can't reach " + down.join(", ");
    document.getElementById("hero-detail").textContent = "Scans will fail until the connection is fixed.";
    const action = document.getElementById("hero-action");
    action.replaceChildren();
    const a = document.createElement("a");
    a.className = "btn btn-secondary";
    a.href = "/system/status";
    a.textContent = "Check connections";
    action.append(a);
    const use = hero.querySelector(".hero-mark use");
    if (use) use.setAttribute("href", use.getAttribute("href").replace(/#.*$/, "#plug"));
  }

  async function loadConnections() {
    const r = await api("/system/status/data");
    chips.replaceChildren();
    if (!r.ok || !Array.isArray(r.connections)) {
      const err = document.createElement("span");
      err.className = "text-sm text-muted";
      err.textContent = "Couldn't check connections right now.";
      chips.append(err);
      return;
    }
    const configured = r.connections.filter((c) => c.configured);
    const down = configured.filter((c) => !c.ok).map((c) => c.name.replace(/\s*\(.*\)$/, ""));
    configured.forEach((c) => chips.append(chip(c.name.replace(/\s*\(.*\)$/, ""), c.ok ? "ok" : "down")));
    if (!configured.length) {
      const none = document.createElement("span");
      none.className = "text-sm text-muted";
      none.textContent = "No services configured yet — add them in Settings → Connections.";
      chips.append(none);
    }
    if (down.length) showOutage(down);
  }

  /* Poll /status so the scanning dot, counts and held badge stay current.
     Every 10 s, and not at all while the tab is hidden. */
  let lastScanning = null;
  async function poll() {
    if (document.visibilityState !== "visible") return;
    const s = await api("/status");
    if (!s || s.ok === false) return;
    const checked = document.getElementById("stat-checked");
    if (checked && s.last_result && s.last_result.torrents_checked != null) {
      checked.textContent = s.last_result.torrents_checked;
    }
    const held = document.getElementById("stat-held");
    if (held && typeof s.held === "number") held.textContent = s.held;
    // A scan finishing or the held count changing changes the hero; reload
    // rather than duplicate the server's decision logic in the browser.
    if ((lastScanning === true && s.scanning === false) ||
        (typeof s.held === "number" && s.held !== data.held) ||
        (typeof s.running === "boolean" && s.running !== data.running)) {
      location.reload();
      return;
    }
    lastScanning = s.scanning;
  }

  document.addEventListener("inspectarr:refresh", () => setTimeout(poll, 1500));
  loadConnections();
  setInterval(poll, 10000);
})();
