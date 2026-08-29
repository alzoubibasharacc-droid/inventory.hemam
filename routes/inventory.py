from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, abort, Response
from flask_login import login_required, current_user
from models import db, Branch, Department, Item, ItemUnitConversion, InventoryCount, Unit, UnitConversion, InventorySession, SessionDepartment
from sqlalchemy import func, distinct
from sqlalchemy.orm import joinedload
from utils.decorators import get_scope
from utils.constants import now_jordan, resolve_session_for_branch, get_active_session

inventory_bp = Blueprint('inventory', __name__, url_prefix='/inventory')


# ── dashboard ─────────────────────────────────────────────────────────────────

@inventory_bp.route('/dashboard')
@login_required
def dashboard():
    now = now_jordan()

    if current_user.is_admin:
        branches = Branch.query.order_by(Branch.name_ar).all()
    elif current_user.branch_id:
        branches = [current_user.branch]
    else:
        branches = Branch.query.order_by(Branch.name_ar).all()

    _, scope_dept = get_scope()

    # Single query for all active sessions covering the relevant branches,
    # with session_departments and creator pre-loaded to avoid N+1 queries.
    branch_ids = [b.id for b in branches]
    active_sessions = (
        InventorySession.query
        .options(
            joinedload(InventorySession.session_departments),
            joinedload(InventorySession.creator),
        )
        .filter(
            InventorySession.branch_id.in_(branch_ids),
            InventorySession.status == 'active',
        )
        .all()
    ) if branch_ids else []
    sessions_by_branch = {s.branch_id: s for s in active_sessions}

    branch_stats = []
    for branch in branches:
        active_session    = sessions_by_branch.get(branch.id)
        session_dept_ids  = None

        if active_session and active_session.session_departments:
            session_dept_ids = {sd.department_id for sd in active_session.session_departments}

        if active_session:
            # Total: active items in the session's assigned departments
            items_q = (
                Item.query.join(Department)
                .filter(Department.branch_id == branch.id, Item.is_active == True)
            )
            if session_dept_ids:
                items_q = items_q.filter(Item.department_id.in_(session_dept_ids))
            if scope_dept:
                items_q = items_q.filter(Item.department_id == scope_dept)

            # Counted: distinct items in this session (same department scope)
            counts_q = (
                db.session.query(func.count(distinct(InventoryCount.item_id)))
                .join(Item, Item.id == InventoryCount.item_id)
                .filter(
                    InventoryCount.session_id == active_session.id,
                    InventoryCount.status == 'active',
                )
            )
            if session_dept_ids:
                counts_q = counts_q.filter(Item.department_id.in_(session_dept_ids))
            if scope_dept:
                counts_q = counts_q.filter(Item.department_id == scope_dept)

            total_items   = items_q.count()
            counted_items = counts_q.scalar() or 0
        else:
            total_items   = 0
            counted_items = 0

        branch_stats.append({
            'branch':           branch,
            'session':          active_session,
            'session_dept_ids': session_dept_ids,
            'total':            total_items,
            'counted':          counted_items,
            'percent':          int(counted_items / total_items * 100) if total_items else 0,
        })

    # Overall totals derive from session-scoped per-branch data — no separate query.
    total_items   = sum(s['total']   for s in branch_stats)
    total_counted = sum(s['counted'] for s in branch_stats)

    return render_template(
        'dashboard.html',
        branch_stats=branch_stats,
        total_items=total_items,
        total_counted=total_counted,
        now=now,
    )


# ── session task list (employee entry point) ──────────────────────────────────

@inventory_bp.route('/sessions')
@login_required
def session_list():
    """
    Employee landing page: shows session task cards scoped to the employee's
    branch and department. Managers are redirected to the admin session panel.
    """
    if current_user.is_manager:
        return redirect(url_for('admin.inv_sessions_list'))

    # Employee has no branch or dept assigned → show no-assignment state
    if not current_user.branch_id or not current_user.department_id:
        return render_template('inventory/sessions.html', task_cards=[], no_assignment=True)

    # Active and paused sessions for this branch where the employee's dept is included
    sessions = (
        InventorySession.query
        .join(SessionDepartment, SessionDepartment.session_id == InventorySession.id)
        .filter(
            InventorySession.branch_id == current_user.branch_id,
            InventorySession.status.in_(['active', 'paused']),
            SessionDepartment.department_id == current_user.department_id,
        )
        # active before paused ('a' < 'p' → ascending puts active first)
        .order_by(InventorySession.status, InventorySession.opened_at.desc())
        .all()
    )

    dept = Department.query.get(current_user.department_id)

    task_cards = []
    for session in sessions:
        total = (
            Item.query
            .filter(
                Item.department_id == current_user.department_id,
                Item.is_active == True,
            )
            .count()
        )
        counted = (
            db.session.query(func.count(distinct(InventoryCount.item_id)))
            .join(Item)
            .filter(
                InventoryCount.session_id == session.id,
                InventoryCount.status == 'active',
                Item.department_id == current_user.department_id,
            )
            .scalar() or 0
        )
        task_cards.append({
            'session': session,
            'dept':    dept,
            'total':   total,
            'counted': counted,
            'percent': int(counted / total * 100) if total else 0,
        })

    return render_template(
        'inventory/sessions.html',
        task_cards=task_cards,
        no_assignment=False,
    )


