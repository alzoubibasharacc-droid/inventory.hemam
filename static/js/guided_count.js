/* ============================================================
   Guided Count — mobile product-by-product inventory input
   ============================================================ */

const GC = (() => {

    /* ── State ────────────────────────────────────────────── */
    let items      = [];
    let session    = null;
    let branchId   = null;
    let currentIdx = 0;
    let isSaving   = false;
    let fastMode   = false;

    // {item_id: {unit_id: qty}} — API-loaded baseline for change detection
    const initialValues = {};
    // {item_id: {unit_id: qty}} — live stepper values (user edits)
    const localValues   = {};
    // item_ids that have at least one saved non-zero count in this session
    const countedSet    = new Set();

    /* ── Numpad state ─────────────────────────────────────── */
    const np = {
        itemId:      null,
        unitId:      null,
        display:     '0',
        isFirstKey:  true,   // next digit press replaces whole display
        hasDecimal:  false,
    };

    let toastTimer = null;
    let holdTimer  = null;
    let holdInterval = null;
    let fastAdvanceTimer = null;

    /* ── DOM ──────────────────────────────────────────────── */
    const el = id => document.getElementById(id);

    /* ── Public: init ─────────────────────────────────────── */
    function init(sessionId, bid) {
        session  = { id: sessionId };
        branchId = bid;

        el('gc-btn-prev').addEventListener('click', () => navigate(-1));
        el('gc-btn-next').addEventListener('click', () => navigate(1));
        el('gc-btn-save').addEventListener('click', () => saveCurrentItem(false));
        el('gc-fast-toggle').addEventListener('click', toggleFastMode);

        _bindNumpad();
        _bindKeyboard();
        _bindSwipe();

        _setAppHeight();
        window.addEventListener('resize', _setAppHeight, { passive: true });

        loadItems();
    }

    /* ── Viewport height ──────────────────────────────────── */
    function _setAppHeight() {
        const navH = document.querySelector('.navbar')?.offsetHeight || 56;
        const app  = el('gc-app');
        if (app) app.style.height = `calc(100dvh - ${navH}px)`;
    }

    /* ── Data loading ─────────────────────────────────────── */
    async function loadItems() {
        try {
            const res  = await fetch(`/inventory/count/guided/items?branch_id=${branchId}`);
            const data = await res.json();

            if (!data.ok) { _showError(data.error || 'خطأ في تحميل البيانات'); return; }

            session = data.session;
            items   = data.items;

            if (!items.length) { _showError('لا توجد أصناف مرتبطة بهذه الجلسة.'); return; }

            items.forEach(item => {
                const vals = {};
                item.units.forEach(u => { vals[u.unit_id] = u.current_value || 0; });
                initialValues[item.id] = { ...vals };
                localValues[item.id]   = { ...vals };
                if (item.is_counted) countedSet.add(item.id);
            });

            _showApp();
            renderProduct(0);
            renderProductMap();
            updateProgress();

        } catch (err) {
            _showError('تعذّر الاتصال بالخادم: ' + err.message);
        }
    }

    /* ── Item status ──────────────────────────────────────── */
    function getStatus(itemId) {
        const item = items.find(i => i.id === itemId);
        if (!item) return 'not_counted';
        const locals = localValues[itemId]   || {};
        const inits  = initialValues[itemId] || {};
        const changed = item.units.some(u => (locals[u.unit_id] ?? 0) !== (inits[u.unit_id] ?? 0));
        if (changed) return 'modified';
        return countedSet.has(itemId) ? 'counted' : 'not_counted';
    }

    /* ── Render: product card ─────────────────────────────── */
    function renderProduct(animDir) {
        if (!items.length) return;
        const item   = items[currentIdx];
        const status = getStatus(item.id);

        el('gc-product-name').textContent = item.name;
        el('gc-dept').textContent         = item.dept || '—';
        el('gc-item-counter').textContent = `الصنف ${currentIdx + 1} من ${items.length}`;
        el('gc-item-counter-header').textContent = `${currentIdx + 1}/${items.length}`;

        const skuRow = el('gc-sku-row');
        if (item.sku) { el('gc-sku').textContent = item.sku; skuRow.classList.remove('d-none'); }
        else skuRow.classList.add('d-none');

        const catRow = el('gc-cat-row');
        if (item.category) { el('gc-category').textContent = item.category; catRow.classList.remove('d-none'); }
        else catRow.classList.add('d-none');

        const noteEl = el('gc-packaging-note');
        if (item.packaging_note) { noteEl.textContent = item.packaging_note; noteEl.classList.remove('d-none'); }
        else noteEl.classList.add('d-none');

        _updateStatusBadge(status);
        renderUnits(item);
        updateProductMap();

        el('gc-scroll').scrollTop = 0;
        el('gc-btn-prev').disabled = currentIdx === 0;
        el('gc-btn-next').disabled = currentIdx === items.length - 1;

        // Slide animation
        if (animDir !== 0) {
            const wrapper = el('gc-content-wrapper');
            wrapper.classList.remove('gc-anim-right', 'gc-anim-left');
            // Force reflow so re-adding the class triggers animation
            void wrapper.offsetWidth;
            wrapper.classList.add(animDir > 0 ? 'gc-anim-left' : 'gc-anim-right');
        }
    }

    function _updateStatusBadge(status) {
        const badge = el('gc-status-badge');
        const map = {
            not_counted: { cls: 'gc-badge-gray',   text: 'لم يُجرد' },
            counted:     { cls: 'gc-badge-green',  text: 'تم الجرد' },
            modified:    { cls: 'gc-badge-orange', text: 'معلّق'    },
        };
        const cfg = map[status] || map.not_counted;
        badge.className = `gc-status-badge ${cfg.cls}`;
        badge.textContent = cfg.text;
    }

    /* ── Render: unit cards ───────────────────────────────── */
    function renderUnits(item) {
        const container = el('gc-units');
        container.innerHTML = '';
        const locals = localValues[item.id]   || {};
        const inits  = initialValues[item.id] || {};

        item.units.forEach((unit, idx) => {
            const val     = locals[unit.unit_id] ?? 0;
            const initVal = inits[unit.unit_id]  ?? 0;
            const changed = val !== initVal;

            const lastHtml = (unit.last_qty !== null && unit.last_qty !== undefined)
                ? `آخر قيمة: <strong>${_fmt(unit.last_qty)}</strong> · ${_esc(unit.last_at || '')}`
                : `<span class="gc-unit-last-empty">لا توجد قيمة سابقة</span>`;

            const card = document.createElement('div');
            card.className = 'gc-unit-card' + (idx === 0 ? ' gc-unit-focus' : '');
            card.dataset.unitId = unit.unit_id;
            card.innerHTML = `
                <div class="gc-unit-row">
                    <div class="gc-unit-info">
                        <span class="gc-unit-name">${_esc(unit.name)}</span>
                        <span class="gc-unit-last">${lastHtml}</span>
                    </div>
                    <div class="gc-stepper" role="group" aria-label="كمية ${_esc(unit.name)}">
                        <button class="gc-step-btn gc-step-minus"
                                data-item="${item.id}" data-unit="${unit.unit_id}"
                                aria-label="تقليل">−</button>
                        <div class="gc-step-divider"></div>
                        <span class="gc-step-val${changed ? ' gc-val-changed' : ''}"
                              id="gcv-${item.id}-${unit.unit_id}"
                              data-item="${item.id}" data-unit="${unit.unit_id}"
                              role="button" tabindex="0"
                              aria-label="إدخال ${_esc(unit.name)} مباشر"
                              aria-live="polite">${_fmt(val)}</span>
                        <div class="gc-step-divider"></div>
                        <button class="gc-step-btn gc-step-plus"
                                data-item="${item.id}" data-unit="${unit.unit_id}"
                                aria-label="زيادة">+</button>
                    </div>
                </div>`;
            container.appendChild(card);
        });

        _bindSteppers(container, item.id);
    }

    /* ── Stepper: click, hold, numpad tap ─────────────────── */
    function _bindSteppers(container, itemId) {
        container.addEventListener('click', e => {
            // Tap the value display → open numpad
            const valSpan = e.target.closest('.gc-step-val');
            if (valSpan) {
                openNumpad(parseInt(valSpan.dataset.item, 10), parseInt(valSpan.dataset.unit, 10));
                return;
            }
            const btn = e.target.closest('.gc-step-btn');
            if (!btn) return;
            const uid   = parseInt(btn.dataset.unit, 10);
            const delta = btn.classList.contains('gc-step-plus') ? 1 : -1;
            step(itemId, uid, delta);
            _highlightCard(container, uid);
        });

        container.addEventListener('pointerdown', e => {
            const btn = e.target.closest('.gc-step-btn');
            if (!btn) return;
            btn.classList.add('gc-held');
            const uid   = parseInt(btn.dataset.unit, 10);
            const delta = btn.classList.contains('gc-step-plus') ? 1 : -1;
            holdTimer = setTimeout(() => {
                holdInterval = setInterval(() => step(itemId, uid, delta), 65);
            }, 360);
        });

        const stopHold = () => {
            container.querySelectorAll('.gc-held').forEach(b => b.classList.remove('gc-held'));
            clearTimeout(holdTimer);
            clearInterval(holdInterval);
            holdTimer = holdInterval = null;
        };
        container.addEventListener('pointerup',     stopHold);
        container.addEventListener('pointerleave',  stopHold);
        container.addEventListener('pointercancel', stopHold);
    }

    function _highlightCard(container, unitId) {
        container.querySelectorAll('.gc-unit-card').forEach(c => {
            c.classList.toggle('gc-unit-focus', parseInt(c.dataset.unitId, 10) === unitId);
        });
    }

    function step(itemId, unitId, delta) {
        if (!localValues[itemId]) localValues[itemId] = {};
        const newVal = Math.max(0, (localValues[itemId][unitId] ?? 0) + delta);
        localValues[itemId][unitId] = newVal;

        const valEl = document.getElementById(`gcv-${itemId}-${unitId}`);
        if (valEl) {
            valEl.textContent = _fmt(newVal);
            const initVal = (initialValues[itemId] || {})[unitId] ?? 0;
            valEl.classList.toggle('gc-val-changed', newVal !== initVal);
        }
        _updateStatusBadge(getStatus(itemId));
        updateProductMap();
    }

    /* ── Product map ──────────────────────────────────────── */
    function renderProductMap() {
        const mapEl = el('gc-map');
        mapEl.innerHTML = '';

        items.forEach((item, idx) => {
            const dot = document.createElement('button');
            dot.className = 'gc-map-dot';
            dot.dataset.idx = idx;
            dot.setAttribute('aria-label', `الصنف ${idx + 1}: ${item.name}`);
            mapEl.appendChild(dot);
        });

        mapEl.addEventListener('click', e => {
            const dot = e.target.closest('.gc-map-dot');
            if (!dot) return;
            const targetIdx = parseInt(dot.dataset.idx, 10);
            if (targetIdx !== currentIdx) navigateTo(targetIdx);
        });

        updateProductMap();
    }

    function updateProductMap() {
        const dots = el('gc-map')?.querySelectorAll('.gc-map-dot');
        if (!dots) return;

        dots.forEach((dot, idx) => {
            const status = getStatus(items[idx]?.id);
            dot.className = 'gc-map-dot';
            if (status === 'counted')     dot.classList.add('gc-dot-counted');
            else if (status === 'modified') dot.classList.add('gc-dot-modified');
            if (idx === currentIdx)       dot.classList.add('gc-dot-current');
        });

        // Scroll map to keep current dot visible
        const currentDot = el('gc-map')?.querySelector('.gc-dot-current');
        if (currentDot) {
            currentDot.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'center' });
        }
    }

    /* ── Numpad: open / close / input ─────────────────────── */
    function openNumpad(itemId, unitId) {
        const item = items.find(i => i.id === itemId);
        const unit = item?.units.find(u => u.unit_id === unitId);
        if (!unit) return;

        const cur = (localValues[itemId] || {})[unitId] ?? 0;
        np.itemId     = itemId;
        np.unitId     = unitId;
        np.display    = _fmt(cur);
        np.isFirstKey = true;
        np.hasDecimal = np.display.includes('.');

        el('gc-np-unit-name').textContent  = unit.name;
        el('gc-np-unit-label').textContent = unit.name;

        const initVal = (initialValues[itemId] || {})[unitId] ?? 0;
        el('gc-np-last').textContent = (unit.last_qty !== null && unit.last_qty !== undefined)
            ? `آخر قيمة: ${_fmt(unit.last_qty)} · ${unit.last_at || ''}  |  عند فتح الصفحة: ${_fmt(initVal)}`
            : `عند فتح الصفحة: ${_fmt(initVal)}`;

        _refreshNumpadDisplay();

        el('gc-numpad-overlay').classList.add('gc-open');
        el('gc-numpad-sheet').classList.add('gc-open');
    }

    function closeNumpad() {
        el('gc-numpad-overlay').classList.remove('gc-open');
        el('gc-numpad-sheet').classList.remove('gc-open');
        np.itemId = np.unitId = null;
    }

    function confirmNumpad() {
        if (np.itemId === null) return;
        const val = Math.max(0, parseFloat(np.display) || 0);
        _commitNumpadValue(val);
        closeNumpad();
    }

    function _commitNumpadValue(val) {
        const { itemId, unitId } = np;
        if (!localValues[itemId]) localValues[itemId] = {};
        localValues[itemId][unitId] = val;

        const valEl = document.getElementById(`gcv-${itemId}-${unitId}`);
        if (valEl) {
            valEl.textContent = _fmt(val);
            const initVal = (initialValues[itemId] || {})[unitId] ?? 0;
            valEl.classList.toggle('gc-val-changed', val !== initVal);
        }
        _updateStatusBadge(getStatus(itemId));
        updateProductMap();
    }

    function numpadPress(key) {
        if (key === '⌫') {
            if (np.isFirstKey) {
                np.display = '0'; np.isFirstKey = false;
            } else if (np.display.length > 1) {
                if (np.display.slice(-1) === '.') np.hasDecimal = false;
                np.display = np.display.slice(0, -1);
            } else {
                np.display = '0';
            }
        } else if (key === '.') {
            if (np.hasDecimal) return;
            if (np.isFirstKey) { np.display = '0.'; np.isFirstKey = false; }
            else np.display += '.';
            np.hasDecimal = true;
        } else {
            // digit
            if (np.isFirstKey) {
                np.display    = key;
                np.isFirstKey = false;
            } else if (np.display === '0') {
                np.display = key;
            } else if (np.display.length < 8) {
                np.display += key;
            }
        }
        _refreshNumpadDisplay();
    }

    function _refreshNumpadDisplay() {
        const dispEl = el('gc-np-display');
        dispEl.textContent = np.display;
        const initVal = np.itemId !== null
            ? ((initialValues[np.itemId] || {})[np.unitId] ?? 0)
            : 0;
        const cur = parseFloat(np.display) || 0;
        dispEl.classList.toggle('gc-np-changed', cur !== initVal);
    }

    function _bindNumpad() {
        // Grid key presses
        document.querySelector('.gc-numpad-grid')?.addEventListener('click', e => {
            const btn = e.target.closest('.gc-np-key');
            if (btn) numpadPress(btn.dataset.key);
        });
        el('gc-np-confirm')?.addEventListener('click', confirmNumpad);
        el('gc-np-close')?.addEventListener('click', closeNumpad);
        el('gc-numpad-overlay')?.addEventListener('click', closeNumpad);

        // Hardware keyboard while numpad open
        document.addEventListener('keydown', e => {
            if (!el('gc-numpad-sheet')?.classList.contains('gc-open')) return;
            if (e.key === 'Escape') { closeNumpad(); return; }
            if (e.key === 'Enter')  { confirmNumpad(); return; }
            if (e.key === 'Backspace') { numpadPress('⌫'); return; }
            if (e.key === '.')    { numpadPress('.'); return; }
            if (/^[0-9]$/.test(e.key)) numpadPress(e.key);
        });
    }

    /* ── Fast mode ────────────────────────────────────────── */
    function toggleFastMode() {
        fastMode = !fastMode;
        const btn = el('gc-fast-toggle');
        btn.classList.toggle('gc-fast-on', fastMode);
        btn.setAttribute('title', fastMode
            ? 'الوضع السريع مفعّل — يحفظ وينتقل تلقائياً'
            : 'الوضع السريع — يحفظ وينتقل تلقائياً');

        const saveBtn = el('gc-btn-save');
        saveBtn.classList.toggle('gc-fast-active', fastMode);

        const label = el('gc-save-label');
        label.textContent = fastMode ? 'حفظ ⟩' : 'حفظ';

        showToast(fastMode ? '⚡ الوضع السريع مفعّل' : 'الوضع السريع متوقف', 'info');
    }

    function _findNextUncounted(fromIdx) {
        for (let i = fromIdx + 1; i < items.length; i++) {
            if (getStatus(items[i].id) !== 'counted') return i;
        }
        for (let i = 0; i < fromIdx; i++) {
            if (getStatus(items[i].id) !== 'counted') return i;
        }
        return -1;
    }

    /* ── Save ─────────────────────────────────────────────── */
    async function saveCurrentItem(silent = false) {
        if (isSaving || !items.length) return;

        const item  = items[currentIdx];
        const vals  = localValues[item.id] || {};
        const units = item.units.map(u => ({ unit_id: u.unit_id, qty: vals[u.unit_id] ?? 0 }));

        isSaving = true;
        const saveBtn = el('gc-btn-save');
        saveBtn.disabled = true;
        saveBtn.innerHTML = '<span class="spinner-border spinner-border-sm" role="status"></span>';

        try {
            const res  = await fetch('/inventory/count/guided/save', {
                method:  'POST',
                headers: { 'Content-Type': 'application/json' },
                body:    JSON.stringify({ item_id: item.id, session_id: session.id, units }),
            });
            const data = await res.json();

            if (!data.ok) {
                if (!silent) showToast(data.error || 'خطأ في الحفظ', 'error');
                return;
            }

            // Sync baseline → no longer "modified"
            Object.assign(initialValues[item.id], vals);
            const anySaved = Object.values(vals).some(v => v > 0);
            if (anySaved) countedSet.add(item.id);
            else          countedSet.delete(item.id);

            // Update last_qty labels in live data
            const savedAt = _nowTime();
            item.units.forEach(u => { u.last_qty = vals[u.unit_id] ?? 0; u.last_at = savedAt; });

            _updateStatusBadge(getStatus(item.id));
            updateProgress();
            updateProductMap();

            if (!silent) {
                saveBtn.classList.add('gc-saved');
                const label = fastMode ? 'تم · ينتقل…' : 'تم الحفظ ✓';
                saveBtn.innerHTML = `<i class="bi bi-check-lg"></i><span>${label}</span>`;
                if (!fastMode) showToast('تم الحفظ بنجاح', 'success');

                // Fast mode: auto-advance to next uncounted
                if (fastMode) {
                    const nextIdx = _findNextUncounted(currentIdx);
                    if (nextIdx !== -1) {
                        clearTimeout(fastAdvanceTimer);
                        fastAdvanceTimer = setTimeout(() => navigateTo(nextIdx), 320);
                    } else {
                        showToast('تم جرد جميع الأصناف! ✓', 'success');
                        el('gc-complete-banner').classList.remove('d-none');
                    }
                }

                setTimeout(() => {
                    saveBtn.classList.remove('gc-saved');
                    _restoreSaveBtn();
                }, fastMode ? 280 : 1100);
            }
        } catch {
            if (!silent) showToast('تعذّر الاتصال بالخادم', 'error');
        } finally {
            isSaving = false;
            saveBtn.disabled = false;
            if (!saveBtn.classList.contains('gc-saved')) _restoreSaveBtn();
        }
    }

    function _restoreSaveBtn() {
        const saveBtn = el('gc-btn-save');
        const label   = fastMode ? 'حفظ ⟩' : 'حفظ';
        saveBtn.innerHTML = `<i class="bi bi-check-lg"></i><span id="gc-save-label">${label}</span>`;
    }

    /* ── Navigation ───────────────────────────────────────── */
    async function navigate(dir) {
        const newIdx = currentIdx + dir;
        if (newIdx < 0 || newIdx >= items.length) return;
        await _doNavigate(newIdx, dir);
    }

    async function navigateTo(targetIdx) {
        if (targetIdx < 0 || targetIdx >= items.length || targetIdx === currentIdx) return;
        const dir = targetIdx > currentIdx ? 1 : -1;
        await _doNavigate(targetIdx, dir);
    }

    async function _doNavigate(newIdx, animDir) {
        // Auto-save unsaved changes before leaving
        if (getStatus(items[currentIdx].id) === 'modified') {
            await saveCurrentItem(true);
        }
        currentIdx = newIdx;
        renderProduct(animDir);
    }

    /* ── Progress ─────────────────────────────────────────── */
    function updateProgress() {
        const total   = items.length;
        const counted = items.filter(i => getStatus(i.id) === 'counted').length;
        const pct     = total ? Math.round(counted / total * 100) : 0;

        el('gc-progress-text').textContent = `تم جرد ${counted} من ${total} صنف`;
        el('gc-progress-pct').textContent  = `${pct}%`;
        el('gc-progress-bar').style.width  = `${pct}%`;
        el('gc-progress-bar').setAttribute('aria-valuenow', pct);

        // Show completion banner if all counted
        if (counted === total && total > 0) {
            el('gc-complete-banner').classList.remove('d-none');
        }
    }

    /* ── Keyboard shortcuts ───────────────────────────────── */
    function _bindKeyboard() {
        document.addEventListener('keydown', e => {
            // Skip if numpad is open (numpad has its own handler)
            if (el('gc-numpad-sheet')?.classList.contains('gc-open')) return;
            // Skip if typing in an input
            if (['INPUT', 'TEXTAREA'].includes(document.activeElement?.tagName)) return;

            if (e.key === 'ArrowRight' || e.key === 'ArrowUp')  navigate(-1);
            if (e.key === 'ArrowLeft'  || e.key === 'ArrowDown') navigate(1);
            if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                saveCurrentItem(false);
            }
            // F key = fast mode toggle
            if (e.key === 'f' || e.key === 'F') toggleFastMode();
        });
    }

    /* ── Swipe gesture ────────────────────────────────────── */
    function _bindSwipe() {
        const scrollEl = el('gc-scroll');
        let startX = 0, startY = 0;
        scrollEl.addEventListener('pointerdown', e => {
            startX = e.clientX; startY = e.clientY;
        }, { passive: true });
        scrollEl.addEventListener('pointerup', e => {
            const dx = e.clientX - startX;
            const dy = e.clientY - startY;
            if (Math.abs(dx) > 55 && Math.abs(dx) > Math.abs(dy) * 1.5) {
                navigate(dx < 0 ? 1 : -1);
            }
        }, { passive: true });
    }

    /* ── Toast ────────────────────────────────────────────── */
    function showToast(msg, type = 'info') {
        const t   = el('gc-toast');
        t.textContent = msg;
        t.className   = `gc-toast-${type} gc-toast-show`;
        t.id          = 'gc-toast';
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => t.classList.remove('gc-toast-show'), 2000);
    }

    /* ── Visibility ───────────────────────────────────────── */
    function _showApp() {
        el('gc-loading').classList.add('d-none');
        el('gc-error').classList.add('d-none');
        el('gc-app').classList.remove('d-none');
        _setAppHeight();
    }

    function _showError(msg) {
        el('gc-loading').classList.add('d-none');
        el('gc-app').classList.add('d-none');
        el('gc-error').classList.remove('d-none');
        el('gc-error-msg').textContent = msg;
    }

    /* ── Utilities ────────────────────────────────────────── */
    function _fmt(n) {
        return parseFloat((+n).toFixed(4)).toString();
    }

    function _esc(s) {
        return String(s ?? '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    function _nowTime() {
        const d  = new Date();
        const hh = String(d.getHours()).padStart(2, '0');
        const mm = String(d.getMinutes()).padStart(2, '0');
        const dd = String(d.getDate()).padStart(2, '0');
        const mo = String(d.getMonth() + 1).padStart(2, '0');
        return `${hh}:${mm} · ${dd}/${mo}`;
    }

    return { init, showToast };
})();
