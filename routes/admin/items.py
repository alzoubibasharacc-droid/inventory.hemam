from flask import render_template, request, redirect, url_for, flash
from models import db, Item, Unit, Department, Branch, Category, ItemUnitConversion
from utils.decorators import manager_required
from routes.admin import admin_bp


def _parse_min_stock(raw):
    """Parse minimum_stock from a form string. Returns 0.0 on blank/invalid, never negative."""
    try:
        v = float(raw or 0)
        return max(v, 0.0)
    except (ValueError, TypeError):
        return 0.0


@admin_bp.route('/items', methods=['GET', 'POST'])
@manager_required
def items():
    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'add':
            code = request.form.get('item_code', '').strip() or None
            if code and Item.query.filter_by(item_code=code).first():
                flash(f'كود الصنف "{code}" مستخدم مسبقاً', 'danger')
                return redirect(url_for('admin.items'))

            ms = _parse_min_stock(request.form.get('minimum_stock', ''))
            # base_unit_id is authoritative; unit_id kept in sync for backward compat
            base_unit_id = int(request.form.get('base_unit_id') or request.form['unit_id'])
            item = Item(
                item_code=code,
                name_ar=request.form['name_ar'].strip(),
                name_en=request.form.get('name_en', '').strip(),
                packaging_note=request.form.get('packaging_note', '').strip() or None,
                unit_id=base_unit_id,
                base_unit_id=base_unit_id,
                department_id=int(request.form['department_id']),
                category_id=request.form.get('category_id') or None,
                min_quantity=ms,
                minimum_stock=ms,
            )
            db.session.add(item)
            db.session.flush()

            db.session.add(ItemUnitConversion(
                item_id=item.id,
                unit_id=base_unit_id,
                multiplier=1.0,
            ))
            db.session.commit()
            flash('تم إضافة الصنف بنجاح', 'success')

        elif action == 'edit':
            item = Item.query.get_or_404(int(request.form['item_id']))
            ms = _parse_min_stock(request.form.get('minimum_stock', ''))
            item.minimum_stock  = ms
            item.min_quantity   = ms   # keep legacy field in sync
            pkg = request.form.get('packaging_note', '').strip()
            item.packaging_note = pkg or None
            db.session.commit()
            flash('تم تحديث الصنف', 'success')

        elif action == 'toggle':
            item = Item.query.get_or_404(int(request.form['item_id']))
            item.is_active = not item.is_active
            db.session.commit()
            flash('تم تحديث حالة الصنف', 'info')

        return redirect(url_for('admin.items'))

    branch_filter = request.args.get('branch_id', type=int)
    dept_filter   = request.args.get('dept_id',   type=int)
    search_q      = request.args.get('q', '').strip()

    items_q = Item.query.join(Department)
    if branch_filter:
        items_q = items_q.filter(Department.branch_id == branch_filter)
    if dept_filter:
        items_q = items_q.filter(Item.department_id == dept_filter)
    if search_q:
        like = f'%{search_q}%'
        items_q = items_q.filter(
            db.or_(Item.name_ar.ilike(like), Item.item_code.ilike(like))
        )
    items_list = items_q.order_by(Department.name_ar, Item.name_ar).all()

    return render_template(
        'admin/items.html',
        items=items_list,
        branches=Branch.query.all(),
        departments=Department.query.all(),
        units=Unit.query.filter_by(is_active=True).order_by(Unit.name_ar).all(),
        categories=Category.query.all(),
        selected_branch=branch_filter,
        selected_dept=dept_filter,
        search_q=search_q,
    )


@admin_bp.route('/items/<int:item_id>/conversions', methods=['GET', 'POST'])
@manager_required
def item_conversions(item_id):
    item  = Item.query.get_or_404(item_id)
    units = Unit.query.filter_by(is_active=True).order_by(Unit.name_ar).all()

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'add':
            unit_id = int(request.form['unit_id'])
            try:
                multiplier = float(request.form['multiplier'])
            except ValueError:
                flash('المعامل يجب أن يكون رقماً', 'danger')
                return redirect(url_for('admin.item_conversions', item_id=item_id))

            if multiplier <= 0:
                flash('المعامل يجب أن يكون أكبر من صفر', 'danger')
                return redirect(url_for('admin.item_conversions', item_id=item_id))

            existing = ItemUnitConversion.query.filter_by(
                item_id=item_id, unit_id=unit_id
            ).first()
            if existing:
                existing.multiplier = multiplier
                flash('تم تحديث معامل التحويل', 'info')
            else:
                db.session.add(ItemUnitConversion(
                    item_id=item_id, unit_id=unit_id, multiplier=multiplier
                ))
                flash('تم إضافة وحدة التحويل', 'success')
            db.session.commit()

        elif action == 'delete':
            conv = ItemUnitConversion.query.get_or_404(int(request.form['conv_id']))
            if conv.unit_id == item.effective_base_unit_id and conv.multiplier == 1.0:
                if ItemUnitConversion.query.filter_by(item_id=item_id).count() <= 1:
                    flash('لا يمكن حذف الوحدة الأساسية', 'danger')
                    return redirect(url_for('admin.item_conversions', item_id=item_id))
            db.session.delete(conv)
            db.session.commit()
            flash('تم حذف وحدة التحويل', 'info')

        return redirect(url_for('admin.item_conversions', item_id=item_id))

    conversions = (
        ItemUnitConversion.query
        .filter_by(item_id=item_id)
        .order_by(ItemUnitConversion.multiplier)
        .all()
    )
    return render_template(
        'admin/item_conversions.html',
        item=item, conversions=conversions, units=units,
    )