# ── count (main page, GET only) ───────────────────────────────────────────────

@inventory_bp.route('/count')
@login_required
def count():
    # Employees: block attempts to view another branch via URL
    if not current_user.is_manager and current_user.branch_id:
        req_branch = request.args.get('branch_id', type=int)
        if req_branch and req_branch != current_user.branch_id:
            flash('وصول غير مصرح به', 'danger')
            return redirect(url_for('inventory.dashboard'))

    branch_id = (
        request.args.get('branch_id', type=int)
        if current_user.is_admin or current_user.is_manager
        else current_user.branch_id
    )

    scope_branch, scope_dept = get_scope()

    if not current_user.is_manager and current_user.branch_id:
        branches = [current_user.branch]
    else:
        branches = Branch.query.all()

    # InventorySession is the sole source of truth — no cookie fallback.
    # resolve_session_for_branch falls back to the baseline when no active
    # session exists; baseline sessions are read-only archives, so we treat
    # them as absent and show a "no active session" state instead.
    inv_session = None
    if branch_id:
        try:
            resolved = resolve_session_for_branch(branch_id)
            if not resolved.is_baseline:
                inv_session = resolved
        except RuntimeError:
            pass

    # For employees: redirect if their department is not in this session's scope.
    if (inv_session
            and not current_user.is_manager
            and current_user.department_id
            and inv_session.session_departments):
        allowed_dept_ids = {sd.department_id for sd in inv_session.session_departments}
        if current_user.department_id not in allowed_dept_ids:
            flash('لا توجد جلسة جرد نشطة لقسمك حالياً', 'warning')
            return redirect(url_for('inventory.session_list'))

    count_date = inv_session.count_date.isoformat() if inv_session else ''

    total_items = counted_items = 0
    if branch_id and inv_session:
        items_q = (
            Item.query.join(Department)
            .filter(Department.branch_id == branch_id, Item.is_active == True)
        )

        # Restrict to session's assigned departments so the total denominator
        # matches the numerator and stays consistent with the Kanban card.
        sd_dept_ids = (
            [sd.department_id for sd in inv_session.session_departments]
            if inv_session.session_departments
            else None
        )

        if sd_dept_ids and not scope_dept:
            items_q = items_q.filter(Item.department_id.in_(sd_dept_ids))
        if scope_dept:
            items_q = items_q.filter(Item.department_id == scope_dept)

        counts_q = (
            db.session.query(func.count(distinct(InventoryCount.item_id)))
            .join(Item, Item.id == InventoryCount.item_id)
            .filter(
                InventoryCount.session_id == inv_session.id,
                InventoryCount.status == 'active',
            )
        )
        if sd_dept_ids and not scope_dept:
            counts_q = counts_q.filter(Item.department_id.in_(sd_dept_ids))
        if scope_dept:
            counts_q = counts_q.filter(Item.department_id == scope_dept)

        total_items   = items_q.count()
        counted_items = counts_q.scalar() or 0

    recent_entries = []
    if inv_session:
        recent_q = (
            InventoryCount.query.join(Item).join(Department)
            .filter(
                InventoryCount.session_id == inv_session.id,
                InventoryCount.status == 'active',
            )
        )
        if scope_dept:
            recent_q = recent_q.filter(Item.department_id == scope_dept)
        if not current_user.is_manager:
            recent_q = recent_q.filter(InventoryCount.user_id == current_user.id)
        recent_entries = recent_q.order_by(InventoryCount.created_at.desc()).limit(50).all()

    return render_template(
        'inventory/count.html',
        branches=branches,
        selected_branch=branch_id,
        total_items=total_items,
        counted_items=counted_items,
        recent_entries=recent_entries,
        count_date=count_date,
        inv_session=inv_session,
    )


# ── AJAX: search items ────────────────────────────────────────────────────────

