/**
 * guided_count.js — Production guided inventory counting module
 *
 * Architecture
 * ────────────
 * Single IIFE, single mutable State object, component functions that do
 * targeted DOM mutations (no virtual DOM, no full re-renders on every key).
 *
 * State layers
 *   S.values      — current local values (changes on every stepper/numpad input)
 *   S.savedValues — last values committed to the server
 *   S.countedSet  — item IDs that have at least one server-confirmed count row
 *
 * Dirty detection: getStatus() compares S.values vs S.savedValues per unit.
 *
 * Performance
 *   • renderUnits()     — full re-render only on product change
 *   • patchUnitValues() — surgical DOM update on every stepper press
 *   • renderProductMap()— single DocumentFragment, one paint
 *   • updateMapDot()    — only touches changed dot class lists
 */
(function () {
  'use strict';

  /* ── Config ───────────────────────────────────────────────────────────── */
  const CFG = window.GC_CONFIG || {};
  const API_ITEMS = '/inventory/count/guided/items';
  const API_SAVE  = '/inventory/count/guided/save';

  /* ── State ────────────────────────────────────────────────────────────── */
  const S = {
    items:       [],
    session:     null,
    branchId:    CFG.branchId ?? null,
    currentIdx:  0,
    isSaving:    false,
    fastMode:    false,

    values:      {},   // { [itemId]: { [unitId]: number } }
    savedValues: {},   // { [itemId]: { [unitId]: number } }
    countedSet:  new Set(),
  };

  /* ── Numpad state ─────────────────────────────────────────────────────── */
  const NP = { itemId: null, unitId: null, display: '0', isFirstKey: true, hasDecimal: false };

  /* ── Hold-to-increment ────────────────────────────────────────────────── */
  const HOLD = { timer: null, interval: null };

  /* ── Element cache ────────────────────────────────────────────────────── */
  let E = {};

  /* ══════════════════════════════════════════════════════════════════════ */
  /*  INIT                                                                  */
  /* ══════════════════════════════════════════════════════════════════════ */
  function init() {
    if (!document.getElementById('gc-app')) return;

    E = {
      app:           document.getElementById('gc-app'),
      sessionName:   document.getElementById('gc-session-name'),
      counterBadge:  document.getElementById('gc-counter-badge'),
      progressBar:   document.getElementById('gc-progress-bar'),
      pct:           document.getElementById('gc-pct'),
      fastBtn:       document.getElementById('gc-fast-btn'),
      scroll:        document.getElementById('gc-scroll'),
      wrapper:       document.getElementById('gc-content-wrapper'),
      completeBanner:document.getElementById('gc-complete-banner'),
      statusBadge:   document.getElementById('gc-status-badge'),
      itemName:      document.getElementById('gc-item-name'),
      itemMeta:      document.getElementById('gc-item-meta'),
      packagingNote: document.getElementById('gc-packaging-note'),
      itemPosition:  document.getElementById('gc-item-position'),
      mapWrap:       document.getElementById('gc-map-wrap'),
      mapTrack:      document.getElementById('gc-map-track'),
      units:         document.getElementById('gc-units'),
      btnPrev:       document.getElementById('gc-btn-prev'),
      btnSave:       document.getElementById('gc-btn-save'),
      btnNext:       document.getElementById('gc-btn-next'),
      npOverlay:     document.getElementById('gc-numpad-overlay'),
      npSheet:       document.getElementById('gc-numpad-sheet'),
      npUnit:        document.getElementById('gc-np-unit'),
      npDisplay:     document.getElementById('gc-np-display'),
      npLast:        document.getElementById('gc-np-last'),
      npClose:       document.getElementById('gc-np-close'),
      npConfirm:     document.getElementById('gc-np-confirm'),
      toast:         document.getElementById('gc-toast'),
    };

    _setAppHeight();
    _bindStaticEvents();
    loadItems();
  }

  /* ══════════════════════════════════════════════════════════════════════ */
  /*  LAYOUT                                                                */
  /* ══════════════════════════════════════════════════════════════════════ */
  function _setAppHeight() {
    const nav = document.querySelector('.navbar');
    E.app.style.height = `calc(100dvh - ${nav ? nav.offsetHeight : 0}px)`;
  }

  /* ══════════════════════════════════════════════════════════════════════ */
  /*  DATA                                                                  */
  /* ══════════════════════════════════════════════════════════════════════ */
  async function loadItems() {
    const params = new URLSearchParams({ branch_id: S.branchId || '' });
    try {
      const res  = await fetch(`${API_ITEMS}?${params}`);
      const data = await res.json();
      if (!data.ok) { showToast(data.error || 'خطأ في التحميل', 'danger'); return; }

      S.session = data.session;
      S.items   = data.items;

      // Seed state from last-saved server values
      for (const item of S.items) {
        S.values[item.id]      = {};
        S.savedValues[item.id] = {};
        for (const u of item.units) {
          const q = u.last_qty != null ? u.last_qty : 0;
          S.values[item.id][u.unit_id]      = q;
          S.savedValues[item.id][u.unit_id] = q;
        }
        if (item.is_counted) S.countedSet.add(item.id);
      }

      renderAll(0);
    } catch (err) {
      showToast('خطأ في الاتصال — تعذّر التحميل', 'danger');
    }
  }

  /* ══════════════════════════════════════════════════════════════════════ */
  /*  STATUS HELPERS                                                        */
  /* ══════════════════════════════════════════════════════════════════════ */
  function getStatus(itemId) {
    const cur    = S.values[itemId]      || {};
    const saved  = S.savedValues[itemId] || {};
    const item   = S.items.find(i => i.id === itemId);
    if (!item) return 'not_counted';
    for (const u of item.units) {
      if ((cur[u.unit_id] || 0) !== (saved[u.unit_id] || 0)) return 'modified';
    }
    return S.countedSet.has(itemId) ? 'counted' : 'not_counted';
  }

  function countedCount() {
    return S.items.filter(i => S.countedSet.has(i.id)).length;
  }

  function _nextUncounted(from) {
    const n = S.items.length;
    for (let i = 1; i < n; i++) {
      const idx = (from + i) % n;
      if (!S.countedSet.has(S.items[idx].id)) return idx;
    }
    return -1;
  }

  /* ══════════════════════════════════════════════════════════════════════ */
  /*  FULL RENDER (called once on load + on navigate)                      */
  /* ══════════════════════════════════════════════════════════════════════ */
  function renderAll(animDir) {
    if (!S.items.length) return;
    renderProgress();
    renderProductCard(animDir);
    renderProductMap();
  }

  /* ══════════════════════════════════════════════════════════════════════ */
  /*  PROGRESS                                                              */
  /* ══════════════════════════════════════════════════════════════════════ */
  function renderProgress() {
    const n   = S.items.length;
    const cnt = countedCount();
    const pct = n ? Math.round(cnt / n * 100) : 0;
    E.counterBadge.textContent = `${cnt} / ${n}`;
    E.progressBar.style.width  = `${pct}%`;
    E.pct.textContent          = `${pct}%`;
    if (S.session) E.sessionName.textContent = S.session.name || '';
  }

  /* ══════════════════════════════════════════════════════════════════════ */
  /*  PRODUCT CARD                                                          */
  /* ══════════════════════════════════════════════════════════════════════ */
  function renderProductCard(animDir) {
    const item   = S.items[S.currentIdx];
    const status = getStatus(item.id);

    // Status badge
    const labels = { not_counted: 'لم يُجرد', counted: 'مجرود', modified: 'معدَّل' };
    E.statusBadge.textContent = labels[status];
    E.statusBadge.className   = `gc-badge-${status}`;
    E.statusBadge.id          = 'gc-status-badge';

    // Name
    E.itemName.textContent = item.name;

    // Meta chips
    const chips = [];
    if (item.sku)      chips.push(`<span class="gc-chip">${esc(item.sku)}</span>`);
    if (item.category) chips.push(`<span class="gc-chip">${esc(item.category)}</span>`);
    if (item.dept)     chips.push(`<span class="gc-chip">${esc(item.dept)}</span>`);
    E.itemMeta.innerHTML = chips.join('');

    // Packaging note
    if (item.packaging_note) {
      E.packagingNote.textContent    = item.packaging_note;
      E.packagingNote.style.display  = 'flex';
    } else {
      E.packagingNote.style.display  = 'none';
    }

    // Position
    E.itemPosition.textContent = `${S.currentIdx + 1} من ${S.items.length}`;

    // Nav button states
    E.btnPrev.disabled = S.currentIdx === 0;
    E.btnNext.disabled = S.currentIdx === S.items.length - 1;
    _updateSaveBtn();

    // Units
    renderUnits(item);

    // Completion banner
    const allDone = S.items.length > 0 && S.items.every(i => S.countedSet.has(i.id));
    E.completeBanner.classList.toggle('d-none', !allDone);

    // Slide animation
    if (animDir !== 0) {
      E.wrapper.classList.remove('gc-anim-right', 'gc-anim-left');
      void E.wrapper.offsetWidth; // force reflow to restart animation
      E.wrapper.classList.add(animDir > 0 ? 'gc-anim-left' : 'gc-anim-right');
    }

    E.scroll.scrollTop = 0;
  }

  /* ══════════════════════════════════════════════════════════════════════ */
  /*  UNIT STEPPER CARDS — full render                                     */
  /* ══════════════════════════════════════════════════════════════════════ */
  function renderUnits(item) {
    const vals  = S.values[item.id]      || {};
    const saved = S.savedValues[item.id] || {};

    const html = item.units.map(u => {
      const cur     = vals[u.unit_id]  || 0;
      const sv      = saved[u.unit_id] || 0;
      const dirty   = cur !== sv;
      const lastStr = _buildLastInfo(u);
      return `
        <div class="gc-unit-card${dirty ? ' gc-unit-modified' : ''}">
          <div class="gc-unit-name">${esc(u.name)}</div>
          <div class="gc-stepper">
            <button class="gc-step-btn"
                    data-uid="${u.unit_id}" data-action="minus">−</button>
            <div class="gc-step-val${dirty ? ' gc-val-amber' : ''}"
                 data-uid="${u.unit_id}" data-item="${item.id}">${fmt(cur)}</div>
            <button class="gc-step-btn"
                    data-uid="${u.unit_id}" data-action="plus">+</button>
          </div>
          ${lastStr ? `<div class="gc-last-info">${lastStr}</div>` : ''}
        </div>`;
    }).join('');

    E.units.innerHTML = html;
    _bindUnitEvents(item.id);
  }

  function _buildLastInfo(unit) {
    if (unit.last_qty == null) return '';
    const t = unit.last_at ? ` · ${fmtTime(unit.last_at)}` : '';
    return `آخر جرد: ${esc(fmt(unit.last_qty))} ${esc(unit.name)}${t}`;
  }

  /* Surgical update — no re-render, only touches changed values */
  function patchUnits(itemId) {
    const item = S.items.find(i => i.id === itemId);
    if (!item || S.items[S.currentIdx].id !== itemId) return;

    const vals  = S.values[itemId]      || {};
    const saved = S.savedValues[itemId] || {};

    E.units.querySelectorAll('.gc-step-val').forEach(el => {
      const uid   = parseInt(el.dataset.uid);
      const cur   = vals[uid]  || 0;
      const dirty = cur !== (saved[uid] || 0);

      el.textContent = fmt(cur);
      el.classList.toggle('gc-val-amber', dirty);

      const card    = el.closest('.gc-unit-card');
      card.classList.toggle('gc-unit-modified', dirty);
    });

    // Refresh status badge
    const status = getStatus(itemId);
    const labels = { not_counted: 'لم يُجرد', counted: 'مجرود', modified: 'معدَّل' };
    E.statusBadge.textContent = labels[status];
    E.statusBadge.className   = `gc-badge-${status}`;
    E.statusBadge.id          = 'gc-status-badge';
  }

  /* ══════════════════════════════════════════════════════════════════════ */
  /*  PRODUCT MAP STRIP                                                     */
  /* ══════════════════════════════════════════════════════════════════════ */
  function renderProductMap() {
    const frag = document.createDocumentFragment();
    S.items.forEach((item, idx) => {
      const dot = document.createElement('span');
      dot.className = 'gc-dot';
      dot.title = item.name;
      _setDotClass(dot, item.id, idx);
      dot.addEventListener('click', () => navigateTo(idx));
      frag.appendChild(dot);
    });
    E.mapTrack.innerHTML = '';
    E.mapTrack.appendChild(frag);
  }

  function updateMapDots() {
    E.mapTrack.querySelectorAll('.gc-dot').forEach((dot, idx) => {
      const item = S.items[idx];
      if (item) _setDotClass(dot, item.id, idx);
    });
    _scrollMapToCurrent();
  }

  function _setDotClass(dot, itemId, idx) {
    const status = getStatus(itemId);
    dot.className = 'gc-dot';
    if (status === 'counted')       dot.classList.add('gc-dot-counted');
    else if (status === 'modified') dot.classList.add('gc-dot-modified');
    if (idx === S.currentIdx)       dot.classList.add('gc-dot-current');
  }

  function _scrollMapToCurrent() {
    const dots = E.mapTrack.children;
    if (!dots.length || S.currentIdx >= dots.length) return;
    const dot    = dots[S.currentIdx];
    const wrapW  = E.mapWrap.offsetWidth;
    const target = dot.offsetLeft - wrapW / 2 + dot.offsetWidth / 2;
    E.mapWrap.scrollTo({ left: target, behavior: 'smooth' });
  }

  /* ══════════════════════════════════════════════════════════════════════ */
  /*  SAVE                                                                  */
  /* ══════════════════════════════════════════════════════════════════════ */
  async function saveCurrentItem(silent = false) {
    const item = S.items[S.currentIdx];
    if (!item || S.isSaving) return false;

    const vals    = S.values[item.id] || {};
    const entries = item.units
      .filter(u => (vals[u.unit_id] || 0) > 0)
      .map(u => ({ unit_id: u.unit_id, qty: vals[u.unit_id] }));

    S.isSaving = true;
    if (!silent) {
      E.btnSave.disabled     = true;
      E.btnSave.innerHTML    = '<span class="spinner-border spinner-border-sm"></span>';
    }

    try {
      const res  = await fetch(API_SAVE, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ item_id: item.id, session_id: S.session.id, entries }),
      });
      const data = await res.json();

      if (data.ok) {
        S.savedValues[item.id] = { ...vals };
        if (data.is_counted) S.countedSet.add(item.id);
        else                 S.countedSet.delete(item.id);

        renderProgress();
        patchUnits(item.id);
        updateMapDots();

        if (!silent) {
          _flashSaveOk();
          if (S.fastMode) {
            const next = _nextUncounted(S.currentIdx);
            setTimeout(() => {
              if (next === -1) showToast('تم جرد جميع الأصناف ✓', 'success');
              else             navigateTo(next, 1);
            }, 300);
          }
        }
        return true;
      } else {
        if (!silent) showToast(data.error || 'خطأ في الحفظ', 'danger');
        return false;
      }
    } catch (_) {
      if (!silent) showToast('خطأ في الاتصال', 'danger');
      return false;
    } finally {
      S.isSaving = false;
      if (!silent) {
        E.btnSave.disabled = false;
        _updateSaveBtn();
      }
    }
  }

  function _flashSaveOk() {
    E.btnSave.classList.add('gc-save-ok');
    E.btnSave.innerHTML = '<i class="bi bi-check2-all"></i>تم';
    setTimeout(() => {
      E.btnSave.classList.remove('gc-save-ok');
      _updateSaveBtn();
    }, 750);
  }

  function _updateSaveBtn() {
    if (S.isSaving) return;
    E.btnSave.innerHTML = S.fastMode
      ? '<i class="bi bi-check-lg"></i>حفظ ⟩'
      : '<i class="bi bi-check-lg"></i>حفظ';
    E.btnSave.classList.toggle('gc-save-fast', S.fastMode);
  }

  /* ══════════════════════════════════════════════════════════════════════ */
  /*  NAVIGATION                                                            */
  /* ══════════════════════════════════════════════════════════════════════ */
  async function navigateTo(newIdx, explicitDir) {
    if (newIdx < 0 || newIdx >= S.items.length) return;
    if (newIdx === S.currentIdx) return;

    const dir = explicitDir !== undefined ? explicitDir : (newIdx > S.currentIdx ? 1 : -1);

    // Auto-save dirty item silently before leaving
    if (getStatus(S.currentIdx) === 'modified') {
      await saveCurrentItem(true);
    }

    S.currentIdx = newIdx;
    renderProductCard(dir);
    updateMapDots();
  }

  /* ══════════════════════════════════════════════════════════════════════ */
  /*  STEPPER                                                               */
  /* ══════════════════════════════════════════════════════════════════════ */
  function _bindUnitEvents(itemId) {
    E.units.querySelectorAll('.gc-step-btn').forEach(btn => {
      const uid   = parseInt(btn.dataset.uid);
      const delta = btn.dataset.action === 'plus' ? 1 : -1;

      btn.addEventListener('click',        () => _step(itemId, uid, delta));
      btn.addEventListener('pointerdown',  () => _holdStart(itemId, uid, delta));
      btn.addEventListener('pointerup',    _holdStop);
      btn.addEventListener('pointercancel',_holdStop);
      btn.addEventListener('pointerleave', _holdStop);
    });

    E.units.querySelectorAll('.gc-step-val').forEach(el => {
      el.addEventListener('click', () => openNumpad(itemId, parseInt(el.dataset.uid)));
    });
  }

  function _step(itemId, unitId, delta) {
    if (!S.values[itemId]) S.values[itemId] = {};
    S.values[itemId][unitId] = Math.max(0, (S.values[itemId][unitId] || 0) + delta);
    patchUnits(itemId);
  }

  function _holdStart(itemId, uid, delta) {
    _holdStop();
    HOLD.timer = setTimeout(() => {
      HOLD.interval = setInterval(() => _step(itemId, uid, delta), 65);
    }, 360);
  }

  function _holdStop() {
    clearTimeout(HOLD.timer);
    clearInterval(HOLD.interval);
  }

  /* ══════════════════════════════════════════════════════════════════════ */
  /*  NUMPAD                                                                */
  /* ══════════════════════════════════════════════════════════════════════ */
  function openNumpad(itemId, unitId) {
    const item = S.items.find(i => i.id === itemId);
    const unit = item?.units.find(u => u.unit_id === unitId);
    const cur  = (S.values[itemId] || {})[unitId] || 0;

    NP.itemId     = itemId;
    NP.unitId     = unitId;
    NP.display    = cur > 0 ? fmt(cur) : '0';
    NP.isFirstKey = true;
    NP.hasDecimal = NP.display.includes('.');

    E.npUnit.textContent    = unit?.name || '';
    E.npDisplay.textContent = NP.display;

    if (unit?.last_qty != null) {
      const t = unit.last_at ? ` · ${fmtTime(unit.last_at)}` : '';
      E.npLast.textContent = `آخر جرد: ${fmt(unit.last_qty)} ${unit.name}${t}`;
    } else {
      E.npLast.textContent = '';
    }

    E.npOverlay.classList.add('gc-open');
    E.npSheet.classList.add('gc-open');
  }

  function closeNumpad() {
    E.npOverlay.classList.remove('gc-open');
    E.npSheet.classList.remove('gc-open');
  }

  function _npPress(key) {
    if (key === 'del') {
      if (NP.display.length > 1) {
        if (NP.display.slice(-1) === '.') NP.hasDecimal = false;
        NP.display = NP.display.slice(0, -1);
      } else {
        NP.display = '0'; NP.isFirstKey = true; NP.hasDecimal = false;
      }
    } else if (key === '.') {
      if (!NP.hasDecimal) {
        NP.display    = NP.isFirstKey ? '0.' : NP.display + '.';
        NP.hasDecimal = true;
        NP.isFirstKey = false;
      }
    } else {
      if (NP.isFirstKey) { NP.display = key; NP.isFirstKey = false; }
      else if (NP.display.length < 10) NP.display += key;
    }
    E.npDisplay.textContent = NP.display;
  }

  function _npConfirm() {
    const val = parseFloat(NP.display) || 0;
    if (!S.values[NP.itemId]) S.values[NP.itemId] = {};
    S.values[NP.itemId][NP.unitId] = val;
    patchUnits(NP.itemId);
    closeNumpad();
  }

  /* ══════════════════════════════════════════════════════════════════════ */
  /*  FAST MODE                                                             */
  /* ══════════════════════════════════════════════════════════════════════ */
  function toggleFastMode() {
    S.fastMode = !S.fastMode;
    E.fastBtn.classList.toggle('gc-fast-on', S.fastMode);
    _updateSaveBtn();
    showToast(S.fastMode ? '⚡ الوضع السريع نشط' : 'الوضع العادي', 'info');
  }

  /* ══════════════════════════════════════════════════════════════════════ */
  /*  EVENT BINDING                                                         */
  /* ══════════════════════════════════════════════════════════════════════ */
  function _bindStaticEvents() {
    // Navigation
    E.btnPrev.addEventListener('click', () => navigateTo(S.currentIdx - 1, -1));
    E.btnNext.addEventListener('click', () => navigateTo(S.currentIdx + 1,  1));
    E.btnSave.addEventListener('click', () => saveCurrentItem(false));
    E.fastBtn.addEventListener('click', toggleFastMode);

    // Numpad
    E.npOverlay.addEventListener('click', closeNumpad);
    E.npClose.addEventListener('click',   closeNumpad);
    E.npConfirm.addEventListener('click', _npConfirm);
    document.querySelectorAll('#gc-numpad-sheet .gc-np-btn').forEach(btn => {
      btn.addEventListener('click', () => _npPress(btn.dataset.k));
    });

    // Keyboard (+ scanner debounce for rapid Enter)
    let scanTimer = null;
    document.addEventListener('keydown', e => {
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;

      // Numpad intercepts all digits when open
      if (E.npSheet.classList.contains('gc-open')) {
        if (/^[0-9]$/.test(e.key)) { _npPress(e.key); return; }
        if (e.key === '.')          { _npPress('.'); return; }
        if (e.key === 'Backspace')  { _npPress('del'); return; }
        if (e.key === 'Enter')      { _npConfirm(); return; }
        if (e.key === 'Escape')     { closeNumpad(); return; }
        return;
      }

      switch (e.key) {
        case 'ArrowRight': case 'ArrowUp':
          navigateTo(S.currentIdx - 1, -1); e.preventDefault(); break;
        case 'ArrowLeft':  case 'ArrowDown':
          navigateTo(S.currentIdx + 1,  1); e.preventDefault(); break;
        case 'Enter': case ' ':
          // Debounce: barcode scanners fire Enter in rapid bursts (<30 ms apart)
          clearTimeout(scanTimer);
          scanTimer = setTimeout(() => saveCurrentItem(false), 50);
          e.preventDefault();
          break;
        case 'f': case 'F':
          toggleFastMode();
          break;
      }
    });

    // Swipe gesture on scroll container
    let tx = 0, ty = 0;
    E.scroll.addEventListener('touchstart', e => {
      tx = e.touches[0].clientX;
      ty = e.touches[0].clientY;
    }, { passive: true });
    E.scroll.addEventListener('touchend', e => {
      const dx = e.changedTouches[0].clientX - tx;
      const dy = e.changedTouches[0].clientY - ty;
      if (Math.abs(dx) > 55 && Math.abs(dx) > Math.abs(dy) * 1.5) {
        if (dx > 0) navigateTo(S.currentIdx - 1, -1);
        else        navigateTo(S.currentIdx + 1,  1);
      }
    }, { passive: true });

    // Resize
    window.addEventListener('resize', _setAppHeight, { passive: true });
  }

  /* ══════════════════════════════════════════════════════════════════════ */
  /*  TOAST                                                                 */
  /* ══════════════════════════════════════════════════════════════════════ */
  function showToast(msg, type = 'success') {
    const div = document.createElement('div');
    div.className = `gc-toast-item${type === 'danger' ? ' danger' : type === 'info' ? ' info' : ''}`;
    div.textContent = msg;
    E.toast.appendChild(div);
    setTimeout(() => {
      div.classList.add('gc-toast-out');
      setTimeout(() => div.remove(), 240);
    }, 2200);
  }

  /* ══════════════════════════════════════════════════════════════════════ */
  /*  HELPERS                                                               */
  /* ══════════════════════════════════════════════════════════════════════ */
  function fmt(n) {
    const f = parseFloat(n);
    if (isNaN(f)) return '0';
    return f === Math.floor(f) ? String(Math.floor(f)) : String(f);
  }

  function fmtTime(iso) {
    if (!iso) return '';
    try {
      return new Date(iso).toLocaleTimeString('ar-SA', {
        hour: '2-digit', minute: '2-digit', hour12: false,
      });
    } catch (_) { return ''; }
  }

  function esc(s) {
    return String(s ?? '')
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  /* ── Bootstrap ─────────────────────────────────────────────────────── */
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

})();
