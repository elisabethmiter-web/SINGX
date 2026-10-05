// Guided signing: render pages, highlight this signer's fields, adopt a signature, finish.
(function () {
  const S = window.SIGN;
  const $ = (s, el = document) => el.querySelector(s);
  const canvas = $('#sign-canvas');
  const st = {
    values: {},              // field id -> text / bool
    applied: new Set(),      // signature/initials fields the signer has clicked to apply
    adopted: null,           // {name, initials, sig, ini, kind}
    pendingField: null,
    started: false,
  };
  if (document.fonts && document.fonts.load) document.fonts.load('600 64px Caveat').catch(() => {});
  const esc = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  const pageSrc = (d, n) => S.pageUrl.replace('__DOC__', d) + n + '.png';
  const LABEL = { signature: 'Sign', initials: 'Initial', date_signed: 'Date', name: 'Name', text: 'Text', checkbox: '' };

  // ------------------------------------------------------------ render
  S.docs.forEach(d => {
    const head = document.createElement('div'); head.className = 'doc-head';
    head.innerHTML = `<span class="eyebrow">Document</span> <strong>${esc(d.name)}</strong> <span class="muted small">${d.pages.length} page${d.pages.length > 1 ? 's' : ''}</span>`;
    canvas.appendChild(head);
    d.pages.forEach((p, n) => {
      const page = document.createElement('div');
      page.className = 'page'; page.dataset.doc = d.id; page.dataset.page = n;
      page.style.aspectRatio = `${p.w} / ${p.h}`;
      page.innerHTML = `<img src="${pageSrc(d.id, n)}" alt="${esc(d.name)}, page ${n + 1} of ${d.pages.length}" loading="${n < 2 ? 'eager' : 'lazy'}" draggable="false">`;
      canvas.appendChild(page);
    });
  });
  const pageOf = f => canvas.querySelector(`.page[data-doc="${f.doc}"][data-page="${f.page}"]`);
  const place = (el, f) => { el.style.cssText += `left:${f.x * 100}%;top:${f.y * 100}%;width:${f.w * 100}%;height:${f.h * 100}%`; };

  // Other signers' completed fields (read only)
  S.others.forEach(f => {
    const pg = pageOf(f); if (!pg) return;
    const el = document.createElement('div'); el.className = 'sf sf-done-other'; place(el, f);
    if (f.img) el.innerHTML = `<img src="${f.img}" alt="Signed">`;
    else if (f.type === 'checkbox') el.innerHTML = f.checked ? checkSvg() : '';
    else el.innerHTML = `<span class="sf-val">${esc(f.text || '')}</span>`;
    pg.appendChild(el);
  });

  // My fields
  const order = S.fields.slice().sort((a, b) => {
    const da = S.docs.findIndex(d => d.id === a.doc), db = S.docs.findIndex(d => d.id === b.doc);
    return da - db || a.page - b.page || a.y - b.y || a.x - b.x;
  });
  order.forEach(f => {
    const pg = pageOf(f); if (!pg) return;
    const el = document.createElement('div');
    el.className = `sf sf-${f.type}` + (f.required ? ' req' : ''); el.dataset.id = f.id; place(el, f);
    if (f.type === 'text') {
      el.innerHTML = `<input type="text" aria-label="${esc(f.label || 'Text')}" placeholder="${esc(f.label || '')}" maxlength="500">`;
      const inp = el.querySelector('input');
      inp.addEventListener('input', () => { st.values[f.id] = inp.value; refresh(); });
      inp.style.fontSize = Math.max(10, Math.min(15, el.offsetHeight * 0.6)) + 'px';
    } else if (f.type === 'checkbox') {
      el.setAttribute('role', 'checkbox'); el.setAttribute('aria-checked', 'false'); el.tabIndex = 0;
      el.setAttribute('aria-label', f.label || 'Checkbox');
      const toggle = () => { st.values[f.id] = !st.values[f.id]; el.setAttribute('aria-checked', st.values[f.id]); el.innerHTML = st.values[f.id] ? checkSvg() : ''; refresh(); };
      el.addEventListener('click', toggle);
      el.addEventListener('keydown', e => { if (e.key === ' ' || e.key === 'Enter') { e.preventDefault(); toggle(); } });
    } else if (f.type === 'signature' || f.type === 'initials') {
      el.tabIndex = 0; el.setAttribute('role', 'button');
      el.setAttribute('aria-label', f.type === 'signature' ? 'Sign here' : 'Initial here');
      el.innerHTML = `<span class="sf-tag">${LABEL[f.type]}</span>`;
      const act = () => clickSign(f, el);
      el.addEventListener('click', act);
      el.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); act(); } });
    } else if (f.type === 'date_signed') {
      el.innerHTML = `<span class="sf-val">${esc(S.dateText)}</span>`;
    } else if (f.type === 'name') {
      el.innerHTML = `<span class="sf-val">${esc(S.name)}</span>`;
    }
    if (f.label && f.type !== 'text') el.title = f.label;
    pg.appendChild(el);
    if (f.type === 'text') requestAnimationFrame(() => { el.querySelector('input').style.fontSize = Math.max(10, Math.min(15, el.offsetHeight * 0.62)) + 'px'; });
  });

  function checkSvg() { return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/></svg>'; }

  // ------------------------------------------------------------ progress + navigation
  function isDone(f) {
    if (f.type === 'signature' || f.type === 'initials') return st.applied.has(f.id);
    if (f.type === 'text') return !f.required || !!(st.values[f.id] || '').trim();
    if (f.type === 'checkbox') return !f.required || st.values[f.id] === true;
    return true;
  }
  const actionable = order.filter(f => ['signature', 'initials', 'text', 'checkbox'].includes(f.type));
  const required = actionable.filter(f => f.required || f.type === 'signature' || f.type === 'initials');
  function refresh() {
    const done = required.filter(isDone).length;
    order.forEach(f => { const el = canvas.querySelector(`.sf[data-id="${f.id}"]`); if (el) el.classList.toggle('done', isDone(f) && actionable.includes(f)); });
    $('#sb-progress').textContent = required.length ? `${done} of ${required.length} required field${required.length > 1 ? 's' : ''} complete` : '';
    const all = done === required.length;
    $('#btn-finish').hidden = !all;
    $('#btn-next').hidden = all;
    $('#sb-title').textContent = all ? 'All set. Select Finish to sign.' : 'Review and complete the highlighted fields';
  }
  $('#btn-next').addEventListener('click', () => {
    const next = required.find(f => !isDone(f)) || actionable.find(f => !isDone(f));
    if (!next) return;
    st.started = true; $('#btn-next').textContent = 'Next';
    const el = canvas.querySelector(`.sf[data-id="${next.id}"]`);
    el.scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'center' });
    canvas.querySelectorAll('.sf.pulse').forEach(x => x.classList.remove('pulse'));
    el.classList.add('pulse');
    const inp = el.querySelector('input'); (inp || el).focus({ preventScroll: true });
  });
  $('#jump-top').addEventListener('click', e => { e.preventDefault(); window.scrollTo({ top: 0 }); });

  // ------------------------------------------------------------ signature fields
  function applyTo(f, el) {
    const img = f.type === 'signature' ? st.adopted.sig : st.adopted.ini;
    el.innerHTML = `<img src="${img}" alt="${f.type === 'signature' ? 'Your signature' : 'Your initials'}">`;
    st.applied.add(f.id);
    refresh();
  }
  function clickSign(f, el) {
    if (st.applied.has(f.id)) { st.applied.delete(f.id); el.innerHTML = `<span class="sf-tag">${LABEL[f.type]}</span>`; refresh(); return; }
    if (!st.adopted) { st.pendingField = [f, el]; openAdopt(); return; }
    applyTo(f, el);
  }

  // ------------------------------------------------------------ adopt modal
  const modal = $('#adopt-modal');
  const nameIn = $('#ad-name'), iniIn = $('#ad-initials');
  let mode = 'type';
  const initialsOf = n => n.trim().split(/\s+/).filter(Boolean).map(w => w[0].toUpperCase()).slice(0, 3).join('');
  function openAdopt() {
    nameIn.value = nameIn.value || S.name; iniIn.value = iniIn.value || initialsOf(S.name);
    paintTyped(); $('#ad-error').textContent = '';
    modal.hidden = false; nameIn.focus();
    if (mode === 'draw') pads.forEach(p => p.resize());
  }
  function closeAdopt() { modal.hidden = true; st.pendingField = null; }
  function paintTyped() {
    $('#pv-sig').textContent = nameIn.value.trim() || 'Your name';
    $('#pv-ini').textContent = iniIn.value.trim() || 'AB';
  }
  nameIn.addEventListener('input', () => { iniIn.value = initialsOf(nameIn.value); paintTyped(); });
  iniIn.addEventListener('input', paintTyped);
  document.querySelectorAll('#adopt-modal .sig-tabs button').forEach(b => b.addEventListener('click', () => {
    mode = b.dataset.mode;
    document.querySelectorAll('#adopt-modal .sig-tabs button').forEach(x => x.setAttribute('aria-selected', x === b));
    $('#ad-type').hidden = mode !== 'type'; $('#ad-draw').hidden = mode !== 'draw';
    if (mode === 'draw') pads.forEach(p => p.resize());
  }));
  $('#ad-cancel').addEventListener('click', closeAdopt);
  modal.addEventListener('keydown', e => { if (e.key === 'Escape') closeAdopt(); });

  function makePad(cv) {
    const ctx = cv.getContext('2d'); let ink = false, drawing = false, last = null;
    const pad = {
      get ink() { return ink; },
      resize() { const r = cv.getBoundingClientRect(); if (!r.width) return; const d = devicePixelRatio || 1;
        cv.width = r.width * d; cv.height = r.height * d; ctx.setTransform(d, 0, 0, d, 0, 0);
        ctx.lineWidth = 2.4; ctx.lineCap = 'round'; ctx.lineJoin = 'round'; ctx.strokeStyle = '#1b2a5c'; ctx.fillStyle = '#1b2a5c'; ink = false; },
      clear() { ctx.clearRect(0, 0, cv.width, cv.height); ink = false; },
      png() { return ink ? trim(cv) : null; },
    };
    const pos = e => { const r = cv.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; };
    cv.addEventListener('pointerdown', e => { drawing = true; last = pos(e); cv.setPointerCapture(e.pointerId); ctx.beginPath(); ctx.arc(last.x, last.y, 1.1, 0, 7); ctx.fill(); ink = true; });
    cv.addEventListener('pointermove', e => { if (!drawing) return; const p = pos(e); ctx.beginPath(); ctx.moveTo(last.x, last.y); ctx.lineTo(p.x, p.y); ctx.stroke(); last = p; });
    ['pointerup', 'pointercancel'].forEach(t => cv.addEventListener(t, () => { drawing = false; }));
    return pad;
  }
  const pads = [makePad($('#pad-sig')), makePad($('#pad-ini'))];
  document.querySelectorAll('[data-clear]').forEach(b => b.addEventListener('click', () => pads[b.dataset.clear === 'sig' ? 0 : 1].clear()));

  function trim(src) {
    const w = src.width, h = src.height, d = src.getContext('2d').getImageData(0, 0, w, h).data;
    let x0 = w, y0 = h, x1 = 0, y1 = 0;
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) if (d[(y * w + x) * 4 + 3] > 10) {
      if (x < x0) x0 = x; if (x > x1) x1 = x; if (y < y0) y0 = y; if (y > y1) y1 = y; }
    if (x1 <= x0 || y1 <= y0) return null;
    const pad = 8, out = document.createElement('canvas');
    out.width = x1 - x0 + pad * 2; out.height = y1 - y0 + pad * 2;
    out.getContext('2d').drawImage(src, x0, y0, x1 - x0, y1 - y0, pad, pad, x1 - x0, y1 - y0);
    return out.toDataURL('image/png');
  }
  function typed(text, size) {
    const c = document.createElement('canvas'), x = c.getContext('2d');
    const font = `600 ${size}px Caveat, "Segoe Script", "Brush Script MT", cursive`;
    x.font = font; c.width = Math.ceil(x.measureText(text).width) + 40; c.height = Math.ceil(size * 1.7);
    x.font = font; x.fillStyle = '#1b2a5c'; x.textBaseline = 'middle'; x.fillText(text, 20, c.height / 2);
    return trim(c);
  }
  $('#ad-confirm').addEventListener('click', () => {
    const name = nameIn.value.trim(), ini = iniIn.value.trim();
    if (!name) { $('#ad-error').textContent = 'Enter your full name.'; return; }
    let sig, inipng;
    if (mode === 'draw') {
      sig = pads[0].png(); inipng = pads[1].png() || (ini ? typed(ini, 64) : null);
      if (!sig) { $('#ad-error').textContent = 'Draw your signature in the box.'; return; }
    } else {
      sig = typed(name, 72); inipng = typed(ini || initialsOf(name), 64);
    }
    st.adopted = { name, initials: ini || initialsOf(name), sig, ini: inipng, kind: mode };
    // Name fields reflect the adopted name
    order.filter(f => f.type === 'name').forEach(f => { const el = canvas.querySelector(`.sf[data-id="${f.id}"] .sf-val`); if (el) el.textContent = name; });
    const pending = st.pendingField; modal.hidden = true; st.pendingField = null;
    if (pending) applyTo(pending[0], pending[1]);
  });

  // ------------------------------------------------------------ decline
  $('#open-decline').addEventListener('click', () => { $('#decline-modal').hidden = false; $('#reason').focus(); document.querySelector('details.more').open = false; });
  $('#decline-cancel').addEventListener('click', () => { $('#decline-modal').hidden = true; });

  // ------------------------------------------------------------ finish
  $('#btn-finish').addEventListener('click', async () => {
    const values = {};
    order.forEach(f => { if (f.type === 'text') values[f.id] = st.values[f.id] || ''; if (f.type === 'checkbox') values[f.id] = st.values[f.id] === true; });
    const needsSig = order.some(f => f.type === 'signature'), needsIni = order.some(f => f.type === 'initials');
    const body = { values, adoptedName: st.adopted ? st.adopted.name : S.name, adoptedInitials: st.adopted ? st.adopted.initials : '',
      signature: needsSig && st.adopted ? st.adopted.sig : null, initials: needsIni && st.adopted ? st.adopted.ini : null,
      signatureKind: st.adopted ? st.adopted.kind : 'type' };
    $('#finish-modal').hidden = false; $('#btn-finish').disabled = true;
    try {
      const res = await fetch(S.finishUrl, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': window.CSRF }, body: JSON.stringify(body) });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || 'Something went wrong. Try again.');
      location.href = data.redirect;
    } catch (err) {
      $('#finish-modal').hidden = true; $('#btn-finish').disabled = false;
      $('#sb-title').textContent = err.message; $('#sb-title').style.color = 'var(--bad)';
    }
  });

  refresh();
})();