@inventory_bp.route('/count/search')
@login_required
def count_search():
    q         = request.args.get('q', '').strip()
    branch_id = request.args.get('branch_id', type=int)
    dept_id   = request.args.get('dept_id',   type=int)

    if not (current_user.is_admin or current_user.is_manager):
        # Reject attempts to search outside the employee's assigned scope
        if current_user.branch_id and branch_id and branch_id != current_user.branch_id:
            return jsonify({'error': 'وصول غير مصرح به'}), 403
        if current_user.department_id and dept_id and dept_id != current_user.department_id:
            return jsonify({'error': 'وصول غير مصرح به'}), 403
        # Always enforce their assigned scope (ignore URL params)
        branch_id = current_user.branch_id
        dept_id   = current_user.department_id
    elif not current_user.is_admin and current_user.branch_id:
        branch_id = current_user.branch_id

    items_q = Item.query.join(Department).filter(Item.is_active == True)
    if branch_id:
        items_q = items_q.filter(Department.branch_id == branch_id)
    if dept_id:
        items_q = items_q.filter(Item.department_id == dept_id)
    if q:
        items_q = items_q.filter(Item.name_ar.ilike(f'%{q}%'))

    items = items_q.order_by(Item.name_ar).limit(20).all()

    # {item_id: total_active_quantity} — SUM, never a single "latest" row.
    # An item may have several independent active entries (different shelves/
    # locations); already_counted + total must reflect all of them combined.
    item_totals = {}
    if items:
        item_ids = [i.id for i in items]
        # Use get_active_session (status='active' only) rather than
        # resolve_session_for_branch, which falls back to the baseline session.
        # The baseline session holds all historical migrated counts; using it
        # here would mark every historically counted item as already_counted=True
        # in a brand-new official session, making progress appear complete
        # before any real counting has occurred.
        search_session = get_active_session(branch_id) if branch_id else None
        # Drop session if the employee's dept is not assigned to it
        if search_session and not current_user.is_manager and search_session.session_departments:
            allowed_dept_ids = {sd.department_id for sd in search_session.session_departments}
            if current_user.department_id and current_user.department_id not in allowed_dept_ids:
                search_session = None

        if search_session:
            rows = (
                db.session.query(
                    InventoryCount.item_id,
                    func.sum(InventoryCount.quantity),
                )
                .filter(
                    InventoryCount.item_id.in_(item_ids),
                    InventoryCount.session_id == search_session.id,
                    InventoryCount.status == 'active',
                )
                .group_by(InventoryCount.item_id)
                .all()
            )
            item_totals = {r[0]: (r[1] or 0.0) for r in rows}

    result = []
    for item in items:
        allowed_units = [
            {'id': c.unit_id, 'name': c.unit.name_ar}
            for c in item.conversions
        ]
        if not allowed_units:
            allowed_units = [{
                'id':   item.effective_base_unit_id,
                'name': item.effective_base_unit.name_ar,
            }]
        result.append({
            'id':              item.id,
            'name':            item.name_ar,
            'dept':            item.department.name_ar,
            'packaging_note':  item.packaging_note or '',
            'item_code':       item.item_code if current_user.is_manager else None,
            'already_counted': item.id in item_totals,
            'total_qty':       item_totals.get(item.id, 0.0),
            'allowed_units':   allowed_units,
            'base_unit':       item.effective_base_unit.name_ar,
        })

    return jsonify(result)


# ── AJAX: save a single count entry ──────────────────────────────────────────

