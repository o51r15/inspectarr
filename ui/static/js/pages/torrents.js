/* torrents.js — category changes, pause/resume, delete and on-page filters.
 * Endpoints are unchanged: /torrents/set-category, /pause, /resume, /delete. */
"use strict";

(function () {
  const STOPPED = ["pausedDL", "pausedUP", "stoppedDL", "stoppedUP"];
  const GROUPS = {
    downloading: ["downloading", "metaDL", "allocating", "forcedDL"],
    seeding: ["uploading", "forcedUP"],
    paused: STOPPED,
    stalled: ["stalledDL", "stalledUP"],
    error: ["error", "missingFiles", "unknown"],
  };

  const rowOf = (el) => el.closest("tr");
  const hashOf = (el) => rowOf(el).dataset.hash;

  /* ---- category ---- */
  document.querySelectorAll(".cat-select").forEach((sel) => {
    sel.addEventListener("change", () => {
      const btn = sel.parentElement.querySelector(".apply-btn");
      btn.disabled = sel.value === sel.dataset.original;
    });
  });

  Actions["apply-category"] = async (btn) => {
    const sel = btn.parentElement.querySelector(".cat-select");
    btn.disabled = true;
    const r = await api("/torrents/set-category", { method: "POST", body: { hash: hashOf(btn), category: sel.value } });
    if (r.ok) {
      sel.dataset.original = sel.value;
      rowOf(btn).dataset.category = sel.value;
      showToast("Category saved", "success");
    } else {
      btn.disabled = false;
      showToast("Couldn't save: " + (r.msg || r.message || "request failed"), "error");
    }
  };

  /* ---- pause / resume: simplest correct thing is to reload the row's
     state from the server, so the page re-renders it with the shared badge. */
  async function setRunning(btn, action) {
    btn.disabled = true;
    const r = await api("/torrents/" + action, { method: "POST", body: { hash: hashOf(btn) } });
    if (r.ok) {
      showToast(action === "pause" ? "Paused" : "Resumed", "success");
      setTimeout(() => location.reload(), 500);
    } else {
      btn.disabled = false;
      showToast("Failed: " + (r.msg || r.message || "request failed"), "error");
    }
  }
  Actions["torrent-pause"] = (btn) => setRunning(btn, "pause");
  Actions["torrent-resume"] = (btn) => setRunning(btn, "resume");

  /* ---- delete ---- */
  Actions["torrent-delete"] = async (btn) => {
    const ok = await confirmDialog({
      title: "Delete this torrent and its files?",
      body: `${btn.dataset.name} will be removed from the torrent client along with its downloaded files. `
          + "It is NOT blocklisted in Sonarr/Radarr, so it may be grabbed again. This can't be undone.",
      confirm: "Delete",
      tone: "danger",
    });
    if (!ok) return;
    btn.disabled = true;
    const r = await api("/torrents/delete", { method: "POST", body: { hash: hashOf(btn) } });
    if (r.ok) {
      rowOf(btn).remove();
      countVisible();
      showToast("Deleted", "info");
    } else {
      btn.disabled = false;
      showToast("Failed: " + (r.msg || r.message || "request failed"), "error");
    }
  };

  /* ---- filters (this page only) ---- */
  function countVisible() {
    const n = [...document.querySelectorAll("#torrent-table tbody tr")].filter((tr) => !tr.hidden).length;
    const c = document.getElementById("visible-count");
    if (c) c.textContent = n;
  }

  function applyFilters() {
    const status = document.getElementById("filter-status").value;
    const cat = document.getElementById("filter-category").value;
    document.querySelectorAll("#torrent-table tbody tr").forEach((tr) => {
      const okStatus = !status || (GROUPS[status] || []).includes(tr.dataset.state);
      const okCat = !cat || (cat === "__none__" ? !tr.dataset.category : tr.dataset.category === cat);
      tr.hidden = !(okStatus && okCat);
    });
    countVisible();
  }
  document.querySelectorAll("[data-filter]").forEach((s) => s.addEventListener("change", applyFilters));
})();
