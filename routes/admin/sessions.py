from flask import render_template, request, jsonify, redirect, url_for, flash
from flask_login import current_user
from sqlalchemy import func, distinct
from sqlalchemy.orm import joinedload
from models import (
    db, Department, Item, InventoryCount, InventorySession,
    SessionDepartment, SessionAuditLog,
)
from utils.decorators import admin_required
from routes.admin import admin_bp
from datetime import date


# ── Tracking Kanban ───────────────────────────────────────────────────────────

@admin_bp.route('/sessions')
@admin_required
def sessions():
    # Distinct inventory dates across non-baseline sessions, newest first
    all_dates = (
        db.session.query(InventorySession.count_date)
        .distinct()
        .filter(InventorySession.session_type != 'baseline')
        .order_by(InventorySession.count_date.desc())
        .all()
    )
    available_dates = [r[0] for r in all_dates if r[0]]

    default_date = available_dates[0] if available_dates else date.today()

    count_date_str = request.args.get('count_date', '')
    filter_date = None
    try:
        filter_date = date.fromisoformat(count_date_str) if count_date_str else None
    except ValueError:
        pass
    if not filter_date:
        filter_date = default_date

    # Sessions matching this inventory date
    inv_sessions = (
        InventorySession.query
        .options(
            joinedload(InventorySession.session_departments),
            joinedload(InventorySession.branch),
        )
        .filter(
            InventorySession.count_date == filter_date,
            InventorySession.session_type != 'baseline',
        )
        .order_by(InventorySession.branch_id)
        .all()
    )

    session_ids = [s.id for s in inv_sessions]

    # Bulk: counted distinct items per session, scoped to session's departments
    counted_rows = (
        db.session.query(
            InventoryCount.session_id,
            func.count(distinct(InventoryCount.item_id)),
        )
        .join(Item, Item.id == InventoryCount.item_id)
        .join(SessionDepartment, db.and_(
            SessionDepartment.session_id    == InventoryCount.session_id,
            SessionDepartment.department_id == Item.department_id,
        ))
        .filter(InventoryCount.session_id.in_(session_ids))
        .group_by(InventoryCount.session_id)
        .all()
    ) if session_ids else []
    counted_map = {r[0]: r[1] for r in counted_rows}

    # Bulk: total active items per session via session_departments
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

    not_started = []
    in_progress = []
    completed   = []

    for s in inv_sessions:
        total = total_map.get(s.id, 0)
        if total == 0:
            continue
        counted = counted_map.get(s.id, 0)
        pct = int(counted / total * 100)
        card = {
            'session': s,
            'branch':  s.branch,
            'total':   total,
            'counted': counted,
            'pct':     pct,
        }
        if counted == 0:
            not_started.append(card)
        elif counted >= total:
            completed.append(card)
        else:
            in_progress.append(card)

    return render_template(
        'admin/sessions.html',
        not_started=not_started,
        in_progress=in_progress,
        completed=completed,
        filter_date=filter_date.isoformat() if filter_date else '',
        available_dates=available_dates,
        total_sessions=len(not_started) + len(in_progress) + len(completed),
    )


# ── Session detail (item-level progress + admin corrections) ──────────────────

@admin_bp.route('/sessions/<int:session_id>')
@admin_required
def session_detail(session_id):
    inv_session = (
        InventorySession.query
        .options(
            joinedload(InventorySession.session_departments),
            joinedload(InventorySession.branch),
        )
        .filter_by(id=session_id)
        .first_or_404()
    )
    branch   = inv_session.branch
    dept_ids = [sd.department_id for sd in inv_session.session_departments]

    items_and_depts = (
        db.session.query(Item, Department)
        .join(Department, Item.department_id == Department.id)
        .filter(
            Item.department_id.in_(dept_ids),
            Item.is_active == True,
        )
        .order_by(Department.name_ar, Item.name_ar)
        .all()
    ) if dept_ids else []

    all_entries = (
        db.session.query(InventoryCount)
        .join(Item, InventoryCount.item_id == Item.id)
        .filter(
            InventoryCount.session_id == session_id,
            Item.department_id.in_(dept_ids),
        )
        .order_by(InventoryCount.created_at.desc())
        .all()
    ) if dept_ids else []

    item_latest = {}
    item_count  = {}
    for entry in all_entries:
        item_count[entry.item_id] = item_count.get(entry.item_id, 0) + 1
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
            dept_map[dept.id] = {'dept': dept, 'rows': [], 'counted': 0, 'total': 0}
            dept_order.append(dept.id)

        dept_map[dept.id]['total'] += 1
        if is_counted:
            dept_map[dept.id]['counted'] += 1
        dept_map[dept.id]['rows'].append({
            'item':        item,
            'is_counted':  is_counted,
            'latest':      latest,
            'entry_count': item_count.get(item.id, 0),
        })

    pct = int(counted_items / total_items * 100) if total_items else 0
    if counted_items == 0:
        status = 'not_started'
    elif counted_items >= total_items:
        status = 'completed'
    else:
        status = 'in_progress'

    # Audit log — admins only
    audit_logs = []
    if current_user.is_admin:
        audit_logs = (
            db.session.query(SessionAuditLog)
            .filter_by(session_id=session_id)
            .options(
                joinedload(SessionAuditLog.item),
                joinedload(SessionAuditLog.editor),
            )
            .order_by(SessionAuditLog.changed_at.desc())
            .all()
        )

    return render_template(
        'admin/session_detail.html',
        inv_session=inv_session,
        branch=branch,
        departments=[dept_map[k] for k in dept_order],
        total_items=total_items,
        counted_items=counted_items,
        pct=pct,
        status=status,
        audit_logs=audit_logs,
    )