@inventory_bp.route('/count/entry', methods=['POST'])
@login_required
def count_entry():
    data     = request.get_json(silent=True) or {}
    item_id  = data.get('item_id')
    qty_raw  = data.get('qty')
    unit_id  = data.get('unit_id')
    notes    = (data.get('notes') or '').strip()
    no_stock = bool(data.get('no_stock'))

    if not all([item_id, qty_raw is not None, unit_id]):
        return jsonify({'ok': False, 'error': 'بيانات ناقصة'}), 400

    if not notes:
        return jsonify({'ok': False, 'error': 'الملاحظة مطلوبة'}), 400

    try:
        entered_qty = float(qty_raw)
    except (ValueError, TypeError):
        return jsonify({'ok': False, 'error': 'الكمية غير صالحة'}), 400

    # "لا يوجد" is an explicit confirmation that the item was physically
    # checked and found empty — always saves as exactly 0 regardless of
    # whatever the (client-disabled) quantity field still holds. Without the
    # flag, behavior is unchanged: 0 and below are rejected.
    if no_stock:
        entered_qty = 0.0
    elif entered_qty <= 0:
        return jsonify({'ok': False, 'error': 'الكمية يجب أن تكون أكبر من صفر'}), 400

    item = Item.query.get(item_id)
    if not item:
        return jsonify({'ok': False, 'error': 'الصنف غير موجود'}), 404

    # Validate item is within the employee's assigned scope
    if not current_user.is_manager:
        if current_user.branch_id and item.department.branch_id != current_user.branch_id:
            return jsonify({'ok': False, 'error': 'وصول غير مصرح به'}), 403
        if current_user.department_id and item.department_id != current_user.department_id:
            return jsonify({'ok': False, 'error': 'وصول غير مصرح به'}), 403

    try:
        inv_session = resolve_session_for_branch(item.department.branch_id)
    except RuntimeError as e:
        return jsonify({'ok': False, 'error': str(e)}), 409

    # Baseline sessions are read-only historical archives — new counts must only
    # go into an active official session. resolve_session_for_branch falls back
    # to the baseline if no active session exists; block that path here so that
    # baseline data is never polluted with current-period counts.
    if inv_session.is_baseline:
        return jsonify({
            'ok':   False,
            'error': 'لا توجد جلسة جرد نشطة حالياً. تواصل مع المدير لتفعيل جلسة جرد.',
        }), 409

    # Non-admin users (employees + managers) may only count in active sessions.
    # Admins use the dedicated edit_count route for completed-session corrections.
    if not current_user.is_admin and inv_session.status != 'active':
        return jsonify({
            'ok':   False,
            'error': 'جلسة الجرد غير نشطة حالياً. تواصل مع المدير.',
        }), 409

    # Verify employee's department is included in this session's scope
    if not current_user.is_manager and inv_session.session_departments:
        allowed_dept_ids = {sd.department_id for sd in inv_session.session_departments}
        if current_user.department_id and current_user.department_id not in allowed_dept_ids:
            return jsonify({'ok': False, 'error': 'قسمك غير مشمول في جلسة الجرد النشطة'}), 403

    # count_date is authoritative from the session; month/year kept for backward compat
    count_date_obj = inv_session.count_date
    month          = count_date_obj.month
    year           = count_date_obj.year

    multiplier    = item.get_multiplier(int(unit_id))
    base_quantity = entered_qty * multiplier

    # Additive logic: the same item may be counted more than once in the same
    # session (e.g. found on two different shelves) — every submission creates
    # its own independent active record. Prior active entries for this item are
    # NEVER withdrawn or replaced here; the item's true total is always
    # SUM(quantity) over all active entries (computed below), not "the last one".
    entry = InventoryCount(
        item_id          = item.id,
        session_id       = inv_session.id,
        quantity         = base_quantity,
        entered_quantity = entered_qty,
        entered_unit_id  = int(unit_id),
        count_date       = count_date_obj,
        month            = month,
        year             = year,
        user_id          = current_user.id,
        notes            = notes or None,
        source           = 'manual',
    )
    db.session.add(entry)

    entry2      = None
    qty_raw_2   = data.get('qty_2')
    unit_id_2   = data.get('unit_id_2')
    entered_qty_2 = 0
    if qty_raw_2 is not None and unit_id_2 is not None:
        try:
            entered_qty_2 = float(qty_raw_2)
        except (ValueError, TypeError):
            entered_qty_2 = 0
        if entered_qty_2 > 0:
            multiplier_2    = item.get_multiplier(int(unit_id_2))
            entry2 = InventoryCount(
                item_id          = item.id,
                session_id       = inv_session.id,
                quantity         = entered_qty_2 * multiplier_2,
                entered_quantity = entered_qty_2,
                entered_unit_id  = int(unit_id_2),
                count_date       = count_date_obj,
                month            = month,
                year             = year,
                user_id          = current_user.id,
                notes            = notes or None,
                source           = 'manual',
            )
            db.session.add(entry2)

    db.session.commit()

    # Aggregate total for this item within the session — active entries only
    all_entries = InventoryCount.query.filter(
        InventoryCount.item_id    == item.id,
        InventoryCount.session_id == inv_session.id,
        InventoryCount.status     == 'active',
    ).all()
    total_base = sum(e.quantity for e in all_entries)

    entered_unit = Unit.query.get(int(unit_id))

    allowed_units = [{'id': c.unit_id, 'name': c.unit.name_ar} for c in item.conversions]
    if not allowed_units:
        allowed_units = [{'id': item.effective_base_unit_id, 'name': item.effective_base_unit.name_ar}]

    entry2_data = None
    if entry2:
        entered_unit_2 = Unit.query.get(int(unit_id_2))
        entry2_data = {
            'ok':              True,
            'entry_id':        entry2.id,
            'session_id':      inv_session.id,
            'session_name':    inv_session.name,
            'session_type':    inv_session.session_type,
            'total_base':      total_base,
            'base_unit':       item.effective_base_unit.name_ar,
            'entry_count':     len(all_entries),
            'entered_qty':     entered_qty_2,
            'entered_unit':    entered_unit_2.name_ar if entered_unit_2 else '',
            'entered_unit_id': int(unit_id_2),
            'item_name':       item.name_ar,
            'user_name':       current_user.full_name,
            'time':            entry2.created_at.strftime('%H:%M') if entry2.created_at else '',
            'notes':           notes,
            'allowed_units':   allowed_units,
        }

    return jsonify({
        'ok':              True,
        'entry_id':        entry.id,
        'session_id':      inv_session.id,
        'session_name':    inv_session.name,
        'session_type':    inv_session.session_type,
        'total_base':      total_base,
        'base_unit':       item.effective_base_unit.name_ar,
        'entry_count':     len(all_entries),
        'entered_qty':     entered_qty,
        'entered_unit':    entered_unit.name_ar if entered_unit else '',
        'entered_unit_id': int(unit_id),
        'item_name':       item.name_ar,
        'user_name':       current_user.full_name,
        'time':            entry.created_at.strftime('%H:%M') if entry.created_at else '',
        'notes':           notes,
        'allowed_units':   allowed_units,
        'entry_2':         entry2_data,
    })


# ── shared security gate for edit/withdraw on a single count record ──────────

def _authorize_entry_mutation(entry):
    """
    Never trust entry_id alone. Before an edit or withdrawal is applied,
    re-validate: ownership (or manager/admin override), branch/department
    scope, and that the owning session still accepts changes from this role.
    Returns a (json_response, status_code) tuple to abort with, or None if
    the action is allowed.
    """
    if entry.user_id != current_user.id and not current_user.is_manager:
        return jsonify({'ok': False, 'error': 'لا صلاحية'}), 403

    item = entry.item
    if not current_user.is_manager:
        if current_user.branch_id and item.department.branch_id != current_user.branch_id:
            return jsonify({'ok': False, 'error': 'وصول غير مصرح به'}), 403
        if current_user.department_id and item.department_id != current_user.department_id:
            return jsonify({'ok': False, 'error': 'وصول غير مصرح به'}), 403

    # Non-admins (employees + managers) may only mutate entries while their
    # session is active — mirrors the rule enforced on new-entry creation.
    # Admins retain the ability to correct records in any session state.
    inv_session = entry.session
    if not current_user.is_admin and inv_session and inv_session.status != 'active':
        return jsonify({
            'ok':   False,
            'error': 'جلسة الجرد غير نشطة حالياً. تواصل مع المدير.',
        }), 409

    return None


