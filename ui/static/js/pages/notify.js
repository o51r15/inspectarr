/* notify.js -- Settings -> Notifications: the places to send messages to.
 *
 * DockLlama's design: one card per place (what it is, when it last sent or why
 * it failed, Send test, Remove, Pause, and its own event checkboxes), and an
 * add form that builds the address from one or two plain fields. Everything
 * saves through /config/notify/* as it happens -- none of it is part of the
 * Settings form, so it never marks the page as unsaved.
 *
 * Built with DOM calls, not HTML strings: descriptions and errors contain
 * things people typed, and that way none of it can become markup.
 */
"use strict";

(function () {
  const S = { data: null, form: null };
  const $ = (id) => document.getElementById(id);

  function el(tag, attrs, ...kids) {
    const n = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (v == null || v === false) return;
      if (k === "text") n.textContent = v;
      else if (k === "dataset") Object.assign(n.dataset, v);
      else n.setAttribute(k, v === true ? "" : v);
    });
    kids.flat().forEach((c) => c != null && n.append(c));
    return n;
  }

  function api(url, body) {
    const opts = body === undefined ? {} :
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
    return fetch(url, opts).then((r) => r.json().catch(() => ({ ok: false, message: "Unexpected answer (" + r.status + ")" })));
  }

  function showError(text) {
    const box = $("notify-error");
    if (!box) return;
    box.textContent = text || "";
    box.hidden = !text;
  }

  function when(iso) {
    if (!iso) return "";
    const d = new Date(/Z|[+-]\d\d:?\d\d$/.test(iso) ? iso : iso + "Z");
    return isNaN(d) ? iso : d.toLocaleString();
  }

  function toast(msg, level) {
    if (typeof window.showToast === "function") window.showToast(msg, level || "success");
  }

  // --- loading ------------------------------------------------------------------
  function load() {
    return api("/config/notify").then((d) => {
      if (!d.ok) { showError(d.message || "Could not load the notification settings."); return; }
      S.data = d;
      showError("");
      renderTargets();
      if (!d.targets.length && !S.form) startAdd("ntfy");
    }).catch(() => showError("Could not reach Inspectarr to load the notification settings."));
  }

  // --- the cards ------------------------------------------------------------------
  function statusLine(t) {
    if (!t.usable) return el("div", { class: "hint text-danger", text: "These details are incomplete. Remove this and add it again." });
    if (t.last_error) return el("div", { class: "hint text-danger", text: "Last try failed: " + t.last_error });
    return el("div", { class: "hint", text: t.last_ok ? "Last message sent " + when(t.last_ok) : "Nothing sent yet" });
  }

  function eventBoxes(checked, onChange, prefix) {
    return el("div", { class: "event-grid" }, S.data.events.map((ev) => {
      const id = prefix + "-" + ev.key;
      const box = el("input", { type: "checkbox", id, checked: !!checked[ev.key] });
      box.addEventListener("change", () => onChange(ev.key, box.checked, box));
      return el("label", { class: "check-item", for: id }, box, el("span", { text: ev.label }));
    }));
  }

  function card(t) {
    const pause = el("input", { type: "checkbox", id: "notify-pause-" + t.id, checked: !t.enabled });
    pause.addEventListener("change", () => setEnabled(t, !pause.checked, pause));
    return el("div", { class: "notify-card" + (t.enabled ? "" : " notify-paused"), dataset: { target: t.id } },
      el("div", { class: "row-between" },
        el("div", {},
          el("strong", { text: t.description }),
          t.enabled ? null : el("span", { class: "badge badge-neutral ml-8", text: "Paused" }),
          statusLine(t)),
        el("div", { class: "flex gap-8" },
          el("button", { type: "button", class: "btn btn-ghost btn-sm", "data-action": "notify-test-saved",
                         "data-id": t.id, text: "Send test" }),
          el("button", { type: "button", class: "btn btn-ghost btn-sm", "data-action": "notify-remove",
                         "data-id": t.id, text: "Remove" }))),
      t.subscribe ? el("div", { class: "hint" }, "Subscribe on another phone: ",
        el("a", { href: t.subscribe, target: "_blank", rel: "noopener", text: t.subscribe })) : null,
      eventBoxes(t.events, (key, on, box) => toggleEvent(t, key, on, box), "notify-ev-" + t.id),
      el("label", { class: "check-item mt-8", for: "notify-pause-" + t.id }, pause,
        el("span", { text: "Pause messages to this one" })));
  }

  function renderTargets() {
    const box = $("notify-targets");
    if (!box || !S.data) return;
    box.replaceChildren(...(S.data.targets.length ? S.data.targets.map(card) :
      [el("p", { class: "hint m-0", text: "Nowhere yet. Add a place below and Inspectarr will message you there." })]));
    $("notify-add-btn").hidden = !S.data.targets.length || !!S.form;
  }

  function target(id) { return S.data.targets.find((t) => t.id === Number(id)); }

  function toggleEvent(t, key, on, box) {
    api("/config/notify/targets/" + t.id, { events: { [key]: on } }).then((r) => {
      if (!r.ok) { box.checked = !on; showError(r.message); return; }
      t.events[key] = on;
    });
  }

  function setEnabled(t, on, box) {
    api("/config/notify/targets/" + t.id, { enabled: on }).then((r) => {
      if (!r.ok) { box.checked = on; showError(r.message); return; }
      load();
    });
  }

  function testSaved(id) {
    const t = target(id);
    if (!t) return;
    api("/config/notify/targets/" + id + "/test", {}).then((r) => {
      toast(r.ok ? "Test sent to " + t.description + ". Check that it arrived." : (r.problem || r.message), r.ok ? "success" : "error");
      load();
    });
  }

  function remove(id) {
    const t = target(id);
    if (!t) return;
    confirmDialog({ title: "Remove this place?", body: "Stop sending messages to " + t.description + "?",
                    confirm: "Remove" }).then((yes) => {
      if (!yes) return;
      api("/config/notify/targets/" + id + "/delete", {}).then((r) => {
        if (!r.ok) showError(r.message);
        load();
      });
    });
  }

  // --- the add form -------------------------------------------------------------
  function service(id) { return S.data.services.find((s) => s.id === id); }

  function startAdd(serviceId) {
    const prev = S.form;
    S.form = { service: serviceId, fields: {}, events: {} };
    S.data.default_events.forEach((k) => { S.form.events[k] = true; });
    if (prev) S.form.events = prev.events;            // keep the ticks when switching service
    $("notify-test-result").textContent = "";
    const ready = serviceId === "ntfy"
      ? api("/config/notify/ntfy-topic").then((r) => { S.form.fields = { topic: r.topic, server: "https://ntfy.sh" }; })
      : Promise.resolve();
    return ready.then(renderForm);
  }

  function renderForm() {
    const f = S.form;
    const box = $("notify-form");
    box.hidden = !f;
    $("notify-add-btn").hidden = !!f || !S.data.targets.length;
    if (!f) return;
    $("notify-cancel").hidden = !S.data.targets.length;
    const sel = $("notify-service");
    sel.replaceChildren(...S.data.services.map((s) =>
      el("option", { value: s.id, selected: s.id === f.service, text: s.label })));
    const svc = service(f.service);
    $("notify-blurb").textContent = svc.blurb;
    $("notify-fields").replaceChildren(...svc.fields.map((field) => {
      const id = "notify-field-" + field.key;
      const input = el("input", { type: field.type || "text", id, autocomplete: "off",
                                  placeholder: field.placeholder || "", value: f.fields[field.key] || "" });
      input.addEventListener("input", () => { f.fields[field.key] = input.value; updateNtfy(); });
      return el("div", { class: "form-group mw-420" },
        el("label", { for: id, text: field.label }), input,
        field.hint ? el("span", { class: "hint", text: field.hint }) : null);
    }));
    $("notify-form-events").replaceChildren(eventBoxes(f.events, (key, on) => { f.events[key] = on; }, "notify-new"));
    updateNtfy();
  }

  function subscribeLink() {
    const f = S.form;
    if (!f || f.service !== "ntfy" || !f.fields.topic) return "";
    let s = (f.fields.server || "https://ntfy.sh").trim().replace(/\/+$/, "");
    if (!/^https?:\/\//.test(s)) s = "https://" + s;
    return s + "/" + f.fields.topic;
  }

  let qrTimer = null;
  function updateNtfy() {
    const link = subscribeLink();
    $("notify-ntfy").hidden = !link;
    if (!link) return;
    const a = $("notify-sub-link");
    a.href = link;
    a.textContent = link;
    clearTimeout(qrTimer);                     // don't fetch a QR code per keystroke
    qrTimer = setTimeout(() => { $("notify-qr").src = "/config/notify/qr?text=" + encodeURIComponent(link); }, 300);
  }

  function chosenEvents() {
    return Object.keys(S.form.events).filter((k) => S.form.events[k]);
  }

  function testNew() {
    const out = $("notify-test-result");
    out.textContent = "Sending…";
    out.className = "hint m-0 mt-8";
    api("/config/notify/test", { service: S.form.service, fields: S.form.fields }).then((r) => {
      out.textContent = r.ok ? "Test sent. If it didn't arrive, check the details." : (r.problem || r.message);
      out.className = "hint m-0 mt-8 " + (r.ok ? "text-ok" : "text-danger");
    });
  }

  function save() {
    if (!chosenEvents().length) {
      $("notify-test-result").textContent = "Tick at least one thing to send.";
      return;
    }
    api("/config/notify/targets", { service: S.form.service, fields: S.form.fields, events: chosenEvents() })
      .then((r) => {
        if (!r.ok) {
          const out = $("notify-test-result");
          out.textContent = r.message;
          out.className = "hint m-0 mt-8 text-danger";
          return;
        }
        S.form = null;
        renderForm();
        toast("Saved. Inspectarr will message you there.");
        load();
      });
  }

  // --- wiring -------------------------------------------------------------------
  Actions["notify-add"] = () => startAdd("ntfy");
  Actions["notify-cancel"] = () => { S.form = null; renderForm(); };
  Actions["notify-test-new"] = () => testNew();
  Actions["notify-save"] = () => save();
  Actions["notify-test-saved"] = (btn) => testSaved(btn.dataset.id);
  Actions["notify-remove"] = (btn) => remove(btn.dataset.id);

  document.addEventListener("DOMContentLoaded", () => {
    const sec = $("notify-section");
    if (!sec) return;
    $("notify-service").addEventListener("change", (e) => startAdd(e.target.value));
    // Not part of the Settings form: stop these events at the section, after
    // the page's own handlers have run and before the form's unsaved-changes
    // listener sees them.
    ["input", "change"].forEach((evt) => sec.addEventListener(evt, (e) => e.stopPropagation()));
    load();
  });
})();