# ── Admin: edit a count entry in any non-baseline session ─────────────────────

@admin_bp.route('/sessions/<int:session_id>/counts/<int:item_id>/edit', methods=['POST'])
@admin_required
def edit_count(session_id, item_id):
    inv_session = InventorySession.query.get_or_404(session_id)
    item        = Item.query.get_or_404(item_id)

    if inv_session.is_baseline:
        return jsonify({'ok': False, 'error': 'الجلسات الأساسية للقراءة فقط'}), 403

    # Managers (non-admin) may not edit completed sessions
    if not current_user.is_admin and inv_session.status == 'completed':
        return jsonify({'ok': False, 'error': 'لا يمكن تعديل جلسة مكتملة'}), 403

    data        = request.get_json(silent=True) or {}
    new_qty_raw = data.get('quantity')
    reason      = (data.get('reason') or '').strip()

    if new_qty_raw is None:
        return jsonify({'ok': False, 'error': 'الكمية مطلوبة'}), 400

    try:
        new_qty = float(new_qty_raw)
    except (ValueError, TypeError):
        return jsonify({'ok': False, 'error': 'الكمية غير صالحة'}), 400

    if new_qty < 0:
        return jsonify({'ok': False, 'error': 'الكمية لا يمكن أن تكون سالبة'}), 400

    # Reason is mandatory when overriding a completed session
    if current_user.is_admin and inv_session.status == 'completed' and not reason:
        return jsonify({'ok': False, 'error': 'سبب التعديل مطلوب للجلسات المكتملة'}), 400

    latest_entry = (
        InventoryCount.query
        .filter_by(session_id=session_id, item_id=item_id)
        .order_by(InventoryCount.created_at.desc())
        .first()
    )

    if not latest_entry:
        return jsonify({'ok': False, 'error': 'لا يوجد إدخال سابق لهذا الصنف في هذه الجلسة'}), 404

    old_qty = latest_entry.quantity
    latest_entry.quantity = new_qty

    if current_user.is_admin and inv_session.status == 'completed':
        db.session.add(SessionAuditLog(
            session_id=session_id,
            item_id=item_id,
            changed_by=current_user.id,
            field_changed='quantity',
            old_value=str(old_qty),
            new_value=str(new_qty),
            reason=reason or None,
        ))

    db.session.commit()

    unit_name = ''
    if item.effective_base_unit:
        unit_name = item.effective_base_unit.name_ar

    return jsonify({
        'ok':          True,
        'new_quantity': new_qty,
        'unit':        unit_name,
        'item_name':   item.name_ar,
        'logged':      current_user.is_admin and inv_session.status == 'completed',
    })


# ── Admin: delete a session and all its count data ────────────────────────────

@admin_bp.route('/sessions/<int:session_id>/delete', methods=['POST'])
@admin_required
def delete_session(session_id):
    if not current_user.is_admin:
        flash('هذه العملية متاحة للمدير العام فقط', 'danger')
        return redirect(url_for('admin.session_detail', session_id=session_id))

    inv_session = InventorySession.query.get_or_404(session_id)

    if inv_session.is_baseline:
        flash('لا يمكن حذف الجلسات الأساسية', 'danger')
        return redirect(url_for('admin.session_detail', session_id=session_id))

    session_name = inv_session.name

    # Manual cascade: InventoryCount rows have no DB-level ON DELETE CASCADE
    InventoryCount.query.filter_by(session_id=session_id).delete()
    db.session.delete(inv_session)  # DB cascade removes session_departments + audit_log
    db.session.commit()

    flash(f'تم حذف جلسة "{session_name}" وجميع بياناتها بنجاح', 'success')
    return redirect(url_for('admin.inv_sessions_kanban'))