def _active_totals_for_item(item_id, session_id):
    """(total_quantity, entry_count) over ACTIVE entries only — never 'latest'."""
    active = InventoryCount.query.filter(
        InventoryCount.item_id    == item_id,
        InventoryCount.session_id == session_id,
        InventoryCount.status     == 'active',
    ).all()
    return sum(e.quantity for e in active), len(active)


# ── withdraw a single count entry ─────────────────────────────────────────────

@inventory_bp.route('/count/withdraw-entry/<int:entry_id>', methods=['POST'])
@login_required
def withdraw_entry(entry_id):
    """
    Soft-withdraws exactly the record identified by entry_id. Never touches
    any other record for the item — withdrawn rows are kept (status='withdrawn')
    for audit, never hard-deleted.
    """
    entry = InventoryCount.query.get_or_404(entry_id)

    denied = _authorize_entry_mutation(entry)
    if denied:
        response, code = denied
        if request.is_json:
            return response, code
        flash(response.get_json().get('error', 'لا صلاحية'), 'danger')
        return redirect(url_for('inventory.count'))

    if entry.status == 'withdrawn':
        msg = 'هذا الإدخال مسحوب بالفعل'
        if request.is_json:
            return jsonify({'ok': False, 'error': msg}), 422
        flash(msg, 'warning')
        return redirect(url_for('inventory.count'))

    item             = entry.item
    dept             = item.department
    entry_session_id = entry.session_id

    entry.status     = 'withdrawn'
    entry.updated_at = now_jordan()
    db.session.commit()

    if request.is_json:
        total_base, entry_count = _active_totals_for_item(item.id, entry_session_id)
        entry_session = entry.session
        return jsonify({
            'ok':           True,
            'total_base':   total_base,
            'entry_count':  entry_count,
            'session_id':   entry_session_id,
            'session_name': entry_session.name         if entry_session else None,
            'session_type': entry_session.session_type if entry_session else None,
        })

    flash('تم سحب الإدخال', 'info')
    return redirect(url_for('inventory.count', branch_id=dept.branch_id))


# ── edit a single count entry ────────────────────────────────────────────

@inventory_bp.route('/count/edit-entry/<int:entry_id>', methods=['POST'])
@login_required
def edit_entry(entry_id):
    entry = InventoryCount.query.get_or_404(entry_id)

    denied = _authorize_entry_mutation(entry)
    if denied:
        return denied

    if entry.status == 'withdrawn':
        return jsonify({'ok': False, 'error': 'لا يمكن تعديل إدخال مسحوب'}), 422

    data     = request.get_json(silent=True) or {}
    qty_raw  = data.get('qty')
    unit_id  = data.get('unit_id')
    notes    = (data.get('notes') or '').strip()
    no_stock = bool(data.get('no_stock'))

    if not notes:
        return jsonify({'ok': False, 'error': 'الملاحظة مطلوبة'}), 400
    if qty_raw is None or unit_id is None:
        return jsonify({'ok': False, 'error': 'بيانات ناقصة'}), 400

    try:
        entered_qty = float(qty_raw)
    except (ValueError, TypeError):
        return jsonify({'ok': False, 'error': 'الكمية غير صالحة'}), 400

    if no_stock:
        entered_qty = 0.0
    elif entered_qty <= 0:
        return jsonify({'ok': False, 'error': 'الكمية يجب أن تكون أكبر من صفر'}), 400

    item          = entry.item
    multiplier    = item.get_multiplier(int(unit_id))
    base_quantity = entered_qty * multiplier

    # This mutates ONLY the exact record the employee selected (entry_id from
    # the URL) — never "the latest entry for this item". Other active records
    # for the same item/session are untouched.
    entry.quantity         = base_quantity
    entry.entered_quantity = entered_qty
    entry.entered_unit_id  = int(unit_id)
    entry.notes            = notes or None
    entry.updated_at       = now_jordan()
    db.session.commit()

    total_base, entry_count = _active_totals_for_item(item.id, entry.session_id)
    entered_unit = Unit.query.get(int(unit_id))
    return jsonify({
        'ok':           True,
        'entry_id':     entry.id,
        'session_id':   entry.session_id,
        'session_name': entry.session.name         if entry.session else None,
        'session_type': entry.session.session_type if entry.session else None,
        'entered_qty':  entered_qty,
        'entered_unit': entered_unit.name_ar if entered_unit else '',
        'item_name':    item.name_ar,
        'notes':        notes,
        'total_base':   total_base,
        'entry_count':  entry_count,
        'base_unit':    item.effective_base_unit.name_ar,
    })


# ── guided count: page ───────────────────────────────────────────────────────

@inventory_bp.route('/count/guided')
@login_required
def guided_count():
    """Mobile-first product-by-product guided counting page."""
    if not current_user.is_manager and current_user.branch_id:
        req_branch = request.args.get('branch_id', type=int)
        if req_branch and req_branch != current_user.branch_id:
            flash('وصول غير مصرح به', 'danger')
            return redirect(url_for('inventory.dashboard'))

    branch_id = (
        request.args.get('branch_id', type=int)
        if current_user.is_admin or current_user.is_manager
        else current_user.branch_id
    )

    branches = (
        [current_user.branch]
        if not current_user.is_manager and current_user.branch_id
        else Branch.query.order_by(Branch.name_ar).all()
    )

    inv_session = None
    if branch_id:
        try:
            resolved = resolve_session_for_branch(branch_id)
            if not resolved.is_baseline:
                inv_session = resolved
        except RuntimeError:
            pass

    if (inv_session
            and not current_user.is_manager
            and current_user.department_id
            and inv_session.session_departments):
        allowed_dept_ids = {sd.department_id for sd in inv_session.session_departments}
        if current_user.department_id not in allowed_dept_ids:
            flash('لا توجد جلسة جرد نشطة لقسمك حالياً', 'warning')
            return redirect(url_for('inventory.session_list'))

    return render_template(
        'inventory/guided_count.html',
        branches=branches,
        selected_branch=branch_id,
        inv_session=inv_session,
    )


