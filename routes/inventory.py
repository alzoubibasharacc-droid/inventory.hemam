from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, session as flask_session
from flask_login import login_required, current_user
from models import db, Branch, Department, Item, InventoryCount, Unit, UnitConversion
from sqlalchemy import func, distinct
from datetime import datetime, date

inventory_bp = Blueprint('inventory', __name__, url_prefix='/inventory')


# ── dashboard ─────────────────────────────────────────────────────────────────

@inventory_bp.route('/dashboard')
@login_required
def dashboard():
    now = datetime.now()
    month, year = now.month, now.year

    if current_user.is_admin:
        branches = Branch.query.all()
    elif current_user.branch_id:
        branches = [current_user.branch]
    else:
        branches = Branch.query.all()

    branch_stats = []
    for branch in branches:
        total_items = (
            Item.query.join(Department)
            .filter(Department.branch_id == branch.id, Item.is_active == True)
            .count()
        )
        counted_items = (
            db.session.query(func.count(distinct(InventoryCount.item_id)))
            .join(Item).join(Department)
            .filter(
                Department.branch_id == branch.id,
                InventoryCount.month == month,
                InventoryCount.year == year,
            ).scalar() or 0
        )
        branch_stats.append({
            'branch':  branch,
            'total':   total_items,
            'counted': counted_items,
            'percent': int(counted_items / total_items * 100) if total_items else 0,
        })

    total_items = Item.query.filter_by(is_active=True).count()
    total_counted = (
        db.session.query(func.count(distinct(InventoryCount.item_id)))
        .filter(InventoryCount.month == month, InventoryCount.year == year)
        .scalar() or 0
    )

    return render_template(
        'dashboard.html',
        branch_stats=branch_stats,
        total_items=total_items,
        total_counted=total_counted,
        month=month, year=year, now=now,
    )


# ── count (main page, GET only) ───────────────────────────────────────────────

@inventory_bp.route('/count')
@login_required
def count():
    now = datetime.now()

    # Lock the count date for this browser session on first visit.
    # Subsequent visits (even after midnight) reuse the same locked date.
    count_date = flask_session.get('count_session_date')
    if not count_date:
        count_date = now.strftime('%Y-%m-%d')
        flask_session['count_session_date'] = count_date

    try:
        cd = date.fromisoformat(count_date)
    except (ValueError, TypeError):
        cd = now.date()
        count_date = cd.strftime('%Y-%m-%d')
        flask_session['count_session_date'] = count_date

    branch_id = (
        request.args.get('branch_id', type=int)
        if current_user.is_admin or current_user.is_manager
        else current_user.branch_id
    )

    branches = Branch.query.all()

    total_items = counted_items = 0
    if branch_id:
        total_items = (
            Item.query.join(Department)
            .filter(Department.branch_id == branch_id, Item.is_active == True)
            .count()
        )
        counted_items = (
            db.session.query(func.count(distinct(InventoryCount.item_id)))
            .join(Item).join(Department)
            .filter(
                Department.branch_id == branch_id,
                InventoryCount.count_date == cd,
            ).scalar() or 0
        )

    recent_q = (
        InventoryCount.query
        .join(Item).join(Department)
        .filter(InventoryCount.count_date == cd)
    )
    if branch_id:
        recent_q = recent_q.filter(Department.branch_id == branch_id)
    if not current_user.is_manager:
        recent_q = recent_q.filter(InventoryCount.user_id == current_user.id)

    recent_entries = recent_q.order_by(InventoryCount.created_at.desc()).limit(50).all()

    return render_template(
        'inventory/count.html',
        branches=branches,
        selected_branch=branch_id,
        now=now,
        total_items=total_items,
        counted_items=counted_items,
        recent_entries=recent_entries,
        count_date=count_date,
    )


# ── AJAX: search items ────────────────────────────────────────────────────────

@inventory_bp.route('/count/search')
@login_required
def count_search():
    q          = request.args.get('q', '').strip()
    branch_id  = request.args.get('branch_id', type=int)
    count_date = request.args.get('count_date', '').strip()

    if not branch_id and not (current_user.is_admin or current_user.is_manager):
        branch_id = current_user.branch_id

    items_q = Item.query.join(Department).filter(Item.is_active == True)
    if branch_id:
        items_q = items_q.filter(Department.branch_id == branch_id)
    if q:
        items_q = items_q.filter(Item.name_ar.ilike(f'%{q}%'))

    items = items_q.order_by(Item.name_ar).limit(20).all()

    counted_ids = set()
    if count_date and items:
        try:
            cd = date.fromisoformat(count_date)
        except (ValueError, TypeError):
            cd = None
        if cd:
            item_ids = [i.id for i in items]
            rows = (
                db.session.query(InventoryCount.item_id)
                .filter(
                    InventoryCount.item_id.in_(item_ids),
                    InventoryCount.count_date == cd,
                ).distinct().all()
            )
            counted_ids = {r[0] for r in rows}

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
            'already_counted': item.id in counted_ids,
            'allowed_units':   allowed_units,
            'base_unit':       item.effective_base_unit.name_ar,
        })

    return jsonify(result)


# ── AJAX: save a single count entry ──────────────────────────────────────────

