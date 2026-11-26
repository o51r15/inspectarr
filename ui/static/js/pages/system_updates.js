/* system_updates.js — compare the running version with the latest GitHub release. */
"use strict";

(function () {
  const $ = (id) => document.getElementById(id);

  function alertBox(tone, text, link) {
    const box = document.createElement("div");
    box.className = "alert alert-" + tone;
    const span = document.createElement("span");
    span.textContent = text;
    box.append(span);
    if (link) {
      const a = document.createElement("a");
      a.href = link;
      a.target = "_blank";
      a.rel = "noopener";
      a.className = "ml-auto";
      a.textContent = "View release";
      box.append(a);
    }
    return box;
  }

  async function check() {
    $("update-loading").hidden = false;
    $("update-result").hidden = true;
    const d = await api("/system/updates/check");
    $("update-loading").hidden = true;
    $("update-result").hidden = false;
    $("ver-current").textContent = d.current || "?";
    $("ver-latest").textContent = d.latest || d.current || "?";
    const badge = $("update-badge");
    badge.replaceChildren();
    if (!d.ok) {
      badge.append(alertBox("danger", "Couldn't check: " + (d.message || "GitHub did not answer")));
      return;
    }
    // Only offer a link that points at GitHub; the URL comes from its API.
    const url = typeof d.url === "string" && d.url.startsWith("https://github.com/") ? d.url : null;
    badge.append(d.up_to_date
      ? alertBox("ok", "You're running the latest version.")
      : alertBox("warn", "Update available: " + d.latest, url));
    if (d.release_notes) {
      $("release-notes").hidden = false;
      $("release-body").textContent = d.release_notes;
    }
  }

  Actions["check-updates"] = check;
  check();
})();