# ── guided count: items API ───────────────────────────────────────────────────

@inventory_bp.route('/count/guided/items')
@login_required
def guided_count_items():
    """JSON — all items for the session with their units and existing counts."""
    branch_id = (
        request.args.get('branch_id', type=int)
        if current_user.is_admin or current_user.is_manager
        else current_user.branch_id
    )

    if not branch_id:
        return jsonify({'ok': False, 'error': 'الفرع مطلوب'}), 400

    if not current_user.is_manager and current_user.branch_id:
        if branch_id != current_user.branch_id:
            return jsonify({'ok': False, 'error': 'وصول غير مصرح به'}), 403

    try:
        inv_session = resolve_session_for_branch(branch_id)
        if inv_session.is_baseline:
            return jsonify({'ok': False, 'error': 'لا توجد جلسة جرد نشطة حالياً'}), 409
    except RuntimeError as e:
        return jsonify({'ok': False, 'error': str(e)}), 409

    _, scope_dept = get_scope()

    sd_dept_ids = (
        [sd.department_id for sd in inv_session.session_departments]
        if inv_session.session_departments else None
    )

    # Employees are always restricted to their own department
    if not current_user.is_manager and current_user.department_id:
        if sd_dept_ids and current_user.department_id not in sd_dept_ids:
            return jsonify({'ok': False, 'error': 'قسمك غير مشمول في جلسة الجرد النشطة'}), 403
        scope_dept = current_user.department_id

    items_q = (
        Item.query
        .join(Department)
        .filter(Department.branch_id == branch_id, Item.is_active == True)
        .options(
            joinedload(Item.conversions).joinedload(ItemUnitConversion.unit),
            joinedload(Item.base_unit),
            joinedload(Item.unit),
            joinedload(Item.department),
            joinedload(Item.category),
        )
    )
    if sd_dept_ids and not scope_dept:
        items_q = items_q.filter(Item.department_id.in_(sd_dept_ids))
    if scope_dept:
        items_q = items_q.filter(Item.department_id == scope_dept)

    items = items_q.order_by(Item.name_ar).all()

    if not items:
        return jsonify({
            'ok': True,
            'session': {
                'id': inv_session.id,
                'name': inv_session.name,
                'count_date': inv_session.count_date.isoformat(),
                'status': inv_session.status,
            },
            'items': [],
        })

    # Load ALL active counts (any source/workflow) for this session in one query.
    # is_counted / total_qty must reflect every active entry regardless of who
    # or which screen created it — a manual entry alone must still mark the
    # item "counted" here, and its quantity must be part of the combined total.
    item_ids = [i.id for i in items]
    all_counts = (
        InventoryCount.query
        .filter(
            InventoryCount.session_id == inv_session.id,
            InventoryCount.item_id.in_(item_ids),
            InventoryCount.status == 'active',
        )
        .order_by(InventoryCount.created_at.asc())
        .all()
    )

    counted_item_ids = set()
    total_qty_by_item = {}
    # Guided mode's own per-unit editable value — seeded ONLY from entries this
    # same guided workflow previously wrote (source='guided'). Never seeded from
    # 'manual' entries: otherwise re-saving here would duplicate a manual
    # location's quantity as an extra guided row, double-counting the item.
    guided_last = {}
    for c in all_counts:
        counted_item_ids.add(c.item_id)
        total_qty_by_item[c.item_id] = total_qty_by_item.get(c.item_id, 0.0) + c.quantity
        if c.source == 'guided':
            uid = c.entered_unit_id or 0
            guided_last.setdefault(c.item_id, {})[uid] = c

    result = []
    for item in items:
        if item.conversions:
            units_list = [
                {'unit_id': c.unit_id, 'name': c.unit.name_ar, 'multiplier': c.multiplier}
                for c in item.conversions
            ]
        else:
            base_unit = item.effective_base_unit
            units_list = [{
                'unit_id': item.effective_base_unit_id,
                'name': base_unit.name_ar if base_unit else '—',
                'multiplier': 1.0,
            }]

        item_guided = guided_last.get(item.id, {})
        for u in units_list:
            lc = item_guided.get(u['unit_id'])
            if lc:
                u['last_qty'] = lc.entered_quantity
                u['last_at'] = (
                    lc.created_at.strftime('%H:%M · %d/%m') if lc.created_at else None
                )
                u['current_value'] = lc.entered_quantity or 0
            else:
                u['last_qty'] = None
                u['last_at'] = None
                u['current_value'] = 0

        # A prior guided-workflow save is a "no stock" confirmation iff it left
        # exactly one active guided row for this item and its quantity is
        # exactly 0 — the only shape that can occur, since a normal save never
        # writes a zero-quantity row (see the no_stock branch in guided_count_save).
        guided_rows   = list(item_guided.values())
        item_no_stock = len(guided_rows) == 1 and guided_rows[0].entered_quantity == 0

        result.append({
            'id': item.id,
            'name': item.name_ar,
            'sku': item.item_code if current_user.is_manager else None,
            'category': item.category.name_ar if item.category else None,
            'dept': item.department.name_ar if item.department else None,
            'packaging_note': item.packaging_note or None,
            'is_counted': item.id in counted_item_ids,
            # Combined total across ALL sources (manual + guided) — shown as a
            # read-only reference so guided mode never hides manual-count data.
            'total_qty': total_qty_by_item.get(item.id, 0.0),
            'base_unit': item.effective_base_unit.name_ar if item.effective_base_unit else '',
            'units': units_list,
            'no_stock': item_no_stock,
        })

    return jsonify({
        'ok': True,
        'session': {
            'id': inv_session.id,
            'name': inv_session.name,
            'count_date': inv_session.count_date.isoformat(),
            'status': inv_session.status,
        },
        'items': result,
    })


