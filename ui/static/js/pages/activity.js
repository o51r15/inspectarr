/* activity.js — Needs review decisions and History row details. */
"use strict";

(function () {
  /* ---------------- Needs review ----------------
     Decisions POST to the unchanged /quarantine/action endpoint. A row is
     removed only after the server confirms; a failed resume leaves it held
     and it must stay visible. */
  async function decide(el, action) {
    const hash = el.dataset.hash;
    const name = el.dataset.name || "this release";
    if (action === "remediate") {
      const ok = await confirmDialog({
        title: "Delete this release?",
        body: `${name} will be blocklisted in your arr app and deleted from the torrent client, with its files. This can't be undone.`,
        confirm: "Delete",
        tone: "danger",
      });
      if (!ok) return;
    }
    const row = document.getElementById("row-" + hash);
    row?.querySelectorAll("button").forEach((b) => { b.disabled = true; });
    const r = await api("/quarantine/action", { method: "POST", body: { hash, action } });
    showToast(r.message || (r.ok ? "Done" : "Something went wrong"), r.ok ? "success" : "error", 5000);
    if (r.ok) {
      row?.remove();
      const body = document.querySelector("tbody");
      if (body && !body.children.length) setTimeout(() => location.reload(), 900);
    } else {
      row?.querySelectorAll("button").forEach((b) => { b.disabled = false; });
    }
  }
  Actions["q-release"] = (el) => decide(el, "release");
  Actions["q-keep"] = (el) => decide(el, "keep");
  Actions["q-remediate"] = (el) => decide(el, "remediate");

  /* ---------------- History details ---------------- */
  function renderDetail(box, d) {
    box.replaceChildren();
    const add = (tag, cls, text) => {
      const n = document.createElement(tag);
      if (cls) n.className = cls;
      if (text != null) n.textContent = text;
      return n;
    };
    if (!d.ok) { box.append(add("span", "text-danger", d.message || "Couldn't load details")); return; }

    const facts = add("div", "flex flex-wrap gap-16 text-muted mb-12");
    [["App", d.arr_app], ["Indexer", d.indexer], ["Category", d.category],
     ["Files in release", d.file_count], ["Risk score", d.risk_score]].forEach(([k, v]) => {
      if (v === null || v === undefined || v === "") return;
      const f = add("span", null, k + ": ");
      f.append(add("span", "text", String(v)));
      facts.append(f);
    });
    box.append(facts);
    if (d.action_detail) box.append(add("p", "text-muted mb-12", d.action_detail));

    if (!d.reasons || !d.reasons.length) {
      box.append(add("p", "text-dim", "No individual reasons were recorded for this catch."));
      return;
    }
    const list = add("ul", "reason-list");
    d.reasons.forEach((r) => {
      const li = add("li");
      const sev = (r.severity || "").toLowerCase();
      const tone = { critical: "danger", high: "danger", medium: "warn", low: "info" }[sev] || "neutral";
      li.append(add("span", "badge badge-" + tone, r.severity || "—"));
      li.append(add("span", "font-medium", " " + (r.signal || "").replace(/_/g, " ")));
      if (r.detail) li.append(add("span", "text-muted", " — " + r.detail));
      if (r.file_path) li.append(add("code", "reason-file", r.file_path));
      list.append(li);
    });
    box.append(list);
  }

  Actions["toggle-detail"] = async (el) => {
    const id = el.dataset.id;
    const detail = document.getElementById("detail-" + id);
    if (!detail) return;
    const open = detail.hidden;
    detail.hidden = !open;
    el.setAttribute("aria-expanded", String(open));
    el.classList.toggle("is-open", open);
    if (!open || detail.dataset.loaded) return;
    const box = detail.querySelector(".detail-body");
    const d = await api("/activity/inspection/" + encodeURIComponent(id));
    detail.dataset.loaded = "1";
    renderDetail(box, d);
  };

  // Keyboard: Enter/Space on a focused row opens it, like a button.
  document.addEventListener("keydown", (e) => {
    const row = e.target.closest?.("tr[data-action='toggle-detail']");
    if (!row || (e.key !== "Enter" && e.key !== " ")) return;
    e.preventDefault();
    Actions["toggle-detail"](row);
  });
})();
