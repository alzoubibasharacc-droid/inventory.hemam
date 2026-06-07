from flask import render_template, request, redirect, url_for, flash, jsonify
from flask_login import current_user
from sqlalchemy import func, distinct
from sqlalchemy.orm import joinedload
from models import db, InventorySession, Branch, Department, Item, InventoryCount, SessionDepartment
from utils.decorators import admin_required
from utils.constants import get_active_session
from routes.admin import admin_bp
from datetime import date

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
    sessions = (
        InventorySession.query
        .options(
            joinedload(InventorySession.session_departments)
            .joinedload(SessionDepartment.department),
            joinedload(InventorySession.branch),
        )
        .filter(InventorySession.status.in_(['draft', 'active', 'paused', 'completed']))
        .order_by(InventorySession.count_date.desc())
        .all()
    )

    session_ids = [s.id for s in sessions]

    # Bulk-fetch counted distinct items per session — one query
    counted_rows = (
        db.session.query(
            InventoryCount.session_id,
            func.count(distinct(InventoryCount.item_id)),
        )
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