@inventory_bp.route('/count/entry', methods=['POST'])
@login_required
def count_entry():
    data    = request.get_json(silent=True) or {}
    item_id = data.get('item_id')
    qty_raw = data.get('qty')
    unit_id = data.get('unit_id')
    notes   = (data.get('notes') or '').strip()

    if not all([item_id, qty_raw is not None, unit_id]):
        return jsonify({'ok': False, 'error': 'بيانات ناقصة'}), 400

    if not notes:
        return jsonify({'ok': False, 'error': 'الملاحظة مطلوبة'}), 400

    try:
        entered_qty = float(qty_raw)
    except (ValueError, TypeError):
        return jsonify({'ok': False, 'error': 'الكمية غير صالحة'}), 400

    if entered_qty <= 0:
        return jsonify({'ok': False, 'error': 'الكمية يجب أن تكون أكبر من صفر'}), 400

    # Always use the server-side locked session date — ignore any client-submitted date.
    session_date_s = flask_session.get('count_session_date')
    if not session_date_s:
        session_date_s = date.today().isoformat()
        flask_session['count_session_date'] = session_date_s
    try:
        count_date_obj = date.fromisoformat(session_date_s)
    except (ValueError, TypeError):
        count_date_obj = date.today()
        flask_session['count_session_date'] = count_date_obj.isoformat()

    month = count_date_obj.month
    year  = count_date_obj.year

    item = Item.query.get(item_id)
    if not item:
        return jsonify({'ok': False, 'error': 'الصنف غير موجود'}), 404

    multiplier    = item.get_multiplier(int(unit_id))
    base_quantity = entered_qty * multiplier

    entry = InventoryCount(
        item_id          = item.id,
        quantity         = base_quantity,
        entered_quantity = entered_qty,
        entered_unit_id  = int(unit_id),
        count_date       = count_date_obj,
        month            = month,
        year             = year,
        user_id          = current_user.id,
        notes            = notes or None,
    )
    db.session.add(entry)
    db.session.commit()

    all_entries = InventoryCount.query.filter(
        InventoryCount.item_id    == item.id,
        InventoryCount.count_date == count_date_obj,
    ).all()
    total_base = sum(e.quantity for e in all_entries)

    entered_unit = Unit.query.get(int(unit_id))

    allowed_units = [{'id': c.unit_id, 'name': c.unit.name_ar} for c in item.conversions]
    if not allowed_units:
        allowed_units = [{'id': item.effective_base_unit_id, 'name': item.effective_base_unit.name_ar}]

    return jsonify({
        'ok':              True,
        'entry_id':        entry.id,
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
    })


# ── start a new counting session (clears the locked date) ────────────────────

@inventory_bp.route('/count/new-session', methods=['POST'])
@login_required
def new_count_session():
    flask_session.pop('count_session_date', None)
    branch_id = request.form.get('branch_id', '')
    kwargs = {'branch_id': branch_id} if branch_id else {}
    return redirect(url_for('inventory.count', **kwargs))


# ── delete a single count entry ───────────────────────────────────────────────

@inventory_bp.route('/count/delete-entry/<int:entry_id>', methods=['POST'])
@login_required
def delete_entry(entry_id):
    entry = InventoryCount.query.get_or_404(entry_id)

    if not current_user.is_admin:
        if request.is_json:
            return jsonify({'ok': False, 'error': 'الحذف للمدير فقط'}), 403
        flash('الحذف متاح للمدير فقط', 'danger')
        return redirect(url_for('inventory.count'))

    item       = entry.item
    dept       = item.department
    count_date = entry.count_date

    db.session.delete(entry)
    db.session.commit()

    if request.is_json:
        remaining = InventoryCount.query.filter(
            InventoryCount.item_id    == item.id,
            InventoryCount.count_date == count_date,
        ).all()
        total_base = sum(e.quantity for e in remaining)
        return jsonify({'ok': True, 'total_base': total_base, 'entry_count': len(remaining)})

    flash('تم حذف الإدخال', 'info')
    return redirect(url_for('inventory.count', branch_id=dept.branch_id, count_date=count_date.isoformat()))


# ── edit a single count entry ────────────────────────────────────────────

@inventory_bp.route('/count/edit-entry/<int:entry_id>', methods=['POST'])
@login_required
def edit_entry(entry_id):
    entry = InventoryCount.query.get_or_404(entry_id)

    if entry.user_id != current_user.id and not current_user.is_manager:
        return jsonify({'ok': False, 'error': 'لا صلاحية'}), 403

    data    = request.get_json(silent=True) or {}
    qty_raw = data.get('qty')
    unit_id = data.get('unit_id')
    notes   = (data.get('notes') or '').strip()

    if not notes:
        return jsonify({'ok': False, 'error': 'الملاحظة مطلوبة'}), 400
    if qty_raw is None or unit_id is None:
        return jsonify({'ok': False, 'error': 'بيانات ناقصة'}), 400

    try:
        entered_qty = float(qty_raw)
    except (ValueError, TypeError):
        return jsonify({'ok': False, 'error': 'الكمية غير صالحة'}), 400

    if entered_qty <= 0:
        return jsonify({'ok': False, 'error': 'الكمية يجب أن تكون أكبر من صفر'}), 400

    item          = entry.item
    multiplier    = item.get_multiplier(int(unit_id))
    base_quantity = entered_qty * multiplier

    entry.quantity         = base_quantity
    entry.entered_quantity = entered_qty
    entry.entered_unit_id  = int(unit_id)
    entry.notes            = notes or None
    entry.updated_at       = datetime.now()
    db.session.commit()

    entered_unit = Unit.query.get(int(unit_id))
    return jsonify({
        'ok':           True,
        'entry_id':     entry.id,
        'entered_qty':  entered_qty,
        'entered_unit': entered_unit.name_ar if entered_unit else '',
        'item_name':    item.name_ar,
        'notes':        notes,
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
