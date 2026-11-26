/* settings-bridge.js — connects Settings markup to settings.js without
 * inline handlers (which the Content-Security-Policy forbids).
 *
 *   data-action="call" data-fn="name" data-args='[...]'   -> name(...args)
 *   data-pass-el                                           -> also passes the element
 *   data-on-change="a b" / data-on-input="a"               -> calls a(), then b()
 *
 * Only names in CALLABLE can be called, so markup cannot reach arbitrary
 * globals. Loaded before settings.js; names resolve at call time.
 */
"use strict";

(function () {
  const CALLABLE = new Set([
    "setEditMode", "toggleAdvanced", "discardFileEdits", "copyText", "generateSecret",
    "openConfigFile", "sumWeights", "toggleTorrentPanes", "testConnection", "addRule", "removeRule",
    "addExt", "removeExt", "addPattern", "removePattern", "describeBands",
    "testProwlarr", "resetScoringDefaults", "syncAiEnabled", "testOllama", "pullModel",
    "loadOllamaModels", "resetSystemPrompt", "saveSystemPrompt", "onBatchInput",
    "onBatchCommit", "testApprise", "createBackup", "deleteBackup",
  ]);

  function call(name, args) {
    if (!CALLABLE.has(name) || typeof window[name] !== "function") {
      console.warn("settings: not callable", name);
      return;
    }
    window[name](...args);
  }

  Actions["call"] = (el) => {
    let args = [];
    try { args = JSON.parse(el.dataset.args || "[]"); } catch (_) { console.warn("bad data-args", el); return; }
    if ("passEl" in el.dataset) args.push(el);
    call(el.dataset.fn, args);
  };

  for (const evt of ["change", "input"]) {
    document.addEventListener(evt, (e) => {
      const el = e.target.closest && e.target.closest(`[data-on-${evt}]`);
      if (!el) return;
      el.getAttribute(`data-on-${evt}`).split(/\s+/).filter(Boolean).forEach((n) => call(n, []));
    });
  }

  Actions["delete-backup"] = (el) => call("deleteBackup", [el.dataset.name]);
  Actions["show-ollama-update"] = () => { const i = document.getElementById("ollama-update-info"); if (i) i.style.display = "block"; };
  Actions["hide-ollama-update"] = () => { const i = document.getElementById("ollama-update-info"); if (i) i.style.display = "none"; };

  /* Elements that started hidden carry .d-none in the markup. Turn that into
     an inline display:none (set through CSSOM, which the CSP allows) so the
     existing code that shows them with el.style.display = '' or 'block'
     works exactly as it did when the attribute was written inline. */
  document.querySelectorAll(".d-none").forEach((el) => {
    el.classList.remove("d-none");
    el.style.display = "none";
  });
})();
