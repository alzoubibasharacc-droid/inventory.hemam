from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required, current_user
from models import db, Branch, Department, Item, InventoryCount, Unit, UnitConversion
from sqlalchemy import func, distinct
from datetime import datetime, date

inventory_bp = Blueprint('inventory', __name__, url_prefix='/inventory')

MONTHS_AR = ['يناير', 'فبراير', 'مارس', 'أبريل', 'مايو', 'يونيو',
             'يوليو', 'أغسطس', 'سبتمبر', 'أكتوبر', 'نوفمبر', 'ديسمبر']


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
    now   = datetime.now()
    month = int(request.args.get('month', now.month))
    year  = int(request.args.get('year',  now.year))

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
                InventoryCount.month == month,
                InventoryCount.year  == year,
            ).scalar() or 0
        )

    recent_q = (
        InventoryCount.query
        .join(Item).join(Department)
        .filter(InventoryCount.month == month, InventoryCount.year == year)
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
        month=month, year=year,
        months_ar=MONTHS_AR,
        now=now,
        total_items=total_items,
        counted_items=counted_items,
        recent_entries=recent_entries,
    )


# ── AJAX: search items ────────────────────────────────────────────────────────

@inventory_bp.route('/count/search')
@login_required
def count_search():
    q         = request.args.get('q', '').strip()
    branch_id = request.args.get('branch_id', type=int)
    month     = request.args.get('month', type=int)
    year      = request.args.get('year',  type=int)

    if not branch_id and not (current_user.is_admin or current_user.is_manager):
        branch_id = current_user.branch_id

    items_q = Item.query.join(Department).filter(Item.is_active == True)
    if branch_id:
        items_q = items_q.filter(Department.branch_id == branch_id)
    if q:
        items_q = items_q.filter(Item.name_ar.ilike(f'%{q}%'))

    items = items_q.order_by(Item.name_ar).limit(20).all()

    counted_ids = set()
    if month and year and items:
        item_ids = [i.id for i in items]
        rows = (
            db.session.query(InventoryCount.item_id)
            .filter(
                InventoryCount.item_id.in_(item_ids),
                InventoryCount.month == month,
                InventoryCount.year  == year,
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
    month   = data.get('month')
    year    = data.get('year')

    if not all([item_id, qty_raw is not None, unit_id, month, year]):
        return jsonify({'ok': False, 'error': 'بيانات ناقصة'}), 400

    try:
        entered_qty = float(qty_raw)
    except (ValueError, TypeError):
        return jsonify({'ok': False, 'error': 'الكمية غير صالحة'}), 400

    if entered_qty <= 0:
        return jsonify({'ok': False, 'error': 'الكمية يجب أن تكون أكبر من صفر'}), 400

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
        count_date       = date(int(year), int(month), 1),
        month            = int(month),
        year             = int(year),
        user_id          = current_user.id,
        notes            = notes or None,
    )
    db.session.add(entry)
    db.session.commit()

    all_entries = InventoryCount.query.filter_by(
        item_id=item.id, month=month, year=year
    ).all()
    total_base = sum(e.quantity for e in all_entries)

    entered_unit = Unit.query.get(int(unit_id))

    return jsonify({
        'ok':           True,
        'entry_id':     entry.id,
        'total_base':   total_base,
        'base_unit':    item.effective_base_unit.name_ar,
        'entry_count':  len(all_entries),
        'entered_qty':  entered_qty,
        'entered_unit': entered_unit.name_ar if entered_unit else '',
        'item_name':    item.name_ar,
        'user_name':    current_user.full_name,
        'time':         entry.created_at.strftime('%H:%M') if entry.created_at else '',
    })


# ── delete a single count entry ───────────────────────────────────────────────

@inventory_bp.route('/count/delete-entry/<int:entry_id>', methods=['POST'])
@login_required
def delete_entry(entry_id):
    entry = InventoryCount.query.get_or_404(entry_id)

    if entry.user_id != current_user.id and not current_user.is_manager:
        if request.is_json:
            return jsonify({'ok': False, 'error': 'لا صلاحية'}), 403
        flash('ليس لديك صلاحية حذف هذا الإدخال', 'danger')
        return redirect(url_for('inventory.count'))

    item  = entry.item
    dept  = item.department
    month = entry.month
    year  = entry.year

    db.session.delete(entry)
    db.session.commit()

    if request.is_json:
        remaining  = InventoryCount.query.filter_by(item_id=item.id, month=month, year=year).all()
        total_base = sum(e.quantity for e in remaining)
        return jsonify({'ok': True, 'total_base': total_base, 'entry_count': len(remaining)})

    flash('تم حذف الإدخال', 'info')
    return redirect(url_for('inventory.count', branch_id=dept.branch_id, month=month, year=year))


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
