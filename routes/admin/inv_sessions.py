from flask import render_template, request, redirect, url_for, flash, jsonify
from flask_login import current_user
from sqlalchemy import func, distinct
from sqlalchemy.orm import joinedload
from models import db, InventorySession, Branch, Department, Item, InventoryCount, SessionDepartment, SessionAuditLog
from utils.decorators import admin_required
from routes.admin import admin_bp
from datetime import date
from collections import defaultdict

# ── Label maps (used in templates via context) ────────────────────────────────

SESSION_TYPE_LABELS = {
    'baseline': 'أساسي',
    'official': 'رسمي',
    'quick':    'سريع',
}

SESSION_TYPE_COLORS = {
    'baseline': 'info',
    'official': 'success',
    'quick':    'warning',
}

SESSION_STATUS_LABELS = {
    'draft':     'مسودة',
    'active':    'نشط',
    'paused':    'موقوف',
    'completed': 'مكتمل',
    'archived':  'مؤرشف',
}

SESSION_STATUS_COLORS = {
    'draft':     'secondary',
    'active':    'success',
    'paused':    'warning',
    'completed': 'primary',
    'archived':  'dark',
}


def _session_context():
    """Shared label/color dicts passed to every template in this module."""
    return {
        'type_labels':    SESSION_TYPE_LABELS,
        'type_colors':    SESSION_TYPE_COLORS,
        'status_labels':  SESSION_STATUS_LABELS,
        'status_colors':  SESSION_STATUS_COLORS,
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _dept_map_for_branches(branches):
    """Build {branch_id: [{id, name}]} used by JS in create/edit templates."""
    return {
        branch.id: [
            {'id': d.id, 'name': d.name_ar}
            for d in branch.departments
        ]
        for branch in branches
    }


def _validate_departments(dept_ids, branch):
    """Return error string or None. Checks non-empty + all depts belong to branch."""
    if not dept_ids:
        return 'يجب اختيار قسم واحد على الأقل'
    valid_ids = {d.id for d in branch.departments}
    if any(did not in valid_ids for did in dept_ids):
        return 'بعض الأقسام المختارة لا تنتمي للفرع المحدد'
    return None


# Drag-and-drop transitions allowed on the Kanban board.
# Paused → Completed is intentionally excluded — must go through Active first.
KANBAN_TRANSITIONS = {
    'draft':     ['active'],
    'active':    ['paused', 'completed'],
    'paused':    ['active'],
    'completed': [],
}


# ── Kanban board ──────────────────────────────────────────────────────────────

@admin_bp.route('/inv-sessions/kanban')
@admin_required
def inv_sessions_kanban():
    date_from_str = request.args.get('date_from', '').strip()
    date_to_str   = request.args.get('date_to',   '').strip()
    status_filter = request.args.get('status',    '').strip()

    date_from = date_to = None
    try:
        date_from = date.fromisoformat(date_from_str) if date_from_str else None
    except ValueError:
        date_from_str = ''
    try:
        date_to = date.fromisoformat(date_to_str) if date_to_str else None
    except ValueError:
        date_to_str = ''

    today = date.today()

    # ── Alert counts (branch-scoped for non-managers) ─────────────────────────
    _active_q = InventorySession.query.filter(InventorySession.status == 'active')
    if not current_user.is_manager and current_user.branch_id:
        _active_q = _active_q.filter(InventorySession.branch_id == current_user.branch_id)
    _active_ids_q = _active_q.with_entities(InventorySession.id)

    # Sessions with at least one count entry
    _sessions_with_counts = db.session.query(InventoryCount.session_id).distinct()
    alert_no_counts = _active_q.filter(
        ~InventorySession.id.in_(_sessions_with_counts)
    ).count()

    alert_overdue = _active_q.filter(InventorySession.count_date < today).count()

    # Items in active session departments that have no count entry in any active session
    _counted_items_q = (
        db.session.query(InventoryCount.item_id)
        .filter(InventoryCount.session_id.in_(_active_ids_q))
        .distinct()
    )
    alert_uncounted_items = (
        db.session.query(func.count(distinct(Item.id)))
        .join(SessionDepartment, SessionDepartment.department_id == Item.department_id)
        .filter(
            SessionDepartment.session_id.in_(_active_ids_q),
            Item.is_active == True,
            Item.id.notin_(_counted_items_q),
        )
        .scalar()
    ) or 0

    alerts = {
        'no_counts':       alert_no_counts,
        'overdue':         alert_overdue,
        'uncounted_items': alert_uncounted_items,
        'has_alerts':      bool(alert_no_counts or alert_overdue or alert_uncounted_items),
    }

    # ── Sessions query (with optional date + status filters) ──────────────────
    q = (
        InventorySession.query
        .options(
            joinedload(InventorySession.session_departments)
            .joinedload(SessionDepartment.department),
            joinedload(InventorySession.branch),
        )
        .filter(InventorySession.status.in_(['draft', 'active', 'paused', 'completed']))
    )
    if date_from:
        q = q.filter(InventorySession.count_date >= date_from)
    if date_to:
        q = q.filter(InventorySession.count_date <= date_to)

    if status_filter == 'active':
        q = q.filter(InventorySession.status == 'active')
    elif status_filter == 'completed':
        q = q.filter(InventorySession.status == 'completed')
    elif status_filter == 'overdue':
        q = q.filter(InventorySession.status == 'active',
                     InventorySession.count_date < today)
    elif status_filter == 'no_counts':
        _swc = db.session.query(InventoryCount.session_id).distinct()
        q = q.filter(InventorySession.status == 'active',
                     ~InventorySession.id.in_(_swc))
    elif status_filter == 'uncounted':
        q = q.filter(InventorySession.status == 'active')

    sessions = q.order_by(InventorySession.count_date.desc()).all()

    session_ids = [s.id for s in sessions]

    # Bulk-fetch counted distinct items per session — restricted to the
    # session's assigned departments via session_departments join.
    # Without this join, items from non-assigned departments (possible when
    # an admin counts without dept restriction) would inflate `counted`
    # beyond `total`, producing progress > 100% or false positives.
    counted_rows = (
        db.session.query(
            InventoryCount.session_id,
            func.count(distinct(InventoryCount.item_id)),
        )
        .join(Item, Item.id == InventoryCount.item_id)
        .join(SessionDepartment, db.and_(
            SessionDepartment.session_id   == InventoryCount.session_id,
            SessionDepartment.department_id == Item.department_id,
        ))
        .filter(InventoryCount.session_id.in_(session_ids))
        .group_by(InventoryCount.session_id)
        .all()
    ) if session_ids else []
    counted_map = {r[0]: r[1] for r in counted_rows}

    # Bulk-fetch total active items per session via session_departments — one query
    total_rows = (
        db.session.query(
            SessionDepartment.session_id,
            func.count(distinct(Item.id)),
        )
        .join(Item, Item.department_id == SessionDepartment.department_id)
        .filter(
            SessionDepartment.session_id.in_(session_ids),
            Item.is_active == True,
        )
        .group_by(SessionDepartment.session_id)
        .all()
    ) if session_ids else []
    total_map = {r[0]: r[1] for r in total_rows}

    columns = {s: [] for s in ['draft', 'active', 'paused', 'completed']}
    for session in sessions:
        total   = total_map.get(session.id, 0)
        counted = counted_map.get(session.id, 0)
        columns[session.status].append({
            'session': session,
            'total':   total,
            'counted': counted,
            'percent': int(counted / total * 100) if total else 0,
        })

    return render_template(
        'admin/inv_sessions_kanban.html',
        columns=columns,
        date_from=date_from_str,
        date_to=date_to_str,
        status_filter=status_filter,
        alerts=alerts,
        today=today,
        **_session_context(),
    )


# ── Kanban: move session to new status ────────────────────────────────────────

@admin_bp.route('/inv-sessions/<int:session_id>/move', methods=['POST'])
@admin_required
def inv_session_move(session_id):
    inv_session = InventorySession.query.get_or_404(session_id)
    data        = request.get_json(silent=True) or {}
    new_status  = data.get('status', '').strip()

    allowed = KANBAN_TRANSITIONS.get(inv_session.status, [])
    if new_status not in allowed:
        return jsonify({
            'ok':    False,
            'error': (
                f'لا يمكن التنقل من '
                f'"{SESSION_STATUS_LABELS.get(inv_session.status)}" '
                f'إلى "{SESSION_STATUS_LABELS.get(new_status, new_status)}"'
            ),
        }), 422

    try:
        if new_status == 'active':
            inv_session.open()
        elif new_status == 'paused':
            inv_session.pause()
        elif new_status == 'completed':
            inv_session.close()
        db.session.commit()
    except ValueError as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 422

    return jsonify({
        'ok':         True,
        'session_id': inv_session.id,
        'new_status': inv_session.status,
        'message':    f'تم نقل الجلسة إلى "{SESSION_STATUS_LABELS[inv_session.status]}"',
    })


# ── List ──────────────────────────────────────────────────────────────────────

@admin_bp.route('/inv-sessions')
@admin_required
def inv_sessions_list():
    rows = (
        db.session.query(
            InventorySession,
            Branch,
            func.count(InventoryCount.id).label('count_records'),
        )
        .join(Branch, InventorySession.branch_id == Branch.id)
        .outerjoin(InventoryCount, InventoryCount.session_id == InventorySession.id)
        .group_by(InventorySession.id, Branch.id)
        .order_by(Branch.name_ar, InventorySession.count_date.desc())
        .all()
    )
    return render_template(
        'admin/inv_sessions_list.html',
        rows=rows,
        **_session_context(),
    )


# ── Create ────────────────────────────────────────────────────────────────────

@admin_bp.route('/inv-sessions/new', methods=['GET', 'POST'])
@admin_required
def inv_session_create():
    branches = Branch.query.order_by(Branch.name_ar).all()
    dept_map = _dept_map_for_branches(branches)

    if request.method == 'POST':
        name           = request.form.get('name', '').strip()
        branch_id      = request.form.get('branch_id', type=int)
        count_date_str = request.form.get('count_date', '').strip()
        session_type   = request.form.get('session_type', 'official').strip()
        dept_ids       = request.form.getlist('department_ids', type=int)

        errors = []

        if not name:
            errors.append('اسم الجلسة مطلوب')

        branch = Branch.query.get(branch_id) if branch_id else None
        if not branch:
            errors.append('الفرع مطلوب')

        count_date = None
        try:
            count_date = date.fromisoformat(count_date_str) if count_date_str else None
        except ValueError:
            pass
        if not count_date:
            errors.append('تاريخ الجرد مطلوب وصحيح')

        if session_type not in ('official', 'quick'):
            session_type = 'official'

        if branch:
            dept_error = _validate_departments(dept_ids, branch)
            if dept_error:
                errors.append(dept_error)

        if errors:
            for e in errors:
                flash(e, 'danger')
            return render_template(
                'admin/inv_session_create.html',
                branches=branches,
                dept_map=dept_map,
                selected_dept_ids=dept_ids,
                form_data={
                    'name':         name,
                    'branch_id':    branch_id,
                    'count_date':   count_date_str,
                    'session_type': session_type,
                },
                **_session_context(),
            )

        inv_session = InventorySession(
            name=name,
            branch_id=branch_id,
            count_date=count_date,
            session_type=session_type,
            status='draft',
            created_by=current_user.id,
        )
        db.session.add(inv_session)
        db.session.flush()  # populate inv_session.id before creating children

        for dept_id in dept_ids:
            db.session.add(SessionDepartment(
                session_id=inv_session.id,
                department_id=dept_id,
            ))

        db.session.commit()
        flash(f'تم إنشاء جلسة "{inv_session.name}" بنجاح', 'success')
        return redirect(url_for('admin.inv_session_detail', session_id=inv_session.id))

    return render_template(
        'admin/inv_session_create.html',
        branches=branches,
        dept_map=dept_map,
        selected_dept_ids=[],
        form_data={},
        **_session_context(),
    )


# ── Detail (read-only view) ───────────────────────────────────────────────────

@admin_bp.route('/inv-sessions/<int:session_id>')
@admin_required
def inv_session_detail(session_id):
    inv_session = InventorySession.query.get_or_404(session_id)
    count_records = InventoryCount.query.filter_by(session_id=session_id).count()
    return render_template(
        'admin/inv_session_detail.html',
        inv_session=inv_session,
        count_records=count_records,
        **_session_context(),
    )


# ── Edit ──────────────────────────────────────────────────────────────────────

@admin_bp.route('/inv-sessions/<int:session_id>/edit', methods=['GET', 'POST'])
@admin_required
def inv_session_edit(session_id):
    inv_session = InventorySession.query.get_or_404(session_id)

    if request.method == 'POST':
        name  = request.form.get('name', '').strip()
        notes = request.form.get('notes', '').strip()

        if not name:
            flash('اسم الجلسة مطلوب', 'danger')
            return redirect(request.url)

        inv_session.name  = name
        inv_session.notes = notes or None

        if not inv_session.is_baseline:
            # ── count_date ────────────────────────────────────────────────────
            count_date_str = request.form.get('count_date', '').strip()
            try:
                inv_session.count_date = date.fromisoformat(count_date_str)
            except (ValueError, TypeError):
                flash('تاريخ الجرد غير صحيح', 'danger')
                return redirect(request.url)

            # ── departments ───────────────────────────────────────────────────
            dept_ids   = request.form.getlist('department_ids', type=int)
            dept_error = _validate_departments(dept_ids, inv_session.branch)
            if dept_error:
                flash(dept_error, 'danger')
                return redirect(request.url)

            existing_ids = {sd.department_id for sd in inv_session.session_departments}
            new_ids      = set(dept_ids)

            # Remove de-selected departments
            for sd in list(inv_session.session_departments):
                if sd.department_id not in new_ids:
                    db.session.delete(sd)

            # Add newly selected departments
            for dept_id in new_ids - existing_ids:
                db.session.add(SessionDepartment(
                    session_id=inv_session.id,
                    department_id=dept_id,
                ))

        db.session.commit()
        flash('تم حفظ التعديلات بنجاح', 'success')
        next_url = request.form.get('next', '').strip()
        if next_url and next_url.startswith('/'):
            return redirect(next_url)
        return redirect(url_for('admin.inv_session_detail', session_id=inv_session.id))

    current_dept_ids = {sd.department_id for sd in inv_session.session_departments}
    return render_template(
        'admin/inv_session_edit.html',
        inv_session=inv_session,
        current_dept_ids=current_dept_ids,
        **_session_context(),
    )


# ── Activate ──────────────────────────────────────────────────────────────────

@admin_bp.route('/inv-sessions/<int:session_id>/activate', methods=['POST'])
@admin_required
def inv_session_activate(session_id):
    inv_session = InventorySession.query.get_or_404(session_id)
    try:
        inv_session.open()
        db.session.commit()
        flash(f'تم تفعيل جلسة "{inv_session.name}" بنجاح', 'success')
    except ValueError as e:
        flash(str(e), 'warning')
    return redirect(url_for('admin.inv_session_detail', session_id=session_id))


# ── Pause ─────────────────────────────────────────────────────────────────────

@admin_bp.route('/inv-sessions/<int:session_id>/pause', methods=['POST'])
@admin_required
def inv_session_pause(session_id):
    inv_session = InventorySession.query.get_or_404(session_id)
    try:
        inv_session.pause()
        db.session.commit()
        flash(f'تم إيقاف جلسة "{inv_session.name}" مؤقتاً', 'success')
    except ValueError as e:
        flash(str(e), 'warning')
    return redirect(url_for('admin.inv_session_detail', session_id=session_id))


# ── Complete ──────────────────────────────────────────────────────────────────

@admin_bp.route('/inv-sessions/<int:session_id>/complete', methods=['POST'])
@admin_required
def inv_session_complete(session_id):
    inv_session = InventorySession.query.get_or_404(session_id)
    try:
        inv_session.close()
        db.session.commit()
        flash(f'تم إغلاق جلسة "{inv_session.name}" كمكتملة', 'success')
    except ValueError as e:
        flash(str(e), 'warning')
    return redirect(url_for('admin.inv_session_detail', session_id=session_id))


# ════════════════════════════════════════════════════════════════════════════════
# SESSION CONTROL CENTER
# ════════════════════════════════════════════════════════════════════════════════

@admin_bp.route('/inv-sessions/<int:session_id>/control')
@admin_required
def inv_session_control(session_id):
    inv_session = (
        InventorySession.query
        .options(
            joinedload(InventorySession.session_departments)
            .joinedload(SessionDepartment.department),
            joinedload(InventorySession.branch),
        )
        .get_or_404(session_id)
    )

    session_dept_ids = [sd.department_id for sd in inv_session.session_departments]

    # ── KPI: total active items across session departments ────────────────────
    total_items = (
        db.session.query(func.count(distinct(Item.id)))
        .join(SessionDepartment, SessionDepartment.department_id == Item.department_id)
        .filter(
            SessionDepartment.session_id == session_id,
            Item.is_active == True,
        )
        .scalar()
    ) or 0

    # ── KPI: distinct items with at least one active count ────────────────────
    counted_items = (
        db.session.query(func.count(distinct(InventoryCount.item_id)))
        .join(Item, Item.id == InventoryCount.item_id)
        .filter(
            InventoryCount.session_id == session_id,
            InventoryCount.status == 'active',
            Item.department_id.in_(session_dept_ids) if session_dept_ids else db.false(),
        )
        .scalar()
    ) or 0

    total_entries    = InventoryCount.query.filter_by(session_id=session_id).count()
    total_corrections = SessionAuditLog.query.filter_by(session_id=session_id).count()
    completion_pct   = int(counted_items / total_items * 100) if total_items else 0

    # ── Per-department stats ──────────────────────────────────────────────────
    dept_stats = []
    for sd in inv_session.session_departments:
        dept        = sd.department
        dept_total  = Item.query.filter_by(department_id=dept.id, is_active=True).count()
        dept_counted = (
            db.session.query(func.count(distinct(InventoryCount.item_id)))
            .join(Item, Item.id == InventoryCount.item_id)
            .filter(
                InventoryCount.session_id == session_id,
                InventoryCount.status     == 'active',
                Item.department_id        == dept.id,
            )
            .scalar()
        ) or 0
        last_activity = (
            db.session.query(func.max(InventoryCount.created_at))
            .join(Item, Item.id == InventoryCount.item_id)
            .filter(
                InventoryCount.session_id == session_id,
                Item.department_id        == dept.id,
            )
            .scalar()
        )
        dept_pct = int(dept_counted / dept_total * 100) if dept_total else 0
        dept_stats.append({
            'dept':          dept,
            'total':         dept_total,
            'counted':       dept_counted,
            'remaining':     dept_total - dept_counted,
            'pct':           dept_pct,
            'last_activity': last_activity,
        })

    # ── Audit log (most recent 100 entries) ───────────────────────────────────
    audit_logs = (
        SessionAuditLog.query
        .filter_by(session_id=session_id)
        .order_by(SessionAuditLog.changed_at.desc())
        .limit(100)
        .all()
    )

    current_dept_ids = {sd.department_id for sd in inv_session.session_departments}

    return render_template(
        'admin/inv_session_control.html',
        inv_session=inv_session,
        total_depts=len(inv_session.session_departments),
        total_items=total_items,
        counted_items=counted_items,
        remaining_items=total_items - counted_items,
        total_entries=total_entries,
        total_corrections=total_corrections,
        completion_pct=completion_pct,
        dept_stats=dept_stats,
        audit_logs=audit_logs,
        current_dept_ids=current_dept_ids,
        **_session_context(),
    )


# ── Department Count Report (HTML partial, loaded into modal via AJAX) ────────

@admin_bp.route('/inv-sessions/<int:session_id>/dept/<int:dept_id>/report')
@admin_required
def inv_session_dept_report(session_id, dept_id):
    inv_session = InventorySession.query.get_or_404(session_id)
    dept        = Department.query.get_or_404(dept_id)

    # Guard: dept must be assigned to this session
    SessionDepartment.query.filter_by(
        session_id=session_id, department_id=dept_id
    ).first_or_404()

    items    = (
        Item.query
        .filter_by(department_id=dept_id, is_active=True)
        .order_by(Item.name_ar)
        .all()
    )
    item_ids = [i.id for i in items]

    entries = (
        InventoryCount.query
        .filter(
            InventoryCount.session_id == session_id,
            InventoryCount.item_id.in_(item_ids) if item_ids else db.false(),
        )
        .options(
            joinedload(InventoryCount.user),
            joinedload(InventoryCount.entered_unit),
        )
        .order_by(InventoryCount.item_id, InventoryCount.created_at.desc())
        .all()
    )

    entries_by_item = defaultdict(list)
    for e in entries:
        entries_by_item[e.item_id].append(e)

    item_rows      = []
    last_modified  = None
    total_revisions = 0

    for item in items:
        item_entries   = entries_by_item.get(item.id, [])
        active_entries = [e for e in item_entries if e.status == 'active']
        latest         = active_entries[0] if active_entries else None
        total_qty      = sum(e.quantity for e in active_entries)

        for e in item_entries:
            ts = e.updated_at or e.created_at
            if ts and (last_modified is None or ts > last_modified):
                last_modified = ts

        total_revisions += len(item_entries)

        item_rows.append({
            'item':          item,
            'has_count':     bool(active_entries),
            'latest':        latest,
            'total_qty':     total_qty,
            'entry_count':   len(item_entries),
            'active_count':  len(active_entries),
            'all_entries':   item_entries,
        })

    counted = sum(1 for r in item_rows if r['has_count'])

    return render_template(
        'admin/_dept_report.html',
        inv_session=inv_session,
        dept=dept,
        item_rows=item_rows,
        total_items=len(items),
        counted_items=counted,
        missing_items=len(items) - counted,
        last_modified=last_modified,
        total_revisions=total_revisions,
        can_edit=current_user.is_manager,
    )


# ── Withdraw a count entry ────────────────────────────────────────────────────

@admin_bp.route('/inv-sessions/<int:session_id>/counts/<int:entry_id>/withdraw',
                methods=['POST'])
@admin_required
def inv_count_withdraw(session_id, entry_id):
    InventorySession.query.get_or_404(session_id)
    entry = InventoryCount.query.filter_by(
        id=entry_id, session_id=session_id
    ).first_or_404()

    if entry.status == 'withdrawn':
        return jsonify({'ok': False, 'error': 'هذا الإدخال مسحوب بالفعل'}), 422

    data   = request.get_json(silent=True) or {}
    reason = data.get('reason', '').strip()
    if not reason:
        return jsonify({'ok': False, 'error': 'سبب السحب مطلوب'}), 422

    # Count revisions for this item in this session so we can number them
    rev_no = (
        SessionAuditLog.query
        .filter_by(session_id=session_id, item_id=entry.item_id)
        .count()
    ) + 1

    entry.status = 'withdrawn'

    db.session.add(SessionAuditLog(
        session_id=session_id,
        item_id=entry.item_id,
        changed_by=current_user.id,
        field_changed='status',
        old_value=str(entry.quantity),
        new_value='withdrawn',
        reason=reason,
        action_type='withdrawal',
        revision_number=rev_no,
    ))
    db.session.commit()

    return jsonify({'ok': True, 'message': 'تم سحب الإدخال بنجاح'})


# ── Submit a corrected count (creates new revision, withdraws old entries) ────

@admin_bp.route('/inv-sessions/<int:session_id>/items/<int:item_id>/correct',
                methods=['POST'])
@admin_required
def inv_count_correct(session_id, item_id):
    inv_session = InventorySession.query.get_or_404(session_id)
    item        = Item.query.get_or_404(item_id)

    data   = request.get_json(silent=True) or {}
    reason = data.get('reason', '').strip()
    try:
        qty = float(data['quantity'])
        if qty < 0:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'الكمية غير صحيحة'}), 422

    if not reason:
        return jsonify({'ok': False, 'error': 'سبب التصحيح مطلوب'}), 422

    # Withdraw all current active entries for this item
    old_entries = (
        InventoryCount.query
        .filter_by(session_id=session_id, item_id=item_id)
        .filter(InventoryCount.status == 'active')
        .all()
    )
    old_total = sum(e.quantity for e in old_entries)
    for e in old_entries:
        e.status = 'withdrawn'

    rev_no = (
        SessionAuditLog.query
        .filter_by(session_id=session_id, item_id=item_id)
        .count()
    ) + 1

    # Insert corrected entry
    new_entry = InventoryCount(
        item_id=item_id,
        session_id=session_id,
        quantity=qty,
        entered_quantity=qty,
        entered_unit_id=item.effective_base_unit_id,
        count_date=inv_session.count_date,
        month=inv_session.count_date.month,
        year=inv_session.count_date.year,
        user_id=current_user.id,
        notes=f'تصحيح — {reason}',
        status='active',
    )
    db.session.add(new_entry)

    db.session.add(SessionAuditLog(
        session_id=session_id,
        item_id=item_id,
        changed_by=current_user.id,
        field_changed='quantity',
        old_value=str(old_total),
        new_value=str(qty),
        reason=reason,
        action_type='correction',
        revision_number=rev_no,
    ))
    db.session.commit()

    return jsonify({
        'ok':          True,
        'message':     'تم حفظ التصحيح بنجاح',
        'new_quantity': qty,
    })