# ── guided count: save one item (replace previous counts) ────────────────────

@inventory_bp.route('/count/guided/save', methods=['POST'])
@login_required
def guided_count_save():
    """Replace all unit counts for one item in the current session."""
    data       = request.get_json(silent=True) or {}
    item_id    = data.get('item_id')
    session_id = data.get('session_id')
    units_data = data.get('units', [])
    no_stock   = bool(data.get('no_stock'))

    if not item_id or not session_id:
        return jsonify({'ok': False, 'error': 'بيانات ناقصة'}), 400

    item = Item.query.get(item_id)
    if not item:
        return jsonify({'ok': False, 'error': 'الصنف غير موجود'}), 404

    if not current_user.is_manager:
        if current_user.branch_id and item.department.branch_id != current_user.branch_id:
            return jsonify({'ok': False, 'error': 'وصول غير مصرح به'}), 403
        if current_user.department_id and item.department_id != current_user.department_id:
            return jsonify({'ok': False, 'error': 'وصول غير مصرح به'}), 403

    inv_session = InventorySession.query.get(session_id)
    if not inv_session:
        return jsonify({'ok': False, 'error': 'الجلسة غير موجودة'}), 404
    if inv_session.is_baseline:
        return jsonify({'ok': False, 'error': 'لا يمكن الحفظ في جلسة أساسية'}), 409
    if not current_user.is_admin and inv_session.status != 'active':
        return jsonify({'ok': False, 'error': 'جلسة الجرد غير نشطة حالياً'}), 409

    if not current_user.is_manager and inv_session.session_departments:
        allowed = {sd.department_id for sd in inv_session.session_departments}
        if current_user.department_id and current_user.department_id not in allowed:
            return jsonify({'ok': False, 'error': 'قسمك غير مشمول في جلسة الجرد'}), 403

    entries_to_create = []
    if no_stock:
        # Explicit "physically checked, nothing there" confirmation — one
        # authoritative zero-quantity record in the item's base unit. Any
        # per-unit values submitted alongside no_stock are ignored: a
        # confirmed-zero item has no meaningful per-unit breakdown.
        entries_to_create.append({'unit_id': item.effective_base_unit_id, 'qty': 0.0})
    else:
        valid_unit_ids = {c.unit_id for c in item.conversions} or {item.effective_base_unit_id}
        for u in units_data:
            uid = u.get('unit_id')
            if uid not in valid_unit_ids:
                return jsonify({'ok': False, 'error': f'وحدة غير صالحة: {uid}'}), 400
            try:
                qty = float(u.get('qty', 0))
            except (ValueError, TypeError):
                qty = 0
            if qty < 0:
                return jsonify({'ok': False, 'error': 'الكمية لا يمكن أن تكون سالبة'}), 400
            if qty > 0:
                entries_to_create.append({'unit_id': uid, 'qty': qty})

    # Replace ONLY this workflow's own previous entries (source='guided') for
    # this item+session — soft-withdrawn, never hard-deleted, so the audit
    # trail is preserved. Manual-count entries (source='manual') for the same
    # item are never touched, deleted, or hidden by this route.
    InventoryCount.query.filter(
        InventoryCount.item_id    == item_id,
        InventoryCount.session_id == session_id,
        InventoryCount.status     == 'active',
        InventoryCount.source     == 'guided',
    ).update({'status': 'withdrawn', 'updated_at': now_jordan()}, synchronize_session=False)

    entry_notes = 'لا يوجد — تم التحقق فعلياً' if no_stock else 'جرد سريع'

    count_date = inv_session.count_date
    for e in entries_to_create:
        multiplier = item.get_multiplier(e['unit_id'])
        db.session.add(InventoryCount(
            item_id          = item.id,
            session_id       = inv_session.id,
            quantity         = e['qty'] * multiplier,
            entered_quantity = e['qty'],
            entered_unit_id  = e['unit_id'],
            count_date       = count_date,
            month            = count_date.month,
            year             = count_date.year,
            user_id          = current_user.id,
            notes            = entry_notes,
            source           = 'guided',
        ))

    db.session.commit()

    # is_counted must reflect "at least one active entry exists", never
    # "the total is a positive number" — a confirmed-zero entry (entry_count=1,
    # total_base=0) is a completed count, not an absence of one.
    total_base, entry_count = _active_totals_for_item(item.id, inv_session.id)

    return jsonify({
        'ok':         True,
        'item_id':    item_id,
        'is_counted': entry_count > 0,
        'total_qty':  total_base,
        'no_stock':   no_stock,
    })


