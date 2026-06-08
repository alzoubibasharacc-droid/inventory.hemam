/* Inventory counting page — extracted from count.html for browser caching.
   Requires window.COUNT_CONFIG to be set by the template before this file loads. */
(function () {
  'use strict';

  /* ── State ──────────────────────────────────────────────────────────────── */
  const cfg = window.COUNT_CONFIG || {};
  const STATE = {
    selectedItem: null,
    branchId:     cfg.branchId     ?? null,
    countedItems: cfg.countedItems ?? 0,
    totalItems:   cfg.totalItems   ?? 0,
    isAdmin:      cfg.isAdmin      ?? false,
    isManager:    cfg.isManager    ?? false,
    userId:       cfg.userId       ?? null,
  };

  /* ── Element refs ───────────────────────────────────────────────────────── */
  const $ = id => document.getElementById(id);
  const searchInput    = $('searchInput');
  const searchResults  = $('searchResults');
  const searchClear    = $('searchClear');
  const searchSection  = $('searchSection');
  const entrySection   = $('entrySection');
  const entryItemName  = $('entryItemName');
  const entryItemNote  = $('entryItemNote');
  const entryItemDept  = $('entryItemDept');
  const entryItemCode  = $('entryItemCode');
  const entryAlready   = $('entryAlreadyCounted');
  const entryQty       = $('entryQty');          // hidden input
  const entryQtyDisplay = $('entryQtyDisplay');  // stepper display
  const qtyStepper     = $('qtyStepper');
  const qtyBtnPlus     = $('qtyBtnPlus');
  const qtyBtnMinus    = $('qtyBtnMinus');
  const entryUnit      = $('entryUnit');
  const entryNotes     = $('entryNotes');
  const saveBtn        = $('saveBtn');
  const cancelBtn      = $('cancelBtn');
  const recentList     = $('recentList');
  const recentCard     = $('recentCard');
  const emptyState     = $('emptyState');
  const progressBar    = $('progressBar');
  const progressText   = $('progressText');
  const progressPct    = $('progressPct');

  if (!searchInput) return;  // no-branch state for non-admin users

  /* ── Stepper ────────────────────────────────────────────────────────────── */
  const np = { display: '0', isFirstKey: true, hasDecimal: false };

  function stepperSetValue(val) {
    const n = Math.max(0, val);
    const display = n === Math.floor(n) ? String(n) : String(n);
    entryQty.value = display;
    entryQtyDisplay.textContent = display === '0' ? '0' : display;
    entryQtyDisplay.classList.toggle('has-value', parseFloat(display) > 0);
    qtyStepper.classList.remove('is-invalid');
  }

  function stepperGetValue() { return parseFloat(entryQty.value) || 0; }

  let holdTimer = null, holdInterval = null;

  function startHold(delta) {
    holdTimer = setTimeout(() => {
      holdInterval = setInterval(() => stepperSetValue(stepperGetValue() + delta), 65);
    }, 360);
  }
  function clearHold() { clearTimeout(holdTimer); clearInterval(holdInterval); }

  qtyBtnPlus.addEventListener('click',       () => stepperSetValue(stepperGetValue() + 1));
  qtyBtnMinus.addEventListener('click',      () => stepperSetValue(Math.max(0, stepperGetValue() - 1)));
  qtyBtnPlus.addEventListener('pointerdown', () => startHold(1));
  qtyBtnMinus.addEventListener('pointerdown',() => startHold(-1));
  ['pointerup','pointercancel','pointerleave'].forEach(ev => {
    qtyBtnPlus.addEventListener(ev,  clearHold);
    qtyBtnMinus.addEventListener(ev, clearHold);
  });

  entryQtyDisplay.addEventListener('click', openNumpad);

  /* ── Numpad ─────────────────────────────────────────────────────────────── */
  const npOverlay = $('cntNumpadOverlay');
  const npSheet   = $('cntNumpadSheet');
  const npDisplay = $('cntNpDisplay');
  const npClose   = $('cntNpClose');
  const npConfirm = $('cntNpConfirm');

  function openNumpad() {
    const cur = stepperGetValue();
    np.display    = cur > 0 ? fmtNum(cur) : '0';
    np.isFirstKey = true;
    np.hasDecimal = np.display.includes('.');
    npDisplay.textContent = np.display;
    npOverlay.classList.add('cnt-np-open');
    npSheet.classList.add('cnt-np-open');
  }

  function closeNumpad() {
    npOverlay.classList.remove('cnt-np-open');
    npSheet.classList.remove('cnt-np-open');
  }

  function numpadPress(key) {
    if (key === 'del') {
      if (np.display.length > 1) {
        if (np.display.slice(-1) === '.') np.hasDecimal = false;
        np.display = np.display.slice(0, -1);
      } else {
        np.display = '0'; np.isFirstKey = true; np.hasDecimal = false;
      }
    } else if (key === '.') {
      if (!np.hasDecimal) {
        np.display    = np.isFirstKey ? '0.' : np.display + '.';
        np.hasDecimal = true;
        np.isFirstKey = false;
      }
    } else {
      if (np.isFirstKey) {
        np.display    = key;
        np.isFirstKey = false;
      } else {
        if (np.display.length < 10) np.display += key;
      }
    }
    npDisplay.textContent = np.display;
  }

  function confirmNumpad() {
    const val = parseFloat(np.display) || 0;
    stepperSetValue(val);
    closeNumpad();
  }

  npOverlay.addEventListener('click', closeNumpad);
  npClose.addEventListener('click',   closeNumpad);
  npConfirm.addEventListener('click', confirmNumpad);

  document.querySelectorAll('#cntNumpadSheet .cnt-np-btn').forEach(btn => {
    btn.addEventListener('click', () => numpadPress(btn.dataset.k));
  });

  /* ── Search ─────────────────────────────────────────────────────────────── */
  let searchTimer;

  searchInput.addEventListener('input', function () {
    const q = this.value.trim();
    searchClear.style.display = q ? 'block' : 'none';
    clearTimeout(searchTimer);
    if (!q) { hideResults(); return; }
    searchTimer = setTimeout(() => doSearch(q), 220);
  });

  searchClear.addEventListener('click', function () {
    searchInput.value = '';
    searchClear.style.display = 'none';
    hideResults();
    searchInput.focus();
  });

  searchInput.addEventListener('keydown', function (e) {
    if (e.key === 'ArrowDown') {
      const first = searchResults.querySelector('.result-item');
      if (first) { first.focus(); e.preventDefault(); }
    }
    if (e.key === 'Escape') hideResults();
    if (e.key === 'Enter') {
      const first = searchResults.querySelector('.result-item');
      if (first) { first.click(); e.preventDefault(); }
    }
  });

  document.addEventListener('click', function (e) {
    if (!e.target.closest('.search-wrapper')) hideResults();
  });

  async function doSearch(q) {
    const params = new URLSearchParams({
      q,
      branch_id: STATE.branchId || '',
    });
    try {
      const res   = await fetch(`/inventory/count/search?${params}`);
      const items = await res.json();
      renderResults(items, q);
    } catch (_) {
      renderError();
    }
  }

  function renderResults(items, q) {
    searchResults.innerHTML = '';
    if (!items.length) {
      searchResults.innerHTML =
        `<div class="result-empty"><i class="bi bi-inbox me-2"></i>لا توجد نتائج لـ "${escHtml(q)}"</div>`;
      showResults();
      return;
    }
    items.forEach(item => {
      const div = document.createElement('div');
      div.className = 'result-item';
      div.setAttribute('tabindex', '0');
      div.setAttribute('role', 'button');

      const codeBadge = item.item_code
        ? `<span style="font-size:0.7rem;font-family:monospace;color:#888;margin-right:6px;">${escHtml(item.item_code)}</span>`
        : '';
      const countedBadge = item.already_counted
        ? `<span class="result-counted-badge">✓ مجرود</span>`
        : '';
      const noteHtml = item.packaging_note
        ? `<div class="result-note"><i class="bi bi-tag me-1"></i>${escHtml(item.packaging_note)}</div>`
        : '';

      div.innerHTML = `
        <div class="d-flex align-items-center gap-2">
          <div class="flex-grow-1 min-w-0">
            <div class="result-name">
              ${escHtml(item.name)} ${codeBadge}${countedBadge}
            </div>
            ${noteHtml}
            <div class="result-dept">${escHtml(item.dept)}</div>
          </div>
          <i class="bi bi-chevron-left text-muted" style="font-size:0.85rem;flex-shrink:0;"></i>
        </div>
      `;
      div.addEventListener('click',   () => selectItem(item));
      div.addEventListener('keydown', e => {
        if (e.key === 'Enter' || e.key === ' ') { selectItem(item); e.preventDefault(); }
        if (e.key === 'ArrowDown') { const n = div.nextSibling;     if (n) n.focus(); e.preventDefault(); }
        if (e.key === 'ArrowUp')   { const p = div.previousSibling; if (p) p.focus(); else searchInput.focus(); e.preventDefault(); }
      });
      searchResults.appendChild(div);
    });
    showResults();
  }

  function renderError() {
    searchResults.innerHTML =
      `<div class="result-empty text-danger"><i class="bi bi-wifi-off me-2"></i>خطأ في الاتصال</div>`;
    showResults();
  }

  function showResults() { searchResults.style.display = 'block'; }
  function hideResults()  { searchResults.style.display = 'none'; searchResults.innerHTML = ''; }

  /* ── Select item → show entry form ─────────────────────────────────────── */
  function selectItem(item) {
    STATE.selectedItem = item;
    hideResults();
    searchInput.value = '';
    searchClear.style.display = 'none';

    entryItemName.textContent = item.name;

    if (item.packaging_note) {
      entryItemNote.textContent   = item.packaging_note;
      entryItemNote.style.display = 'flex';
    } else {
      entryItemNote.style.display = 'none';
    }
    entryItemDept.textContent = item.dept;

    if (item.item_code) {
      entryItemCode.textContent   = item.item_code;
      entryItemCode.style.display = 'inline-block';
      entryItemCode.className     = 'item-code-badge';
    } else {
      entryItemCode.style.display = 'none';
    }

    entryAlready.style.display = item.already_counted ? 'inline-block' : 'none';

    entryUnit.innerHTML = '';
    (item.allowed_units || []).forEach(u => {
      const opt = document.createElement('option');
      opt.value       = u.id;
      opt.textContent = u.name;
      entryUnit.appendChild(opt);
    });

    stepperSetValue(0);
    entryNotes.value = '';
    qtyStepper.classList.remove('is-invalid');
    entryNotes.classList.remove('is-invalid');
    const errEl = $('entryError');
    if (errEl) errEl.style.display = 'none';

    searchSection.style.display = 'none';
    entrySection.style.display  = 'block';
  }

  /* ── Cancel → back to search ────────────────────────────────────────────── */
  cancelBtn.addEventListener('click', backToSearch);

  function backToSearch() {
    STATE.selectedItem = null;
    entrySection.style.display  = 'none';
    searchSection.style.display = 'block';
    setTimeout(() => searchInput.focus(), 80);
  }

  /* ── Save entry ─────────────────────────────────────────────────────────── */
  saveBtn.addEventListener('click', saveEntry);

  /* entryQty is now a hidden input — keyboard flow goes unit → notes */
  entryUnit.addEventListener('keydown',  e => { if (e.key === 'Enter') { entryNotes.focus(); e.preventDefault(); } });
  entryNotes.addEventListener('keydown', e => { if (e.key === 'Enter') { saveEntry(); e.preventDefault(); } });

  async function saveEntry() {
    if (!STATE.selectedItem) return;

    const qty = entryQty.value;
    if (!qty || isNaN(parseFloat(qty)) || parseFloat(qty) <= 0) {
      qtyStepper.classList.add('is-invalid');
      openNumpad();
      return;
    }
    qtyStepper.classList.remove('is-invalid');

    const notes = entryNotes.value.trim();
    if (!notes) {
      entryNotes.classList.add('is-invalid');
      entryNotes.focus();
      showEntryError('الملاحظة مطلوبة');
      return;
    }
    entryNotes.classList.remove('is-invalid');

    saveBtn.disabled  = true;
    saveBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>جاري الحفظ...';

    try {
      const res = await fetch('/inventory/count/entry', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          item_id: STATE.selectedItem.id,
          qty:     parseFloat(qty),
          unit_id: parseInt(entryUnit.value),
          notes:   notes,
        }),
      });
      const data = await res.json();

      if (data.ok) {
        showToast(`${data.item_name} — ${fmtNum(data.entered_qty)} ${data.entered_unit}`, 'success');
        prependRecentEntry(data);
        if (data.entry_count === 1) {
          STATE.countedItems++;
          renderProgress();
        }
        backToSearch();
      } else {
        showEntryError(data.error || 'خطأ في الحفظ');
      }
    } catch (_) {
      showEntryError('خطأ في الاتصال — تحقق من الشبكة');
    } finally {
      saveBtn.disabled  = false;
      saveBtn.innerHTML = '<i class="bi bi-check-lg me-2"></i>حفظ الإدخال';
    }
  }

  function showEntryError(msg) {
    const el = $('entryError');
    if (!el) return;
    el.textContent   = msg;
    el.style.display = 'block';
    setTimeout(() => { el.style.display = 'none'; }, 4000);
  }

  /* ── Prepend to recent list ─────────────────────────────────────────────── */
  function prependRecentEntry(data) {
    if (emptyState) emptyState.style.display = 'none';
    if (recentCard) recentCard.style.display = 'block';

    const div = document.createElement('div');
    div.className = 'recent-entry';
    div.id = `recent-${data.entry_id}`;
    div.dataset.entryId       = data.entry_id;
    div.dataset.enteredQty    = data.entered_qty;
    div.dataset.enteredUnitId = data.entered_unit_id || '';
    div.dataset.notes         = data.notes || '';
    div.dataset.allowedUnits  = JSON.stringify(data.allowed_units || []);

    const editBtn = `<button class="btn-edit-entry" title="تعديل"><i class="bi bi-pencil-fill"></i></button>`;
    const delBtn  = STATE.isAdmin
      ? `<button class="btn-delete-entry" title="حذف"><i class="bi bi-x-lg"></i></button>`
      : '';

    const notesHtml = data.notes
      ? `<span class="text-muted d-block text-truncate entry-notes-meta" style="max-width:220px;">· ${escHtml(data.notes)}</span>`
      : '';

    div.innerHTML = `
      <div class="entry-display d-flex align-items-center gap-2">
        <div class="flex-grow-1 min-w-0">
          <div class="recent-item-name text-truncate">${escHtml(data.item_name)}</div>
          <div class="recent-meta">
            <span class="recent-qty">${fmtNum(data.entered_qty)} ${escHtml(data.entered_unit)}</span>
            <span class="text-muted">· ${escHtml(data.time)}</span>
            ${notesHtml}
          </div>
        </div>
        <div class="d-flex gap-1">${editBtn}${delBtn}</div>
      </div>
    `;

    div.querySelector('.btn-edit-entry').addEventListener('click', () => showEditForm(div));
    div.querySelector('.btn-delete-entry')?.addEventListener('click', () => deleteEntry(data.entry_id, div));

    recentList.prepend(div);
  }

  /* ── Edit entry (inline) ────────────────────────────────────────────────── */
  function showEditForm(card) {
    const display = card.querySelector('.entry-display');
    let editForm  = card.querySelector('.entry-edit-form');

    if (!editForm) {
      const enteredQty    = parseFloat(card.dataset.enteredQty) || 0;
      const enteredUnitId = parseInt(card.dataset.enteredUnitId) || 0;
      const notes         = card.dataset.notes || '';
      const allowedUnits  = JSON.parse(card.dataset.allowedUnits || '[]');

      const unitOpts = allowedUnits.map(u =>
        `<option value="${u.id}" ${u.id === enteredUnitId ? 'selected' : ''}>${escHtml(u.name)}</option>`
      ).join('');

      editForm = document.createElement('div');
      editForm.className = 'entry-edit-form';
      editForm.innerHTML = `
        <div class="d-flex gap-2 mb-2 flex-wrap">
          <input type="number" class="form-control edit-qty" value="${enteredQty}"
                 step="any" min="0"
                 style="max-width:110px; font-size:1.1rem; font-weight:700; color:var(--brand-green);">
          <select class="form-select edit-unit" style="flex:1; min-width:100px;">${unitOpts}</select>
        </div>
        <input type="text" class="form-control edit-notes mb-2"
               value="${escHtml(notes)}" placeholder="ملاحظة *">
        <div class="d-flex gap-2">
          <button class="btn btn-success btn-sm flex-grow-1 edit-save-btn">
            <i class="bi bi-check-lg me-1"></i>حفظ التعديل
          </button>
          <button class="btn btn-outline-secondary btn-sm edit-cancel-btn" style="min-width:44px;">
            <i class="bi bi-x-lg"></i>
          </button>
        </div>
        <div class="edit-error text-danger small mt-1" style="display:none;"></div>
      `;
      card.appendChild(editForm);

      editForm.querySelector('.edit-cancel-btn').addEventListener('click', () => {
        editForm.style.display = 'none';
        display.style.display  = '';
      });
      editForm.querySelector('.edit-save-btn').addEventListener('click', () => {
        saveEditEntry(card, editForm);
      });
      editForm.querySelector('.edit-notes').addEventListener('keydown', e => {
        if (e.key === 'Enter') { saveEditEntry(card, editForm); e.preventDefault(); }
      });
    }

    display.style.display = 'none';
    editForm.style.display = 'block';
    editForm.querySelector('.edit-qty').select();
  }

  async function saveEditEntry(card, editForm) {
    const entryId = card.dataset.entryId;
    const qtyVal  = editForm.querySelector('.edit-qty').value.trim();
    const unitId  = editForm.querySelector('.edit-unit').value;
    const notes   = editForm.querySelector('.edit-notes').value.trim();
    const errEl   = editForm.querySelector('.edit-error');
    const saveBtnEl = editForm.querySelector('.edit-save-btn');

    errEl.style.display = 'none';

    if (!notes) {
      errEl.textContent   = 'الملاحظة مطلوبة';
      errEl.style.display = 'block';
      editForm.querySelector('.edit-notes').focus();
      return;
    }
    const qty = parseFloat(qtyVal);
    if (!qtyVal || isNaN(qty) || qty <= 0) {
      errEl.textContent   = 'الكمية غير صالحة';
      errEl.style.display = 'block';
      editForm.querySelector('.edit-qty').focus();
      return;
    }

    saveBtnEl.disabled  = true;
    saveBtnEl.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';

    try {
      const res  = await fetch(`/inventory/count/edit-entry/${entryId}`, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ qty, unit_id: parseInt(unitId), notes }),
      });
      const data = await res.json();

      if (data.ok) {
        card.dataset.enteredQty    = data.entered_qty;
        card.dataset.enteredUnitId = unitId;
        card.dataset.notes         = data.notes;

        const display  = card.querySelector('.entry-display');
        display.querySelector('.recent-qty').textContent =
          `${fmtNum(data.entered_qty)} ${data.entered_unit}`;

        const notesSpan = display.querySelector('.entry-notes-meta');
        if (data.notes) {
          if (notesSpan) {
            notesSpan.textContent = `· ${data.notes}`;
          } else {
            const meta = display.querySelector('.recent-meta');
            const span = document.createElement('span');
            span.className = 'text-muted d-block text-truncate entry-notes-meta';
            span.style.maxWidth = '220px';
            span.textContent = `· ${data.notes}`;
            meta.appendChild(span);
          }
        } else if (notesSpan) {
          notesSpan.remove();
        }

        editForm.style.display = 'none';
        display.style.display  = '';
        showToast(`تم تعديل ${data.item_name}`, 'info');
      } else {
        errEl.textContent   = data.error || 'خطأ في الحفظ';
        errEl.style.display = 'block';
      }
    } catch (_) {
      errEl.textContent   = 'خطأ في الاتصال';
      errEl.style.display = 'block';
    } finally {
      saveBtnEl.disabled  = false;
      saveBtnEl.innerHTML = '<i class="bi bi-check-lg me-1"></i>حفظ التعديل';
    }
  }

  /* ── Delete entry ───────────────────────────────────────────────────────── */
  async function deleteEntry(entryId, cardEl) {
    if (!confirm('حذف هذا الإدخال؟')) return;
    try {
      const res  = await fetch(`/inventory/count/delete-entry/${entryId}`, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
      });
      const data = await res.json();
      if (data.ok) {
        cardEl.style.opacity    = '0';
        cardEl.style.transition = 'opacity .2s';
        setTimeout(() => cardEl.remove(), 200);
        showToast('تم حذف الإدخال', 'info');
      } else {
        showToast(data.error || 'خطأ في الحذف', 'danger');
      }
    } catch (_) {
      showToast('خطأ في الحذف', 'danger');
    }
  }

  /* Wire up buttons on server-rendered entries */
  document.querySelectorAll('.recent-entry[data-entry-id]').forEach(card => {
    card.querySelector('.btn-edit-entry')?.addEventListener('click', () => showEditForm(card));
    card.querySelector('.btn-delete-entry')?.addEventListener('click', function () {
      deleteEntry(this.dataset.entryId, card);
    });
  });

  /* ── Progress bar ───────────────────────────────────────────────────────── */
  function renderProgress() {
    const pct = STATE.totalItems > 0
      ? Math.round(STATE.countedItems / STATE.totalItems * 100)
      : 0;
    if (progressBar) {
      progressBar.style.width = `${pct}%`;
      progressBar.setAttribute('aria-valuenow', STATE.countedItems);
    }
    if (progressText) {
      progressText.innerHTML =
        `تم جرد <strong>${STATE.countedItems}</strong> من <strong>${STATE.totalItems}</strong> صنف`;
    }
    if (progressPct) progressPct.textContent = `${pct}%`;
  }

  /* ── Toast notifications ────────────────────────────────────────────────── */
  function showToast(msg, type) {
    const container = $('toastContainer');
    if (!container) return;
    const div = document.createElement('div');
    div.className = `count-toast${type === 'danger' ? ' danger' : type === 'info' ? ' info' : ''}`;
    div.textContent = msg;
    container.appendChild(div);
    setTimeout(() => {
      div.classList.add('fade-out');
      setTimeout(() => div.remove(), 260);
    }, 2400);
  }

  /* ── Helpers ────────────────────────────────────────────────────────────── */
  function fmtNum(n) {
    const f = parseFloat(n);
    if (isNaN(f)) return String(n);
    return f === Math.floor(f) ? f.toFixed(0) : f.toFixed(3).replace(/\.?0+$/, '');
  }

  function escHtml(str) {
    return String(str ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

})();
