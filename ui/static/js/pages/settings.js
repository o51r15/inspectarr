/* settings.js — the Settings page (config.html).
 * Moved out of an inline <script> for the Content-Security-Policy.
 * Function names are unchanged on purpose (Phase 1); markup calls them
 * through data-action="call" via the whitelist in settings-bridge.js.
 * Phase 3 reshapes this file by pane. */
  // ---- Edit mode (form vs raw yaml) ----
  function setEditMode(mode) {
    document.getElementById('edit_mode').value = mode;
    document.getElementById('form-view').style.display = (mode === 'form') ? 'block' : 'none';
    document.getElementById('yaml-view').style.display = (mode === 'yaml') ? 'block' : 'none';
    document.getElementById('mode-form-btn').classList.toggle('active', mode === 'form');
    document.getElementById('mode-yaml-btn').classList.toggle('active', mode === 'yaml');
  }
  setEditMode('form');

  // The AI master switch. Everything on this pane, plus the Ollama-backed
  // notification options, is meaningless without it -- disable rather than
  // hide, so the capability stays discoverable and it is obvious what
  // unlocks it.
  const AI_FIELDS = ['ollama_url', 'ollama_timeout', 'ollama_cache_ttl_hours',
                     'ollama_update_check_hours', 'ollama-model-select',
                     'ollama-apply-btn', 'ollama-validate-btn',
                     'ollama-calibrate-btn', 'ollama_context_window',
                     'ollama_max_indexers_per_call',
                     'ollama-system-prompt'];

  function aiEnabled() {
    const t = document.getElementById('ollama_enabled');
    return !t || t.checked;
  }

  function syncAiEnabled() {
    const on = aiEnabled();
    AI_FIELDS.forEach(function (id) {
      const el = document.getElementById(id);
      if (!el) return;
      el.disabled = !on;
      const grp = el.closest('.form-group') || el.parentElement;
      if (grp) grp.style.opacity = on ? '' : '0.5';
    });
    document.querySelectorAll('[data-pane="ai"] .form-section').forEach(
      function (sec, i) {
        // Leave the switch's own section fully lit.
        if (i === 0) return;
        sec.style.opacity = on ? '' : '0.55';
      });
    // Buttons driven by onclick rather than an id lookup.
    document.querySelectorAll('[data-pane="ai"] button').forEach(function (b) {
      if (b.id === 'ollama_enabled') return;
      b.disabled = !on;
    });
    syncAiDependentControls();
  }

  // ---- AI pane -----------------------------------------------------------
  function aiSettingsPayload() {
    return {
      url:                document.getElementById('ollama_url').value.trim(),
      timeout:            document.getElementById('ollama_timeout').value,
      cache_ttl_hours:    document.getElementById('ollama_cache_ttl_hours').value,
      update_check_hours: document.getElementById('ollama_update_check_hours').value,
    };
  }

  function testOllama() {
    const el = document.getElementById('ollama-test-status');
    const url = document.getElementById('ollama_url').value.trim();
    if (!url) { el.textContent = 'Enter a URL first'; return; }
    el.textContent = 'Testing…';
    fetch('/config/ai/test-connection', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url: url}),
    })
      .then(r => r.json())
      .then(d => { el.textContent = (d.ok ? '✓ ' : '✗ ') + d.message; })
      .catch(e => { el.textContent = '✗ ' + e; });
  }

  // Ollama toggles on the Notifications pane depend on a configured URL.
  // Disable rather than hide them, so it is clear the capability exists and
  // what unlocks it.
  function syncAiDependentControls() {
    const urlEl = document.getElementById('ollama_url');
    if (!urlEl) return;
    const master = document.getElementById('ollama_enabled');
    const configured = urlEl.value.trim().length > 0
                       && (!master || master.checked);
    ['digest_use_ollama', 'summary_use_ollama'].forEach(function (id) {
      const cb = document.getElementById(id);
      if (!cb) return;
      cb.disabled = !configured;
      const row = cb.closest('.toggle-row') || cb.parentElement;
      if (row) row.style.opacity = configured ? '' : '0.5';
      const lbl = row ? row.querySelector('label[for="' + id + '"]') : null;
      if (lbl) {
        lbl.title = configured ? '' : 'Requires an Ollama URL — set one in Settings → AI';
      }
      let hint = document.getElementById(id + '-ai-hint');
      if (!configured && !hint && row) {
        hint = document.createElement('span');
        hint.id = id + '-ai-hint';
        hint.className = 'hint';
        hint.style.marginLeft = '8px';
        hint.innerHTML = 'Requires <a href="/config#ai">Ollama</a>';
        row.appendChild(hint);
      } else if (configured && hint) {
        hint.remove();
      }
    });
  }

  // ---- Pane switching — driven by the sidebar Settings group + URL hash ----
  const paneNames = {
    connections: 'Connections', rules: 'Rules', indexers: 'Indexers',
    notifications: 'Notifications', ai: 'AI', general: 'General', advanced: 'Advanced',
    backups: 'Backups',
  };

  function showPane(pane) {
    if (!paneNames[pane]) pane = 'connections';
    document.querySelectorAll('.settings-pane').forEach(p =>
      p.classList.toggle('active', p.dataset.pane === pane));
    document.getElementById('active-pane-name').textContent = paneNames[pane];
    // Reflect the active sub-item in the sidebar Settings group
    document.querySelectorAll('#settings-group .nav-group-items a').forEach(a =>
      a.classList.toggle('active', a.dataset.pane === pane));
    // Ensure form mode is visible if a YAML view was open
    if (document.getElementById('edit_mode').value !== 'form') setEditMode('form');
  }

  // Initial pane from the URL hash (e.g. /config#rules), default connections
  // NOTE: deferred hooks (loadBackups, loadOllamaModels) are added later in the
  // script via the showPane wrapper. Store initial pane and re-fire after wrapper.
  var _initialPane = (window.location.hash || '').replace('#', '') || 'connections';
  showPane(_initialPane);

  syncAiEnabled();
  resumeValidationIfRunning();
  describeBands();
  loadValidations();
  (function () {
    const u = document.getElementById('ollama_url');
    if (u) u.addEventListener('input', syncAiDependentControls);
  })();

  // Sidebar sub-item clicks: switch pane without a full reload (same page)
  document.querySelectorAll('#settings-group .nav-group-items a').forEach(a => {
    a.addEventListener('click', (e) => {
      // Already on /config — intercept and switch pane in place
      e.preventDefault();
      const pane = a.dataset.pane;
      history.replaceState(null, '', '/config#' + pane);
      showPane(pane);
    });
  });

  // Respond to back/forward hash changes
  window.addEventListener('hashchange', () => {
    showPane((window.location.hash || '').replace('#', '') || 'connections');
  });

  // ---- Dirty-state tracking ----
  // Delay arming so JS-driven population (qBit categories, model selects,
  // default values) doesn't false-trigger the "unsaved changes" warning.
  const savebar = document.getElementById('savebar');
  let armed = false;
  function markDirty() { if (armed) savebar.classList.add('dirty'); }
  document.getElementById('config-form').addEventListener('input', markDirty);
  document.getElementById('config-form').addEventListener('change', markDirty);
  // Clear dirty flag on submit so the warning doesn't fire during navigation
  document.getElementById('config-form').addEventListener('submit', () => {
    window.removeEventListener('beforeunload', beforeUnloadHandler);
  });
  function beforeUnloadHandler(e) {
    if (savebar.classList.contains('dirty')) { e.preventDefault(); e.returnValue = ''; }
  }
  window.addEventListener('beforeunload', beforeUnloadHandler);
  // Arm after a tick — all dynamic selects / defaults are populated by then
  setTimeout(() => { armed = true; }, 500);

  // ---- qBit category population + rules gating ----
  let qbitCategories = [];
  let qbitConnected  = false;

  function buildCategoryOptions(selectEl) {
    const current = selectEl.dataset.current || '';
    selectEl.innerHTML = '';
    // Always include the currently-saved value first so it's never lost
    const values = [...qbitCategories];
    if (current && !values.includes(current)) values.unshift(current);
    if (!current && !values.includes('')) {
      const opt = document.createElement('option');
      opt.value = ''; opt.textContent = '(select category)';
      selectEl.appendChild(opt);
    }
    values.forEach(v => {
      const opt = document.createElement('option');
      opt.value = v; opt.textContent = v;
      if (v === current) opt.selected = true;
      selectEl.appendChild(opt);
    });
  }

  function applyRulesGating() {
    const banner = document.getElementById('qbit-conn-banner');
    const dot    = document.getElementById('qbit-conn-dot');
    const text   = document.getElementById('qbit-conn-text');
    const addBtn = document.getElementById('add-rule-btn');
    const container = document.getElementById('rules-container');

    if (qbitConnected) {
      banner.classList.add('connected'); banner.classList.remove('disconnected');
      dot.className = 'status-dot running';
      text.textContent = `qBittorrent connected · ${qbitCategories.length} categories`;
      addBtn.disabled = false;
      container.classList.remove('rules-disabled');
      document.querySelectorAll('.rule-category').forEach(buildCategoryOptions);
    } else {
      banner.classList.add('disconnected'); banner.classList.remove('connected');
      dot.className = 'status-dot stopped';
      text.innerHTML = 'qBittorrent not reachable — rules are read-only. Check the Connections tab.';
      addBtn.disabled = true;
      container.classList.add('rules-disabled');
    }
  }

  function loadCategories() {
    fetch('/config/qbit/categories')
      .then(r => r.json())
      .then(d => {
        qbitConnected  = d.ok && Array.isArray(d.categories);
        qbitCategories = d.categories || [];
        applyRulesGating();
      })
      .catch(() => { qbitConnected = false; qbitCategories = []; applyRulesGating(); });
  }
  loadCategories();

  // ---- Rule builder ----
  let ruleIndex = JSON.parse(document.getElementById('settings-data').textContent).rule_count;

  function addRule() {
    if (!qbitConnected) return;
    const idx = ruleIndex++;
    const catOpts = qbitCategories.map(c => `<option value="${c}">${c}</option>`).join('');
    const card = document.createElement('div');
    card.className = 'rule-card';
    card.id = `rule-${idx}`;
    card.innerHTML = `
      <div class="rule-header">
        <strong class="text">New Rule</strong>
        <button type="button" class="rule-remove" data-action="call" data-fn="removeRule" data-args="[${idx}]" title="Remove">×</button>
      </div>
      <div class="form-row">
        <div class="form-group">
          <label>Rule Name</label>
          <input type="text" name="rule_name_${idx}" value="">
        </div>
        <div class="form-group">
          <label>qBit Category</label>
          <select name="rule_category_${idx}" class="rule-category" data-current="">
            <option value="">(select category)</option>
            ${catOpts}
          </select>
        </div>
      </div>
      <div class="form-row">
        <div class="form-group">
          <label>App</label>
          <select name="rule_app_${idx}">
            <option value="sonarr">Sonarr</option>
            <option value="radarr">Radarr</option>
            <option value="lidarr">Lidarr</option>
          </select>
        </div>
        <div class="form-group">
          <label>Match Mode</label>
          <select name="rule_match_mode_${idx}">
            <option value="any">Any bad file</option>
            <option value="primary">Primary file only</option>
          </select>
        </div>
      </div>
      <div class="form-group">
        <label>Bad Extensions</label>
        <input type="hidden" name="rule_extensions_${idx}" id="rule_ext_hidden_${idx}" value="">
        <div class="input-with-btn">
          <input type="text" id="rule_ext_input_${idx}" placeholder=".exe" class="mw-160">
          <button type="button" class="btn btn-ghost btn-sm" data-action="call" data-fn="addExt" data-args="[${idx}]">+ Add</button>
        </div>
        <div class="ext-tags" id="rule_ext_tags_${idx}"></div>
      </div>
      <div class="form-row">
        <div class="form-group mw-220">
          <label>Min Primary File Size (MB)</label>
          <input type="number" name="rule_min_size_mb_${idx}" value="" placeholder="Leave blank to disable" min="1">
          <div class="hint">Flag if primary file is smaller than this</div>
        </div>
      </div>
      <div class="form-group">
        <label>Bad Filename Patterns (regex)</label>
        <input type="hidden" name="rule_patterns_${idx}" id="rule_pat_hidden_${idx}" value="">
        <div class="input-with-btn">
          <input type="text" id="rule_pat_input_${idx}" placeholder="e.g. sample\\." class="mw-200">
          <button type="button" class="btn btn-ghost btn-sm" data-action="call" data-fn="addPattern" data-args="[${idx}]">+ Add</button>
        </div>
        <div class="ext-tags" id="rule_pat_tags_${idx}"></div>
      </div>`;
    document.getElementById('rules-container').appendChild(card);
    markDirty();
  }

  function removeRule(idx) {
    const card = document.getElementById(`rule-${idx}`);
    if (card) { card.remove(); markDirty(); }
  }

  function addExt(idx) {
    const input  = document.getElementById(`rule_ext_input_${idx}`);
    const hidden = document.getElementById(`rule_ext_hidden_${idx}`);
    const tags   = document.getElementById(`rule_ext_tags_${idx}`);
    let val = input.value.trim();
    if (!val) return;
    if (!val.startsWith('.')) val = '.' + val;
    const cur = hidden.value ? hidden.value.split(',') : [];
    if (cur.includes(val)) { input.value = ''; return; }
    cur.push(val);
    hidden.value = cur.join(',');
    const span = document.createElement('span');
    span.className = 'ext-tag';
    span.innerHTML = `${esc(val)} <button type="button">×</button>`;
    span.querySelector('button').onclick = () => removeExt(idx, val);
    tags.appendChild(span);
    input.value = '';
    markDirty();
  }

  function removeExt(idx, val) {
    const hidden = document.getElementById(`rule_ext_hidden_${idx}`);
    const tags   = document.getElementById(`rule_ext_tags_${idx}`);
    hidden.value = hidden.value.split(',').filter(e => e && e !== val).join(',');
    [...tags.children].forEach(c => { if (c.textContent.trim().startsWith(val)) c.remove(); });
    markDirty();
  }

  function addPattern(idx) {
    const input  = document.getElementById(`rule_pat_input_${idx}`);
    const hidden = document.getElementById(`rule_pat_hidden_${idx}`);
    const tags   = document.getElementById(`rule_pat_tags_${idx}`);
    const val = input.value.trim();
    if (!val) return;
    const cur = hidden.value ? hidden.value.split(',') : [];
    if (cur.includes(val)) { input.value = ''; return; }
    cur.push(val);
    hidden.value = cur.join(',');
    const span = document.createElement('span');
    span.className = 'ext-tag';
    span.setAttribute('data-pattern', val);
    span.innerHTML = `${esc(val)} <button type="button">×</button>`;
    span.querySelector('button').onclick = () => removePattern(idx, span.querySelector('button'));
    tags.appendChild(span);
    input.value = '';
    markDirty();
  }

  function removePattern(idx, btn) {
    const span   = btn.closest('.ext-tag');
    const val    = span.getAttribute('data-pattern');
    const hidden = document.getElementById(`rule_pat_hidden_${idx}`);
    hidden.value = hidden.value.split(',').filter(p => p && p !== val).join(',');
    span.remove();
    markDirty();
  }

  // ---- Torrent client pane toggle ----
  function toggleTorrentPanes() {
    const active = document.getElementById('torrent_client').value;
    document.querySelectorAll('.torrent-pane').forEach(p => {
      p.style.display = p.id === 'pane-' + active ? '' : 'none';
    });
    markDirty();
  }
  toggleTorrentPanes();

  // ---- Test connections ----
  function testConnection(which) {
    const map = {
      qbit:         {url:'qbit_url', extra:{username:'qbit_username', password:'qbit_password'}, endpoint:'/config/test/qbit'},
      transmission: {url:'transmission_url', extra:{username:'transmission_username', password:'transmission_password'}, endpoint:'/config/test/transmission'},
      deluge:       {url:'deluge_url', extra:{password:'deluge_password'}, endpoint:'/config/test/deluge'},
      sonarr: {url:'sonarr_url', key:'sonarr_api_key', endpoint:'/config/test/sonarr'},
      radarr: {url:'radarr_url', key:'radarr_api_key', endpoint:'/config/test/radarr'},
      lidarr: {url:'lidarr_url', key:'lidarr_api_key', endpoint:'/config/test/lidarr'},
    };
    const cfg = map[which];
    const el  = document.getElementById(`test-${which}-result`);
    el.textContent = 'Testing…'; el.style.color = '';
    const body = { url: document.querySelector(`[name="${cfg.url}"]`).value };
    if (cfg.key)   body.api_key = document.querySelector(`[name="${cfg.key}"]`).value;
    if (cfg.extra) {
      if (cfg.extra.username) body.username = document.querySelector(`[name="${cfg.extra.username}"]`).value;
      if (cfg.extra.password) body.password = document.querySelector(`[name="${cfg.extra.password}"]`).value;
    }
    fetch(cfg.endpoint, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
    })
    .then(r => r.json())
    .then(d => { el.textContent = d.message; el.style.color = d.ok ? 'var(--success)' : 'var(--error)'; })
    .catch(() => { el.textContent = 'Request failed'; el.style.color = 'var(--error)'; });
  }

  function testProwlarr() {
    const url     = document.getElementById('prowlarr_url').value;
    const api_key = document.getElementById('prowlarr_api_key').value;
    const el      = document.getElementById('prowlarr-status');
    el.textContent = 'Testing…'; el.style.color = '';
    fetch('/config/test/prowlarr', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url, api_key}),
    })
    .then(r => r.json())
    .then(d => { el.textContent = d.message; el.style.color = d.ok ? 'var(--success)' : 'var(--error)'; });
  }

  function testApprise() {
    const urlsRaw = document.querySelector('[name="apprise_urls"]').value;
    const urls    = urlsRaw.split('\n').map(u => u.trim()).filter(Boolean);
    const el      = document.getElementById('test-apprise-result');
    el.textContent = 'Sending…'; el.style.color = '';
    fetch('/config/test/apprise', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({urls}),
    })
    .then(r => r.json())
    .then(d => { el.textContent = d.message; el.style.color = d.ok ? 'var(--success)' : 'var(--error)'; })
    .catch(() => { el.textContent = 'Request failed'; el.style.color = 'var(--error)'; });
  }

  // ---- Scoring weights reset ----
  function resetScoringDefaults() {
    document.querySelectorAll('[name^="scoring_"]').forEach(el => {
      if (el.dataset.default) el.value = el.dataset.default;
    });
    // Mark form dirty
    const savebar = document.getElementById('savebar');
    if (savebar) savebar.classList.add('dirty');
  }

  // ---- System prompt management ----
  function loadSystemPrompt() {
    fetch('/config/ollama/system-prompt')
      .then(r => r.json())
      .then(d => {
        if (d.ok) {
          document.getElementById('ollama-system-prompt').value = d.prompt;
        }
      });
  }

  function saveSystemPrompt() {
    const prompt = document.getElementById('ollama-system-prompt').value;
    const el = document.getElementById('system-prompt-status');
    el.textContent = 'Saving…'; el.style.color = '';
    fetch('/config/ollama/system-prompt', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({prompt}),
    })
    .then(r => r.json())
    .then(d => {
      el.textContent = d.message;
      el.style.color = d.ok ? 'var(--success)' : 'var(--error)';
    })
    .catch(() => { el.textContent = 'Request failed'; el.style.color = 'var(--error)'; });
  }

  function resetSystemPrompt() {
    fetch('/config/ollama/system-prompt')
      .then(r => r.json())
      .then(d => {
        if (d.ok) {
          document.getElementById('ollama-system-prompt').value = d.default_prompt;
          // Save the reset (empty string = use default)
          fetch('/config/ollama/system-prompt', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({prompt: ''}),
          })
          .then(r => r.json())
          .then(d2 => {
            const el = document.getElementById('system-prompt-status');
            el.textContent = d2.message || 'Reset to default';
            el.style.color = 'var(--success)';
          });
        }
      });
  }

  // ---- Backup management ----
  function loadBackups() {
    fetch('/config/backups')
      .then(r => r.json())
      .then(d => {
        const tbody = document.getElementById('backups-body');
        if (!d.ok || !d.backups.length) {
          tbody.innerHTML = '<tr><td colspan="4" class="hint text-center">No backups yet</td></tr>';
          return;
        }
        tbody.innerHTML = d.backups.map(b => `<tr>
          <td class="mono text-xs">${esc(b.name)}</td>
          <td>${esc(b.created)}</td>
          <td>${esc(b.size)}</td>
          <td class="text-right nowrap">
            <a href="/config/backups/download/${encodeURIComponent(b.name)}" class="btn btn-ghost btn-sm">Download</a>
            <button type="button" class="btn btn-ghost btn-sm text-danger" data-action="delete-backup" data-name="${esc(b.name)}">Delete</button>
          </td>
        </tr>`).join('');
      });
  }

  function createBackup() {
    const el = document.getElementById('backup-status');
    el.textContent = 'Creating backup…'; el.style.color = '';
    fetch('/config/backups/create', {method:'POST'})
      .then(r => r.json())
      .then(d => {
        el.textContent = d.message;
        el.style.color = d.ok ? 'var(--success)' : 'var(--error)';
        if (d.ok) loadBackups();
      })
      .catch(() => { el.textContent = 'Request failed'; el.style.color = 'var(--error)'; });
  }

  function deleteBackup(name) {
    if (!confirm('Delete backup ' + name + '?')) return;
    fetch('/config/backups/delete', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name}),
    })
    .then(r => r.json())
    .then(d => {
      const el = document.getElementById('backup-status');
      el.textContent = d.message;
      el.style.color = d.ok ? 'var(--success)' : 'var(--error)';
      if (d.ok) loadBackups();
    });
  }

  // Load backups when pane is first shown
  const origShowPane = showPane;
  showPane = function(pane) {
    origShowPane(pane);
    if (pane === 'backups') loadBackups();
    if (pane === 'ai') {
      loadOllamaModels(); loadSystemPrompt(); loadBatchCalibration();
    }
  };

  // ---- Ollama model selection ----
  function loadOllamaModels() {
    const sel = document.getElementById('ollama-model-select');
    const btn = document.getElementById('ollama-apply-btn');
    const st  = document.getElementById('ollama-model-status');
    sel.disabled = true; btn.disabled = true;
    sel.innerHTML = '<option value="">Loading…</option>';
    st.textContent = ''; st.style.color = '';
    fetch('/config/ollama/models')
      .then(r => r.json())
      .then(d => {
        if (!d.ok) {
          sel.innerHTML = '<option value="">' + (d.message || 'Unavailable') + '</option>';
          return;
        }
        if (!d.models.length) {
          sel.innerHTML = '<option value="">No models found</option>';
          return;
        }
        sel.innerHTML = d.models.map(m =>
          `<option value="${esc(m)}"${m === d.current ? ' selected' : ''}>${esc(m)}</option>`
        ).join('');
        sel.disabled = false; btn.disabled = false;
        decorateModelOptions();
        renderValidationDetail();
      })
      .catch(() => {
        sel.innerHTML = '<option value="">Failed to fetch models</option>';
      });
  }

  function describeBands() {
    const el = document.getElementById('band-explainer');
    if (!el) return;
    const order = ['LOW', 'MEDIUM', 'HIGH', 'CRITICAL'];
    const minEl = document.getElementById('remediation_min_severity');
    const remEl = document.getElementById('remediation_remediate_at');
    if (!minEl || !remEl) return;
    let mi = order.indexOf(minEl.value), ri = order.indexOf(remEl.value);
    // A delete floor below the act floor is contradictory; the backend clamps
    // it, so describe what will actually happen rather than what was picked.
    const clamped = ri < mi;
    if (clamped) ri = mi;

    // The operating mode is a ceiling applied AFTER these bands. Describing
    // the bands without it would say "Deleted: CRITICAL" on a box that is
    // configured never to delete anything, which is the most misleading
    // thing this page could tell you.
    const modeEl = document.getElementById('remediation_operating_mode');
    const mode = modeEl ? modeEl.value : 'automatic';

    let recorded = order.slice(0, mi);
    let held = order.slice(mi, ri);
    let deleted = order.slice(ri);
    let capNote = '';

    if (mode === 'monitor') {
      recorded = order.slice();
      held = [];
      deleted = [];
      capNote = '<br><em>Operating mode is <strong>Monitor</strong>: nothing ' +
                'is paused or deleted, whatever the thresholds say. The ' +
                'thresholds are kept and take effect again when you switch ' +
                'back.</em>';
    } else if (mode === 'quarantine') {
      held = held.concat(deleted);
      deleted = [];
      capNote = '<br><em>Operating mode is <strong>Quarantine</strong>: ' +
                'nothing is deleted automatically. Anything at or above ' +
                (clamped ? order[mi] : remEl.value) +
                ' is held for review instead.</em>';
    }

    const fmt = a => a.length ? a.join(', ') : 'nothing';
    el.innerHTML =
      '<strong>Recorded only:</strong> ' + fmt(recorded) +
      ' &nbsp;|&nbsp; <strong>Quarantined:</strong> ' + fmt(held) +
      ' &nbsp;|&nbsp; <strong>Deleted:</strong> ' + fmt(deleted) +
      (clamped ? '<br><em>Delete threshold is below the act threshold - ' +
                 'treating both as ' + order[mi] + '.</em>' : '') +
      (mode === 'automatic' && !order.slice(mi, ri).length ?
        '<br><em>No quarantine band - every catch that is acted on is ' +
        'deleted immediately.</em>' : '') +
      capNote;
  }

  // ---- Model validation ---------------------------------------------------
  let _validations = {};      // model -> record
  let _validationPoll = null;

  // One place decides what a model's state IS, so the dropdown, the detail
  // box and the table can never disagree about it.
  //
  // Colour is the fast channel, but it is never the ONLY channel: roughly one
  // man in twelve cannot separate the red from the green, and a native
  // <select> on macOS and on most mobile pickers ignores option colour
  // entirely. So every state also carries a glyph and a word, and the colour
  // is the enhancement rather than the message.
  //
  // Two states beyond the obvious four, both of which would otherwise get a
  // colour that flatters them:
  //   forced    -- applied deliberately WITHOUT passing. Not a failure, but
  //                not trustworthy either, so it sits with the red group.
  //   mismatch  -- passed, but against a different Ollama host. Showing that
  //                green is precisely the silent carry-over that moving from
  //                .125 to .97 caused.
  const MODEL_STATES = {
    untested:   {cls: 'ms-none', text: '\u2014 untested'},
    failed:     {cls: 'ms-bad',  text: '\u2717 failed'},
    forced:     {cls: 'ms-bad',  text: '\u26a0 forced'},
    mismatch:   {cls: 'ms-bad',  text: '\u26a0 other host'},
    validated:  {cls: 'ms-warn', text: '\u2713 validated'},
    calibrated: {cls: 'ms-good', text: '\u2713 calibrated'},
  };

  function modelStateKey(rec) {
    if (!rec) return 'untested';
    if (rec.status === 'failed') return 'failed';
    if (rec.status === 'forced') return 'forced';
    if (rec.status !== 'supported') return 'untested';
    if (rec.host_state === 'mismatch') return 'mismatch';
    return rec.calibrated ? 'calibrated' : 'validated';
  }

  function modelState(rec) {
    return MODEL_STATES[modelStateKey(rec)] || MODEL_STATES.untested;
  }

  // Kept for the detail box, which describes the RECORD rather than the
  // model's usable state.
  const VAL_MARK = {supported: '✓ validated', failed: '✗ failed',
                    forced: '⚠ forced'};

  // Colour AND text on every option.
  //
  // Chrome, Edge and Firefox honour a colour set on an <option>; the macOS
  // native popup and most mobile pickers do not. The text is therefore the
  // guaranteed channel and the colour is the fast one -- dropping the text
  // would make this pane unreadable on the platforms that ignore the colour.
  function decorateModelOptions() {
    const sel = document.getElementById('ollama-model-select');
    if (!sel) return;
    Array.from(sel.options).forEach(function (opt) {
      const base = opt.dataset.baseLabel || opt.textContent.trim();
      opt.dataset.baseLabel = base;
      if (!opt.value) return;              // the placeholder row
      const st = modelState(_validations[opt.value]);
      opt.textContent = base + '  ' + st.text;
      opt.className = 'model-opt ' + st.cls;
    });
    paintSelectedModel();
  }

  // The closed select shows only the selected row, and a <select> does not
  // inherit the chosen option's colour, so it is applied to the control too.
  function paintSelectedModel() {
    const sel = document.getElementById('ollama-model-select');
    if (!sel) return;
    sel.classList.remove('ms-none', 'ms-bad', 'ms-warn', 'ms-good');
    sel.classList.add(modelState(_validations[sel.value]).cls);
  }

  function renderValidationDetail() {
    const sel = document.getElementById('ollama-model-select');
    const box = document.getElementById('validation-detail');
    if (!sel || !box) return;
    const model = sel.value;
    if (!model) { box.textContent = 'Select a model to see its validation status.'; return; }
    const rec = _validations[model];
    if (!rec) {
      box.innerHTML = '<strong>' + escapeHtml(model) + '</strong> — not yet validated. ' +
                      'Click Validate to test it.';
      return;
    }
    let rows = (rec.tests || []).map(function (t) {
      return '<li>' + (t.passed ? '✓ ' : '✗ ') +
             escapeHtml(t.name) + ' — ' + escapeHtml(t.detail || '') + '</li>';
    }).join('');
    const when = rec.validated_at ? rec.validated_at.replace('T', ' ').slice(0, 16) : '';
    box.innerHTML =
      '<strong>' + escapeHtml(model) + '</strong> — ' +
      (VAL_MARK[rec.status] || rec.status) +
      (rec.indexer_count ? ' at ' + rec.indexer_count + ' indexers' : '') +
      (rec.avg_response_ms ? ', avg ' + Math.round(rec.avg_response_ms / 1000) + 's' : '') +
      (when ? ', ' + when : '') +
      (rows ? '<ul class="reason-ul">' + rows + '</ul>' : '');
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;',
              '"': '&quot;', "'": '&#39;'}[c];
    });
  }

  function loadValidations() {
    return fetch('/config/ai/validations')
      .then(r => r.json())
      .then(function (d) {
        _validations = {};
        (d.validations || []).forEach(function (v) { _validations[v.model] = v; });
        decorateModelOptions();
        renderValidationDetail();
        renderValidationTable(d.validations || []);
      })
      .catch(function () { /* status display only -- never block the pane */ });
  }

  function renderValidationTable(list) {
    const tbl = document.getElementById('validation-table');
    const body = document.getElementById('validation-tbody');
    if (!tbl || !body) return;
    if (!list.length) { tbl.style.display = 'none'; return; }
    tbl.style.display = '';
    function cell(t) {
      if (!t) return '<td class="mono">—</td>';
      return '<td class="mono" title="' + escapeHtml(t.detail || '') + '">' +
             (t.passed ? '✓' : '✗') + '</td>';
    }
    body.innerHTML = list.map(function (v) {
      const byName = {};
      (v.tests || []).forEach(function (t) {
        if (/^Discrimination/.test(t.name)) byName.disc = t;
        else if (/^Schema/.test(t.name)) byName.schema = t;
        else if (/^Context/.test(t.name)) byName.ctx = t;
      });
      const st = modelState(v);

      // Calibrated shows the MEASURED ceiling, because "yes" on its own is
      // not the useful part -- the number is what the slider is set from.
      const calCell = v.calibrated
        ? '<td class="mono ms-good" title="Largest batch that scored cleanly '
          + 'twice in a row">\u2713 ' + (v.max_safe_batch || '?') + '/call</td>'
        : '<td class="mono ms-warn" title="Validated, but the batch ceiling '
          + 'was never measured. Run Calibrate.">\u2014</td>';

      // Which machine earned this verdict. Blank host means the record
      // predates host tracking, which is not the same as a wrong host.
      let hostCell;
      if (v.host_state === 'mismatch') {
        hostCell = '<td class="mono ms-bad" title="' + escapeHtml(v.ollama_url || '')
          + '">other host</td>';
      } else if (v.host_state === 'match') {
        hostCell = '<td class="mono ms-good" title="' + escapeHtml(v.ollama_url || '')
          + '">this host'
          + (v.gpu_offload_pct === null || v.gpu_offload_pct === undefined ? ''
             : ' \u00b7 ' + (v.gpu_offload_pct === 0 ? 'CPU'
                              : v.gpu_offload_pct + '% GPU'))
          + '</td>';
      } else {
        hostCell = '<td class="mono ms-none" title="Recorded before the host '
          + 'was tracked">unknown</td>';
      }

      return '<tr>' +
        '<td class="mono">' + escapeHtml(v.model) + '</td>' +
        '<td class="' + st.cls + '">' + st.text + '</td>' +
        cell(byName.disc) + cell(byName.schema) + cell(byName.ctx) +
        calCell + hostCell +
        '<td class="mono">' + (v.avg_response_ms
            ? Math.round(v.avg_response_ms / 1000) + 's' : '—') + '</td>' +
        '<td class="mono">' + escapeHtml((v.validated_at || '')
            .replace('T', ' ').slice(0, 16)) + '</td>' +
        '<td><button type="button" class="btn btn-ghost btn-sm" ' +
            'data-action="clear-validation" data-model="' + escapeHtml(v.model) + '" ' +
            'title="Forget this result so the model shows as untested">' +
            'Clear</button></td>' +
      '</tr>';
    }).join('');
  }

  function clearValidation(model) {
    // Retracts a stored verdict. The model itself is untouched -- it simply
    // shows as untested again, and Apply will warn as it does for any
    // unvalidated model.
    fetch('/config/ai/validation/delete', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({model: model}),
    })
      .then(r => r.json())
      .then(function () { loadValidations(); });
  }

  // Show the progress row and start polling. Used both when we start a run
  // and when we discover one already in flight -- a validation takes a
  // couple of minutes and survives a page reload, so the UI has to be able
  // to rejoin one it did not start.
  function attachToValidation(model) {
    const prog = document.getElementById('validation-progress');
    const stage = document.getElementById('validation-stage');
    if (stage) {
      stage.textContent = 'Starting…' + (model ? ' — ' + model : '');
    }
    if (prog) prog.style.display = '';
    pollValidation();
  }

  // ---- batch size slider -------------------------------------------------
  //
  // The ceiling is measured, so the slider can say which positions are known
  // good and which are guesses. A modal on every nudge would be worse than
  // useless: people learn to dismiss it, and then the one that mattered goes
  // unread too. So movement inside the measured range is silent, and only
  // crossing ABOVE the measured ceiling asks for confirmation -- that is the
  // only move with a known failure behind it.
  let BATCH_CAL = null;

  function onBatchInput() {
    const el = document.getElementById('ollama_max_indexers_per_call');
    const out = document.getElementById('batch-value');
    if (out && el) out.textContent = el.value;
    paintBatchZone();
  }

  function paintBatchZone() {
    const el = document.getElementById('ollama_max_indexers_per_call');
    const note = document.getElementById('batch-calibration');
    if (!el || !note) return;
    const v = parseInt(el.value, 10);
    const cal = BATCH_CAL || {};

    if (!cal.calibrated || !cal.max_safe_batch) {
      note.textContent = cal.host_state === 'mismatch'
        ? 'Not calibrated on this host \u2014 the stored result came from '
          + (cal.validated_on || 'another server') + '. Run Calibrate.'
        : 'Not calibrated. Run Calibrate to measure the largest batch this '
          + 'model handles reliably here.';
      note.style.color = 'var(--warning)';
      return;
    }
    const safe = cal.max_safe_batch;
    let hw = '';
    if (cal.gpu_offload_pct !== null && cal.gpu_offload_pct !== undefined) {
      hw = ' (' + (cal.gpu_offload_pct === 0 ? 'CPU only'
            : cal.gpu_offload_pct + '% on GPU')
           + (cal.tok_per_s ? ', ' + cal.tok_per_s + ' tok/s' : '') + ')';
    }
    if (v <= safe) {
      note.textContent = 'Measured safe up to ' + safe + ' on this host' + hw + '.';
      note.style.color = 'var(--success)';
    } else {
      note.textContent = 'Above the measured ceiling of ' + safe
        + '. Indexers may be dropped silently' + hw + '.';
      note.style.color = 'var(--error)';
    }
  }

  function onBatchCommit() {
    const el = document.getElementById('ollama_max_indexers_per_call');
    const cal = BATCH_CAL || {};
    if (!el || !cal.calibrated || !cal.max_safe_batch) return;
    const v = parseInt(el.value, 10);
    if (v <= cal.max_safe_batch) return;
    const ok = confirm(
      'Measured ceiling for ' + (cal.model || 'this model') + ' on this host is '
      + cal.max_safe_batch + ' indexers per call.\n\n'
      + 'Above that, this model returned well-formed JSON that silently left '
      + 'indexers out. They do not error -- they quietly keep their '
      + 'deterministic score, and if auto-manage is on they can be disabled '
      + 'on it.\n\nSet it to ' + v + ' anyway?');
    if (!ok) {
      el.value = cal.max_safe_batch;
      onBatchInput();
    }
  }

  function loadBatchCalibration() {
    fetch('/config/ai/calibration')
      .then(r => r.json())
      .then(function (d) { if (d.ok) { BATCH_CAL = d; } onBatchInput(); })
      .catch(function () { onBatchInput(); });
  }

  function validateModel(mode) {
    const sel = document.getElementById('ollama-model-select');
    const model = sel ? sel.value : '';
    if (!model) return;
    const deep = (mode === 'deep');
    if (deep) {
      if (!confirm(
          'In-depth calibration scores your indexers repeatedly at several '
          + 'batch sizes, and each size has to come back clean twice before '
          + 'it is accepted \u2014 one clean run is not evidence, because '
          + 'dropped indexers happen intermittently.\n\n'
          + 'On a slow or CPU-only host with a lot of indexers this can take '
          + '20-40 minutes. It runs in the background and you can keep '
          + 'working.\n\nStart calibration?')) {
        return;
      }
    }
    // A finished calibration changes the slider's ceiling, so re-read it
    // once the run reports done rather than leaving the old zone painted.
    window.__afterValidation = loadBatchCalibration;
    // Immediate feedback: the request itself can take a moment, and silence
    // reads as a dead button.
    attachToValidation(model);
    fetch('/config/ai/validate', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({model: model, calibration: deep ? 'deep' : 'quick'}),
    })
      .then(r => r.json().then(d => ({code: r.status, d: d})))
      .then(function (res) {
        if (res.d.ok) return;                 // polling already attached
        if (res.code === 409) {
          // Something else is already validating. Follow that run rather
          // than reporting an error, but say so -- otherwise the progress
          // row appears to be for the model the user just clicked, and it
          // is not. Only one run at a time: concurrent runs against one
          // Ollama host make both slower and the timings meaningless.
          const el = document.getElementById('validation-detail');
          if (el) {
            el.textContent = (res.d.message || 'Already running') +
              '. Showing that run — another model is still being validated, ' +
              'so this one has not started. Try again once it finishes.';
          }
          return;
        }
        if (_validationPoll) { clearInterval(_validationPoll); _validationPoll = null; }
        const prog = document.getElementById('validation-progress');
        if (prog) prog.style.display = 'none';
        document.getElementById('validation-detail').textContent =
          res.d.message || 'Could not start validation';
      })
      .catch(function (e) {
        if (_validationPoll) { clearInterval(_validationPoll); _validationPoll = null; }
        const prog = document.getElementById('validation-progress');
        if (prog) prog.style.display = 'none';
        document.getElementById('validation-detail').textContent = 'Request failed: ' + e;
      });
  }

  // On load, rejoin a validation that is already running -- otherwise a
  // reload during a two-minute run leaves it invisible, and the next click
  // just reports "already running" with nothing on screen to explain it.
  function resumeValidationIfRunning() {
    fetch('/config/ai/validate/status')
      .then(r => r.json())
      .then(function (s) {
        if (s && s.running) attachToValidation(s.model);
      })
      .catch(function () { /* status only -- never block the pane */ });
  }

  function pollValidation() {
    if (_validationPoll) clearInterval(_validationPoll);
    _validationPoll = setInterval(function () {
      fetch('/config/ai/validate/status').then(r => r.json()).then(function (s) {
        const stage = document.getElementById('validation-stage');
        if (stage) {
          stage.textContent = (s.stage || 'Working') +
            ' (' + (s.done || 0) + '/' + (s.total || 3) + ')' +
            (s.model ? ' — ' + s.model : '');
        }
        if (!s.running) {
          clearInterval(_validationPoll); _validationPoll = null;
          document.getElementById('validation-progress').style.display = 'none';
          if (s.interrupted && s.error) {
            document.getElementById('validation-detail').textContent = s.error;
          }
          loadValidations();
        }
      }).catch(function () {
        clearInterval(_validationPoll); _validationPoll = null;
        document.getElementById('validation-progress').style.display = 'none';
      });
    }, 2000);
  }

  // Apply never starts a validation run. On an unvalidated model it explains
  // the situation and offers Cancel or Apply anyway; validation is always an
  // explicit, separate action via the Validate button.
  function applyOllamaModel() {
    const sel = document.getElementById('ollama-model-select');
    const status = document.getElementById('ollama-model-status');
    const model = sel ? sel.value : '';
    if (!model) return;
    _applyModel(model, false).then(function (res) {
      if (res.code === 409 && res.d.needs_validation) {
        showApplyAnywayDialog(model, res.d);
        return;
      }
      if (status) {
        status.textContent = (res.d.ok ? '\u2713 ' : '\u2717 ') + res.d.message;
        status.className = 'hint ' + (res.d.ok ? 'ms-good' : 'ms-bad');
      }
      if (res.d.ok) {
        // A validated-but-uncalibrated model applies cleanly -- it scores
        // correctly, only its batch ceiling is unmeasured. That does not
        // warrant a dialog; it warrants a sentence. Blocking it would put a
        // model that works behind the same barrier as one that failed, and
        // a dialog people learn to dismiss is worse than no dialog.
        if (modelStateKey(_validations[model]) === 'validated' && status) {
          status.className = 'hint ms-warn';
          status.textContent =
            '\u2713 applied. Batch size is not calibrated on this host, so '
            + 'scoring uses the default. Run Calibrate to measure it.';
        }
        loadValidations();
      }
    });
  }

  function _applyModel(model, force) {
    return fetch('/config/ai/model', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({model: model, force: !!force}),
    }).then(r => r.json().then(d => ({code: r.status, d: d})));
  }

  // Four situations reach this dialog and they are not the same problem, so
  // they do not get the same sentence. The old copy said "has not been
  // validated" for all of them, which was simply untrue for a model that had
  // passed on another host, and unhelpful for one that had failed outright.
  const APPLY_COPY = {
    untested: {
      title: 'Apply an untested model?',
      known: 'Nothing is known about this model on this server yet.',
      advice: 'Run <strong>Quick Check</strong> first (about 1-3 minutes) to '
              + 'confirm it can score at all, or <strong>Calibrate</strong> to '
              + 'do that and measure its batch size.',
    },
    failed: {
      title: 'Apply a model that failed?',
      known: 'This model was tested and <strong>failed</strong>. It did not '
             + 'score correctly on this server.',
      advice: 'Choosing a different model is usually the right move. The '
              + 'failure detail is in the table below.',
    },
    forced: {
      title: 'Apply an unvalidated model again?',
      known: 'This model was previously applied <strong>without passing '
             + 'validation</strong>.',
      advice: 'Run <strong>Quick Check</strong> to find out whether it '
              + 'actually works before relying on it.',
    },
    mismatch: {
      title: 'Apply a model validated on another server?',
      known: 'This model passed \u2014 but on a <strong>different Ollama '
             + 'host</strong>, so that result does not describe this one.',
      advice: 'Batch size and reliability differ between machines: the same '
              + 'model can score cleanly on one host and drop indexers on '
              + 'another. Run <strong>Calibrate</strong> here.',
    },
  };

  function showApplyAnywayDialog(model, payload) {
    const existing = document.getElementById('apply-anyway-backdrop');
    if (existing) existing.remove();
    payload = payload || {};
    // The server sends status + host_state; derive the same key the badges
    // use so the dialog can never describe a different state than the row.
    let key = 'untested';
    if (payload.status === 'failed') key = 'failed';
    else if (payload.status === 'forced') key = 'forced';
    else if (payload.host_state === 'mismatch') key = 'mismatch';
    const copy = APPLY_COPY[key] || APPLY_COPY.untested;
    const known = copy.known
      + (key === 'mismatch' && payload.validated_on
         ? ' <br><span class="hint">Validated on <code>'
           + escapeHtml(payload.validated_on) + '</code>, now using <code>'
           + escapeHtml(payload.current_host || '?') + '</code>.</span>'
         : '');
    const wrap = document.createElement('div');
    wrap.id = 'apply-anyway-backdrop';
    wrap.setAttribute('role', 'dialog');
    wrap.setAttribute('aria-modal', 'true');
    wrap.setAttribute('aria-labelledby', 'apply-anyway-title');
    wrap.className = 'modal-backdrop';
    wrap.innerHTML =
      '<div class="modal-panel">' +
        '<div id="apply-anyway-title" class="form-section-title mb-8">' + copy.title + '</div>' +
        '<p class="hint mb-8">' + known + '</p>' +
        '<p class="hint mb-8"><code>' +
          escapeHtml(model) + '</code> may score indexers incorrectly. The '
          + 'failure to worry about is silent: a model that mishandles the '
          + 'prompt returns plausible numbers, or quietly leaves indexers '
          + 'out, rather than raising an error.</p>' +
        '<p class="hint mb-16">' + copy.advice +
          ' Applying anyway is recorded as <em>forced</em>.</p>' +
        '<div class="flex gap-8 justify-end">' +
          '<button type="button" class="btn btn-ghost btn-sm" ' +
                  'id="apply-anyway-cancel">Cancel</button>' +
          '<button type="button" class="btn btn-primary btn-sm" ' +
                  'id="apply-anyway-go">Apply anyway</button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(wrap);

    function close() {
      document.removeEventListener('keydown', onKey);
      wrap.remove();
    }
    function onKey(e) { if (e.key === 'Escape') close(); }
    document.addEventListener('keydown', onKey);
    wrap.addEventListener('click', function (e) { if (e.target === wrap) close(); });
    document.getElementById('apply-anyway-cancel').onclick = close;
    document.getElementById('apply-anyway-go').onclick = function () {
      close();
      _applyModel(model, true).then(function (res) {
        const status = document.getElementById('ollama-model-status');
        if (status) status.textContent = (res.d.ok ? '⚠ ' : '✗ ') + res.d.message;
        loadValidations();
      });
    };
    document.getElementById('apply-anyway-go').focus();
  }

  // ---- Ollama update check ----
  function checkOllamaUpdate() {
    fetch('/config/ollama/update-check')
      .then(r => r.json())
      .then(d => {
        const badge = document.getElementById('ollama-update-badge');
        if (!(d.ok && d.update_available)) return;
        if (badge) {
          // Two different findings, two different reactions. "Your local
          // model was replaced since validation" means re-validate; "upstream
          // published a newer build" means pull. A single generic badge
          // would leave the user guessing which.
          badge.style.display = '';
          badge.textContent = d.reason === 'upstream'
            ? 'Newer build available upstream'
            : 'Model changed since validation';
          badge.title = d.message || '';
        }
        // Only offer the button for the case a pull actually fixes.
        const btn = document.getElementById('ollama-pull-btn');
        if (btn && d.reason === 'upstream') btn.style.display = '';

        const navItem = document.querySelector('#settings-group [data-pane="ai"]');
        if (navItem && !navItem.querySelector('.update-dot')) {
          const dot = document.createElement('span');
          dot.className = 'update-dot';
          dot.title = d.message || 'Ollama model update available';
          dot.classList.add('update-dot-inline');
          navItem.appendChild(dot);
        }
      })
      .catch(() => {}); // silently ignore if Ollama is not configured
  }

  // ---- Pull the newer build (ROADMAP item 17) ----
  let _pullPoll = null;

  function pullModel() {
    const sel = document.getElementById('ollama-model-select');
    const model = sel ? sel.value : '';
    if (!confirm('Download the newer build of ' + (model || 'this model') +
                 '?\n\nThis can be several gigabytes and will replace the ' +
                 'installed copy. Scoring keeps using the current build until ' +
                 'it finishes.')) return;

    setPullUI('Starting…', 0, true);
    fetch('/config/ollama/pull', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({model: model})
    })
      .then(r => r.json().then(d => ({status: r.status, d: d})))
      .then(res => {
        // 409 means one is already running -- follow it rather than
        // reporting an error, the same reasoning as the validation UI.
        if (res.status === 409) setPullUI(res.d.message, 0, true);
        else if (!res.d.ok) { setPullUI(res.d.message, 0, false); return; }
        pollPull();
      })
      .catch(e => setPullUI('Could not start: ' + e, 0, false));
  }

  function setPullUI(text, percent, running) {
    const row = document.getElementById('pull-progress');
    const label = document.getElementById('pull-status');
    const bar = document.getElementById('pull-bar');
    const btn = document.getElementById('ollama-pull-btn');
    if (row) row.style.display = running || text ? '' : 'none';
    if (label) label.textContent = text || '';
    if (bar) bar.style.width = (percent || 0) + '%';
    if (btn) btn.disabled = !!running;
  }

  function pollPull() {
    if (_pullPoll) clearInterval(_pullPoll);
    _pullPoll = setInterval(function () {
      fetch('/config/ollama/pull/status')
        .then(r => r.json())
        .then(s => {
          if (s.error) {
            clearInterval(_pullPoll); _pullPoll = null;
            setPullUI('Failed: ' + s.error, 0, false);
            return;
          }
          if (s.done) {
            clearInterval(_pullPoll); _pullPoll = null;
            setPullUI('Done. Re-validate before relying on it.', 100, false);
            const badge = document.getElementById('ollama-update-badge');
            if (badge) badge.style.display = 'none';
            const btn = document.getElementById('ollama-pull-btn');
            if (btn) btn.style.display = 'none';
            // The pulled build is NOT the validated one. Refresh the table so
            // that is visible rather than implied.
            if (typeof loadValidations === 'function') loadValidations();
            return;
          }
          const mb = s.total ? ' (' + Math.round(s.completed / 1048576) + ' / ' +
                               Math.round(s.total / 1048576) + ' MB)' : '';
          setPullUI((s.status || 'Working') + mb, s.percent, true);
        })
        .catch(() => {});
    }, 1000);
  }

  // Rejoin a pull already in flight, so a reload does not hide it.
  function resumePullIfRunning() {
    fetch('/config/ollama/pull/status')
      .then(r => r.json())
      .then(s => { if (s && s.running) { setPullUI(s.status || 'Working', s.percent, true); pollPull(); } })
      .catch(() => {});
  }

  // Re-fire deferred hooks for the initial pane (wrapper + functions defined above)
  if (_initialPane === 'backups') loadBackups();
  if (_initialPane === 'ai') {
    loadOllamaModels(); loadSystemPrompt(); loadBatchCalibration();
  }
  // Always check for Ollama updates on Settings page load
  checkOllamaUpdate();
  resumePullIfRunning();
