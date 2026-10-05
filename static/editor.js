// Envelope / template editor: recipients, documents and drag-and-drop field placement.
(function () {
  const C = window.EDITOR;
  const $ = (s, el = document) => el.querySelector(s);
  const state = {
    docs: C.docs.slice(),
    recipients: C.recipients.length ? C.recipients.slice() : [
      { key: 'r1', name: C.mode === 'template' ? 'Client' : '', email: '', kind: 'signer', order: 1 }],
    fields: C.fields.slice(),
    active: null, selected: null, pending: null, dirty: false,
  };
  state.active = state.recipients[0].key;
  const ICONS = {
    signature: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 17c3-1 4-7 7-7s1 6 4 6 3-3 5-3M3 21h18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
    initials: '<svg viewBox="0 0 24 24" aria-hidden="true"><text x="3" y="17" font-size="13" font-weight="700" fill="currentColor" font-family="Georgia,serif">AB</text></svg>',
    date_signed: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="5" width="18" height="16" rx="2" fill="none" stroke="currentColor" stroke-width="2"/><path d="M3 10h18M8 3v4M16 3v4" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
    name: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="8" r="4" fill="none" stroke="currentColor" stroke-width="2"/><path d="M4 21c1-4 4-6 8-6s7 2 8 6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
    text: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 6h14M12 6v13M9 19h6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
    checkbox: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="4" width="16" height="16" rx="3" fill="none" stroke="currentColor" stroke-width="2"/><path d="M8 12l3 3 5-6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
  };
  const HELP = {
    signature: 'The recipient adopts a signature once and it fills every signature field assigned to them.',
    initials: 'Filled with the recipient\'s adopted initials.',
    date_signed: 'Filled automatically with the date they sign.',
    name: 'Filled automatically with the recipient\'s full name.',
    text: 'A box the recipient types into, such as a title, address or company name.',
    checkbox: 'A box the recipient ticks. Mark it required if they must tick it to finish.',
  };
  const uid = p => p + Math.random().toString(36).slice(2, 8);
  const rIndex = key => Math.max(0, state.recipients.findIndex(r => r.key === key));
  const rColor = key => `var(--r${rIndex(key) % 6})`;
  const rLabel = r => r ? (r.name || (C.mode === 'template' ? 'Unnamed role' : 'Unnamed recipient')) : '?';

  function setDirty(v = true) { state.dirty = v; status(v ? 'Unsaved changes' : ''); }
  function status(t, err) { const s = $('#ed-status'); s.textContent = t; s.style.color = err ? 'var(--bad)' : ''; }
  window.addEventListener('beforeunload', e => { if (state.dirty) { e.preventDefault(); e.returnValue = ''; } });

  // ---------------------------------------------------------------- title + message
  $('#ed-title').value = C.title || '';
  $('#ed-message').value = C.message || '';
  $('#ed-title').addEventListener('input', () => setDirty());
  $('#ed-message').addEventListener('input', () => setDirty());

  // ---------------------------------------------------------------- recipients
  function renderRecipients() {
    const box = $('#recipients');
    box.innerHTML = '';
    state.recipients.forEach((r, i) => {
      const row = document.createElement('div');
      row.className = 'recip' + (r.key === state.active ? ' active' : '');
      row.style.setProperty('--rc', rColor(r.key));
      const count = state.fields.filter(f => f.recipient === r.key).length;
      row.innerHTML = `
        <div class="recip-head"><span class="rdot"></span>
          <input type="text" class="r-name" placeholder="${C.mode === 'template' ? 'Role, e.g. Client' : 'Full name'}" aria-label="Name">
          <button type="button" class="btn btn-quiet small r-del" aria-label="Remove">✕</button></div>
        ${C.mode === 'template' ? '' : '<input type="email" class="r-email" placeholder="Email" aria-label="Email">'}
        <div class="recip-meta">
          <select class="r-kind" aria-label="Action"><option value="signer">Needs to sign</option><option value="cc">Gets a copy</option></select>
          <label class="r-order-wrap small">Order <input type="number" class="r-order" min="1" max="20" aria-label="Signing order"></label>
          <span class="muted small r-count">${count} field${count === 1 ? '' : 's'}</span>
        </div>`;
      $('.r-name', row).value = r.name;
      if ($('.r-email', row)) $('.r-email', row).value = r.email;
      $('.r-kind', row).value = r.kind;
      $('.r-order', row).value = r.order;
      row.addEventListener('pointerdown', () => { if (state.active !== r.key) { state.active = r.key; refreshActive(); } });
      $('.r-name', row).addEventListener('input', e => { r.name = e.target.value; setDirty(); renderRecipientSelects(); paintFieldLabels(); });
      if ($('.r-email', row)) $('.r-email', row).addEventListener('input', e => { r.email = e.target.value; setDirty(); });
      $('.r-kind', row).addEventListener('change', e => {
        r.kind = e.target.value; setDirty();
        if (r.kind === 'cc' && state.fields.some(f => f.recipient === r.key)) {
          state.fields = state.fields.filter(f => f.recipient !== r.key); renderAllFields(); status('Removed fields: copy recipients don\'t fill anything in.');
        }
        renderRecipientSelects();
      });
      $('.r-order', row).addEventListener('input', e => { r.order = Math.max(1, parseInt(e.target.value || '1', 10)); setDirty(); });
      $('.r-del', row).addEventListener('click', ev => {
        ev.stopPropagation();
        if (state.recipients.length === 1) { status('Keep at least one recipient.', true); return; }
        const n = state.fields.filter(f => f.recipient === r.key).length;
        if (n && !confirm(`Remove ${rLabel(r)} and their ${n} field${n === 1 ? '' : 's'}?`)) return;
        state.recipients = state.recipients.filter(x => x !== r);
        state.fields = state.fields.filter(f => f.recipient !== r.key);
        if (state.active === r.key) state.active = state.recipients[0].key;
        setDirty(); renderRecipients(); renderRecipientSelects(); renderAllFields(); select(null);
      });
      box.appendChild(row);
    });
  }
  $('#add-recipient').addEventListener('click', () => {
    const maxOrder = Math.max(...state.recipients.map(r => r.order), 0);
    const r = { key: uid('r'), name: '', email: '', kind: 'signer', order: Math.min(maxOrder + 1, 20) };
    state.recipients.push(r); state.active = r.key; setDirty();
    renderRecipients(); renderRecipientSelects();
    $('#recipients .recip:last-child .r-name').focus();
  });

  function renderRecipientSelects() {
    const signers = state.recipients.filter(r => r.kind === 'signer');
    if (!signers.find(r => r.key === state.active) && signers.length) state.active = signers[0].key;
    for (const id of ['#active-recipient', '#prop-recipient']) {
      const sel = $(id); const v = id === '#active-recipient' ? state.active : (selectedField() || {}).recipient;
      sel.innerHTML = signers.map(r => `<option value="${r.key}">${escapeHtml(rLabel(r))}</option>`).join('');
      if (v) sel.value = v;
    }
    $('#active-recipient').style.setProperty('--rc', rColor(state.active));
    document.querySelectorAll('.pal-item').forEach(p => p.style.setProperty('--rc', rColor(state.active)));
  }
  $('#active-recipient').addEventListener('change', e => { state.active = e.target.value; refreshActive(); });
  function refreshActive() {
    document.querySelectorAll('.recip').forEach((row, i) => row.classList.toggle('active', state.recipients[i] && state.recipients[i].key === state.active));
    renderRecipientSelects();
  }

  // ---------------------------------------------------------------- documents
  function renderDocList() {
    const box = $('#doc-list'); box.innerHTML = '';
    state.docs.forEach((d, i) => {
      const row = document.createElement('div'); row.className = 'doc-mini-row';
      row.innerHTML = `<input type="text" aria-label="Document name"><span class="muted small">${d.pages.length} p.</span>
        <button type="button" class="btn btn-quiet small" aria-label="Move up" ${i === 0 ? 'disabled' : ''}>↑</button>
        <button type="button" class="btn btn-quiet small btn-danger" aria-label="Remove document">✕</button>`;
      const [inp] = row.getElementsByTagName('input'); inp.value = d.name;
      inp.addEventListener('input', () => { d.name = inp.value; setDirty(); const h = document.querySelector(`[data-doc-head="${d.id}"]`); if (h) h.textContent = d.name; });
      const [up, del] = row.getElementsByTagName('button');
      up.addEventListener('click', () => { state.docs.splice(i, 1); state.docs.splice(i - 1, 0, d); setDirty(); renderDocList(); renderPages(); });
      del.addEventListener('click', () => {
        if (state.docs.length === 1) { status('An envelope needs at least one document.', true); return; }
        if (!confirm(`Remove “${d.name}” and its fields?`)) return;
        state.docs.splice(i, 1); state.fields = state.fields.filter(f => f.doc !== d.id); setDirty(); renderDocList(); renderPages();
      });
      box.appendChild(row);
    });
  }
  $('#add-doc').addEventListener('change', async e => {
    const files = [...e.target.files]; if (!files.length) return;
    const fd = new FormData(); files.forEach(f => fd.append('files', f));
    status('Uploading…');
    try {
      const res = await fetch(C.addDocUrl, { method: 'POST', body: fd, headers: { 'X-CSRF-Token': window.CSRF } });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Upload failed');
      state.docs.push(...data.docs); renderDocList(); renderPages(); setDirty(); status('Document added');
    } catch (err) { status(err.message, true); }
    e.target.value = '';
  });

  // ---------------------------------------------------------------- pages + fields
  const canvas = $('#canvas');
  function pageSrc(docId, n) { return C.pageUrl.replace('__DOC__', docId) + n + '.png'; }
  function renderPages() {
    canvas.innerHTML = '';
    state.docs.forEach(d => {
      const head = document.createElement('div'); head.className = 'doc-head';
      head.innerHTML = `<span class="eyebrow">Document</span> <strong data-doc-head="${d.id}"></strong>`;
      head.querySelector('strong').textContent = d.name;
      canvas.appendChild(head);
      d.pages.forEach((p, n) => {
        const page = document.createElement('div');
        page.className = 'page'; page.dataset.doc = d.id; page.dataset.page = n;
        page.style.aspectRatio = `${p.w} / ${p.h}`;
        page.innerHTML = `<img src="${pageSrc(d.id, n)}" alt="${escapeHtml(d.name)}, page ${n + 1}" loading="lazy" draggable="false"><span class="page-num">${n + 1} / ${d.pages.length}</span>`;
        page.addEventListener('pointerdown', e => {
          if (e.target.closest('.fld')) return;
          if (state.pending) { placeAt(state.pending, page, e.clientX, e.clientY, true); setPending(null); e.preventDefault(); }
          else select(null);
        });
        canvas.appendChild(page);
      });
    });
    renderAllFields();
  }
  function pageEl(f) { return canvas.querySelector(`.page[data-doc="${f.doc}"][data-page="${f.page}"]`); }
  function renderAllFields() { canvas.querySelectorAll('.fld').forEach(x => x.remove()); state.fields.forEach(renderField); renderRecipients(); }
  function fieldText(f) {
    const r = state.recipients.find(x => x.key === f.recipient);
    const base = f.type === 'text' && f.label ? f.label : C.types[f.type].label;
    return { base, who: rLabel(r) };
  }
  function renderField(f) {
    const page = pageEl(f); if (!page) return;
    const el = document.createElement('div');
    el.className = `fld fld-${f.type}` + (state.selected === f.id ? ' selected' : '');
    el.dataset.id = f.id; el.tabIndex = 0;
    el.style.cssText = `left:${f.x * 100}%;top:${f.y * 100}%;width:${f.w * 100}%;height:${f.h * 100}%;--rc:${rColor(f.recipient)}`;
    const t = fieldText(f);
    el.innerHTML = `<span class="fld-icon">${ICONS[f.type]}</span><span class="fld-label">${escapeHtml(t.base)}${f.required && ['text', 'checkbox'].includes(f.type) ? '<b>*</b>' : ''}</span><span class="fld-who">${escapeHtml(t.who)}</span><span class="fld-handle" aria-hidden="true"></span>`;
    el.addEventListener('pointerdown', e => startMove(e, f, el));
    el.addEventListener('focus', () => select(f.id));
    page.appendChild(el);
  }
  function paintFieldLabels() {
    state.fields.forEach(f => { const el = canvas.querySelector(`.fld[data-id="${f.id}"]`); if (el) { const t = fieldText(f); el.querySelector('.fld-who').textContent = t.who; } });
  }
  function updateFieldEl(f) {
    const el = canvas.querySelector(`.fld[data-id="${f.id}"]`); if (!el) return;
    el.style.left = f.x * 100 + '%'; el.style.top = f.y * 100 + '%'; el.style.width = f.w * 100 + '%'; el.style.height = f.h * 100 + '%';
  }
  function clamp(v, a, b) { return Math.min(Math.max(v, a), b); }

  function placeAt(type, page, cx, cy, center) {
    const signers = state.recipients.filter(r => r.kind === 'signer');
    if (!signers.length) { status('Add a recipient who needs to sign first.', true); return; }
    const rect = page.getBoundingClientRect();
    const def = C.types[type];
    const docPage = state.docs.find(d => d.id === page.dataset.doc).pages[+page.dataset.page];
    // default sizes are relative to a portrait letter page; keep physical size on other page shapes
    const w = def.w * (612 / docPage.w), h = def.h * (792 / docPage.h);
    let x = (cx - rect.left) / rect.width - (center ? w / 2 : 0.0);
    let y = (cy - rect.top) / rect.height - (center ? h / 2 : 0.0);
    const f = { id: uid('f'), type, recipient: state.active, doc: page.dataset.doc, page: +page.dataset.page,
      x: clamp(x, 0, 1 - w), y: clamp(y, 0, 1 - h), w, h, required: type !== 'checkbox', label: '' };
    state.fields.push(f); renderField(f); select(f.id); setDirty(); renderRecipients();
  }

  function startMove(e, f, el) {
    e.stopPropagation(); e.preventDefault();
    select(f.id);
    const page = el.parentElement, rect = page.getBoundingClientRect();
    const resizing = e.target.classList.contains('fld-handle');
    const sx = e.clientX, sy = e.clientY, o = { ...f };
    el.setPointerCapture(e.pointerId);
    const move = ev => {
      const dx = (ev.clientX - sx) / rect.width, dy = (ev.clientY - sy) / rect.height;
      if (resizing) { f.w = clamp(o.w + dx, 0.015, 1 - f.x); f.h = clamp(o.h + dy, 0.01, 1 - f.y); }
      else { f.x = clamp(o.x + dx, 0, 1 - f.w); f.y = clamp(o.y + dy, 0, 1 - f.h); }
      updateFieldEl(f);
    };
    const up = () => {
      el.removeEventListener('pointermove', move); el.removeEventListener('pointerup', up); el.removeEventListener('pointercancel', up);
      if (f.x !== o.x || f.y !== o.y || f.w !== o.w || f.h !== o.h) setDirty();
    };
    el.addEventListener('pointermove', move); el.addEventListener('pointerup', up); el.addEventListener('pointercancel', up);
  }

  // ---------------------------------------------------------------- palette (drag or click-to-place)
  const palette = $('#palette');
  Object.entries(C.types).forEach(([type, def]) => {
    const b = document.createElement('button');
    b.type = 'button'; b.className = 'pal-item'; b.dataset.type = type;
    b.innerHTML = `<span class="fld-icon">${ICONS[type]}</span>${def.label}`;
    b.addEventListener('pointerdown', e => startPaletteDrag(e, type, b));
    palette.appendChild(b);
  });
  function setPending(type) {
    state.pending = type;
    document.querySelectorAll('.pal-item').forEach(p => p.classList.toggle('armed', p.dataset.type === type));
    canvas.classList.toggle('placing', !!type);
    if (type) status(`Click on a page to place “${C.types[type].label}”. Esc to cancel.`);
  }
  function startPaletteDrag(e, type, btn) {
    e.preventDefault();
    const sx = e.clientX, sy = e.clientY; let ghost = null;
    const move = ev => {
      if (!ghost && Math.hypot(ev.clientX - sx, ev.clientY - sy) > 6) {
        ghost = document.createElement('div'); ghost.className = 'drag-ghost';
        ghost.style.setProperty('--rc', rColor(state.active));
        ghost.innerHTML = `<span class="fld-icon">${ICONS[type]}</span>${C.types[type].label}`;
        document.body.appendChild(ghost);
      }
      if (ghost) { ghost.style.left = ev.clientX + 'px'; ghost.style.top = ev.clientY + 'px'; }
    };
    const up = ev => {
      window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up);
      if (ghost) {
        ghost.remove();
        const target = document.elementFromPoint(ev.clientX, ev.clientY);
        const page = target && target.closest('.page');
        if (page) placeAt(type, page, ev.clientX, ev.clientY, true);
        else status('Drop the field onto a page.', true);
      } else setPending(state.pending === type ? null : type);
    };
    window.addEventListener('pointermove', move); window.addEventListener('pointerup', up);
  }

  // ---------------------------------------------------------------- selection + properties
  function selectedField() { return state.fields.find(f => f.id === state.selected); }
  function select(id) {
    state.selected = id;
    canvas.querySelectorAll('.fld').forEach(el => el.classList.toggle('selected', el.dataset.id === id));
    const f = selectedField(); const props = $('#props');
    props.hidden = !f;
    if (!f) return;
    renderRecipientSelects();
    $('#prop-recipient').value = f.recipient;
    $('#prop-required').checked = f.required;
    $('#prop-required').disabled = ['signature', 'date_signed', 'name', 'initials'].includes(f.type);
    $('#prop-label-wrap').hidden = !['text', 'checkbox'].includes(f.type);
    $('#prop-label').value = f.label || '';
    $('#prop-help').textContent = HELP[f.type];
  }
  $('#prop-recipient').addEventListener('change', e => { const f = selectedField(); f.recipient = e.target.value; setDirty(); renderAllFields(); select(f.id); });
  $('#prop-required').addEventListener('change', e => { const f = selectedField(); f.required = e.target.checked; setDirty(); renderAllFields(); select(f.id); });
  $('#prop-label').addEventListener('input', e => { const f = selectedField(); f.label = e.target.value; setDirty();
    const el = canvas.querySelector(`.fld[data-id="${f.id}"] .fld-label`); if (el) el.innerHTML = escapeHtml(fieldText(f).base) + (f.required && ['text', 'checkbox'].includes(f.type) ? '<b>*</b>' : ''); });
  $('#prop-delete').addEventListener('click', deleteSelected);
  function deleteSelected() {
    if (!state.selected) return;
    state.fields = state.fields.filter(f => f.id !== state.selected);
    canvas.querySelector(`.fld[data-id="${state.selected}"]`)?.remove();
    select(null); setDirty(); renderRecipients();
  }
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') { setPending(null); select(null); status(state.dirty ? 'Unsaved changes' : ''); }
    const typing = ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName);
    if (typing || !state.selected) return;
    const f = selectedField(); const step = e.shiftKey ? 0.01 : 0.002;
    if (e.key === 'Delete' || e.key === 'Backspace') { e.preventDefault(); deleteSelected(); }
    else if (e.key.startsWith('Arrow')) {
      e.preventDefault();
      if (e.key === 'ArrowLeft') f.x = clamp(f.x - step, 0, 1 - f.w);
      if (e.key === 'ArrowRight') f.x = clamp(f.x + step, 0, 1 - f.w);
      if (e.key === 'ArrowUp') f.y = clamp(f.y - step, 0, 1 - f.h);
      if (e.key === 'ArrowDown') f.y = clamp(f.y + step, 0, 1 - f.h);
      updateFieldEl(f); setDirty();
    }
  });

  // ---------------------------------------------------------------- save / send
  function payload(extra) {
    return Object.assign({
      title: $('#ed-title').value, message: $('#ed-message').value,
      docs: state.docs.map(d => ({ id: d.id, name: d.name })),
      recipients: state.recipients, fields: state.fields,
    }, extra || {});
  }
  async function post(extra) {
    const res = await fetch(C.saveUrl, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': window.CSRF }, body: JSON.stringify(payload(extra)) });
    let data = {}; try { data = await res.json(); } catch (_) { /* non-JSON */ }
    if (!res.ok) throw new Error(data.error || `Save failed (${res.status})`);
    return data;
  }
  $('#btn-save').addEventListener('click', async () => {
    status('Saving…');
    try { const d = await post(); setDirty(false); status(d.message || 'Saved'); } catch (err) { status(err.message, true); }
  });
  const useBtn = $('#btn-use');
  if (useBtn) useBtn.addEventListener('click', async e => {
    e.preventDefault();
    try { await post(); setDirty(false); location.href = useBtn.href; } catch (err) { status(err.message, true); }
  });

  function problems() {
    const out = [];
    const signers = state.recipients.filter(r => r.kind === 'signer');
    if (!signers.length) out.push('Add at least one recipient who needs to sign.');
    state.recipients.forEach(r => {
      if (!r.name.trim()) out.push('Every recipient needs a name.');
      else if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(r.email.trim())) out.push(`Add a valid email for ${r.name}.`);
    });
    signers.forEach(r => { if (r.name && !state.fields.some(f => f.recipient === r.key && f.type === 'signature')) out.push(`Place a signature field for ${r.name}.`); });
    return [...new Set(out)];
  }
  const sendBtn = $('#btn-send');
  if (sendBtn) {
    sendBtn.addEventListener('click', () => {
      const p = problems();
      if (p.length) { status(p[0], true); return; }
      const orders = [...new Set(state.recipients.map(r => r.order))].sort((a, b) => a - b);
      const summary = $('#send-summary');
      summary.innerHTML = `<p><strong>${escapeHtml($('#ed-title').value || 'Untitled')}</strong></p>` + orders.map(o => {
        const rs = state.recipients.filter(r => r.order === o);
        return `<div class="send-step"><span class="eyebrow">Step ${orders.indexOf(o) + 1}</span>${rs.map(r =>
          `<div class="row" style="gap:8px"><span class="rdot" style="--rc:${rColor(r.key)}"></span>${escapeHtml(r.name)} <span class="muted small">${escapeHtml(r.email)} · ${r.kind === 'cc' ? 'gets a copy when complete' : state.fields.filter(f => f.recipient === r.key).length + ' fields'}</span></div>`).join('')}</div>`;
      }).join('') + `<p class="muted small">${C.emailEnabled ? '' : 'Email isn\'t set up, so you\'ll copy each signing link from the next page.'}</p>`;
      $('#send-modal').hidden = false; $('#send-confirm').focus();
    });
    $('#send-cancel').addEventListener('click', () => { $('#send-modal').hidden = true; });
    $('#send-confirm').addEventListener('click', async () => {
      $('#send-confirm').disabled = true; status('Sending…');
      try { const d = await post({ send: true }); state.dirty = false; location.href = d.redirect; }
      catch (err) { $('#send-modal').hidden = true; $('#send-confirm').disabled = false; status(err.message, true); }
    });
    $('#btn-template').addEventListener('click', () => { $('#tpl-name').value = $('#ed-title').value.replace(/^Please sign:\s*/i, ''); $('#tpl-modal').hidden = false; $('#tpl-name').focus(); });
    $('#tpl-cancel').addEventListener('click', () => { $('#tpl-modal').hidden = true; });
    $('#tpl-confirm').addEventListener('click', async () => {
      const name = $('#tpl-name').value.trim(); if (!name) return;
      try { const d = await post({ template_name: name }); setDirty(false); status(d.message); } catch (err) { status(err.message, true); }
      $('#tpl-modal').hidden = true;
    });
  }

  function escapeHtml(s) { return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]); }

  renderRecipients(); renderRecipientSelects(); renderDocList(); renderPages(); setDirty(false);
})();