# ── global unit converter (widget API) ───────────────────────────────────────

@inventory_bp.route('/convert')
@login_required
def convert():
    from_id = request.args.get('from', type=int)
    to_id   = request.args.get('to',   type=int)
    value   = request.args.get('value', type=float)

    if not all([from_id, to_id, value is not None]):
        return jsonify({'result': None, 'to_unit': ''})

    if from_id == to_id:
        to_unit = Unit.query.get(to_id)
        return jsonify({'result': value, 'to_unit': to_unit.name_ar if to_unit else ''})

    conversion = UnitConversion.query.filter_by(from_unit_id=from_id, to_unit_id=to_id).first()
    if conversion:
        to_unit = Unit.query.get(to_id)
        return jsonify({'result': value * conversion.factor, 'to_unit': to_unit.name_ar if to_unit else ''})

    return jsonify({'result': None, 'to_unit': ''})


# ── Inventory report ──────────────────────────────────────────────────────────

def _build_report_data(session_id, scope_branch, scope_dept):
    """Load items + counts for a session, scoped to get_scope(). Returns dict."""
    inv_session = (
        InventorySession.query
        .options(
            joinedload(InventorySession.session_departments),
            joinedload(InventorySession.branch),
        )
        .filter_by(id=session_id)
        .first_or_404()
    )

    if scope_branch and inv_session.branch_id != scope_branch:
        abort(403)

    branch   = inv_session.branch
    dept_ids = [sd.department_id for sd in inv_session.session_departments]

    if scope_dept:
        dept_ids = [d for d in dept_ids if d == scope_dept]

    if not dept_ids:
        abort(403)

    items_and_depts = (
        db.session.query(Item, Department)
        .join(Department, Item.department_id == Department.id)
        .filter(Item.department_id.in_(dept_ids), Item.is_active == True)
        .order_by(Department.name_ar, Item.name_ar)
        .all()
    )

    all_entries = (
        db.session.query(InventoryCount)
        .join(Item, InventoryCount.item_id == Item.id)
        .filter(
            InventoryCount.session_id == session_id,
            InventoryCount.status == 'active',
            Item.department_id.in_(dept_ids),
        )
        .order_by(InventoryCount.created_at.desc())
        .all()
    )

    item_latest = {}
    item_totals = {}
    for entry in all_entries:
        item_totals[entry.item_id] = item_totals.get(entry.item_id, 0.0) + entry.quantity
        if entry.item_id not in item_latest:
            item_latest[entry.item_id] = entry

    dept_map   = {}
    dept_order = []
    total_items = counted_items = 0

    for item, dept in items_and_depts:
        total_items += 1
        latest     = item_latest.get(item.id)
        is_counted = latest is not None
        if is_counted:
            counted_items += 1

        if dept.id not in dept_map:
            dept_map[dept.id] = {
                'dept':        dept,
                'counted':     [],
                'uncounted':   [],
                'total':       0,
                'count_count': 0,
            }
            dept_order.append(dept.id)

        dept_map[dept.id]['total'] += 1
        if is_counted:
            dept_map[dept.id]['count_count'] += 1
            dept_map[dept.id]['counted'].append({
                'item':      item,
                'latest':    latest,
                'total_qty': item_totals.get(item.id, 0.0),
            })
        else:
            dept_map[dept.id]['uncounted'].append({'item': item})

    pct = int(counted_items / total_items * 100) if total_items else 0

    return {
        'inv_session':   inv_session,
        'branch':        branch,
        'departments':   [dept_map[k] for k in dept_order],
        'total_items':   total_items,
        'counted_items': counted_items,
        'remaining':     total_items - counted_items,
        'pct':           pct,
    }


@inventory_bp.route('/report')
@login_required
def inventory_report():
    session_id = request.args.get('session_id', type=int)
    if not session_id:
        flash('معرّف الجلسة مطلوب', 'danger')
        return redirect(url_for('inventory.dashboard'))

    scope_branch, scope_dept = get_scope()
    data = _build_report_data(session_id, scope_branch, scope_dept)
    return render_template('inventory/report.html', **data)


@inventory_bp.route('/report/pdf')
@login_required
def inventory_report_pdf():
    session_id = request.args.get('session_id', type=int)
    if not session_id:
        abort(400)

    scope_branch, scope_dept = get_scope()
    data = _build_report_data(session_id, scope_branch, scope_dept)

    html_content = render_template('inventory/report_pdf.html', **data)

    try:
        import weasyprint  # type: ignore[import]
        pdf = weasyprint.HTML(string=html_content, base_url=request.host_url).write_pdf()
        filename = f"inventory-{data['inv_session'].id}-{data['branch'].id}.pdf"
        return Response(
            pdf,
            mimetype='application/pdf',
            headers={'Content-Disposition': f'attachment; filename="{filename}"'},
        )
    except ImportError:
        flash('مكتبة WeasyPrint غير متوفرة على هذا الخادم.', 'warning')
        return redirect(url_for('inventory.inventory_report', session_id=session_id))
