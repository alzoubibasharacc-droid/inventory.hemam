from flask import render_template, request, redirect, url_for, flash
from models import db, Unit, UnitConversion, Item, ItemUnitConversion, InventoryCount
from utils.decorators import admin_required
from routes.admin import admin_bp


def _unit_usage_count(unit_id):
    """Return the total number of references to this unit across all tables."""
    items_ref = (
        Item.query.filter(
            db.or_(Item.unit_id == unit_id, Item.base_unit_id == unit_id)
        ).count()
    )
    conv_ref = ItemUnitConversion.query.filter_by(unit_id=unit_id).count()
    count_ref = InventoryCount.query.filter_by(entered_unit_id=unit_id).count()
    global_conv = UnitConversion.query.filter(
        db.or_(
            UnitConversion.from_unit_id == unit_id,
            UnitConversion.to_unit_id == unit_id,
        )
    ).count()
    return items_ref + conv_ref + count_ref + global_conv


@admin_bp.route('/units', methods=['GET', 'POST'])
@admin_required
def units():
    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'add_unit':
            name_ar = request.form.get('name_ar', '').strip()
            name_en = request.form.get('name_en', '').strip()
            symbol = request.form.get('symbol', '').strip() or None
            if not name_ar:
                flash('اسم الوحدة بالعربية مطلوب', 'danger')
                return redirect(url_for('admin.units'))
            if Unit.query.filter(
                db.func.lower(Unit.name_ar) == name_ar.lower()
            ).first():
                flash(f'الوحدة "{name_ar}" موجودة مسبقاً', 'warning')
                return redirect(url_for('admin.units'))
            db.session.add(Unit(
                name_ar=name_ar,
                name_en=name_en,
                symbol=symbol or name_en or None,
                is_active=True,
            ))
            db.session.commit()
            flash('تم إضافة وحدة القياس', 'success')

        elif action == 'edit_unit':
            unit = Unit.query.get_or_404(int(request.form['unit_id']))
            name_ar = request.form.get('name_ar', '').strip()
            name_en = request.form.get('name_en', '').strip()
            symbol = request.form.get('symbol', '').strip() or None
            if not name_ar:
                flash('اسم الوحدة بالعربية مطلوب', 'danger')
                return redirect(url_for('admin.units'))
            duplicate = Unit.query.filter(
                db.func.lower(Unit.name_ar) == name_ar.lower(),
                Unit.id != unit.id,
            ).first()
            if duplicate:
                flash(f'الوحدة "{name_ar}" موجودة مسبقاً', 'warning')
                return redirect(url_for('admin.units'))
            unit.name_ar = name_ar
            unit.name_en = name_en
            unit.symbol = symbol
            db.session.commit()
            flash('تم تحديث وحدة القياس', 'success')

        elif action == 'toggle_unit':
            unit = Unit.query.get_or_404(int(request.form['unit_id']))
            unit.is_active = not unit.is_active
            db.session.commit()
            state = 'مفعّلة' if unit.is_active else 'موقوفة'
            flash(f'الوحدة "{unit.name_ar}" أصبحت {state}', 'info')

        elif action == 'delete_unit':
            unit = Unit.query.get_or_404(int(request.form['unit_id']))
            usage = _unit_usage_count(unit.id)
            if usage > 0:
                flash(
                    f'لا يمكن حذف "{unit.name_ar}" — مستخدمة في {usage} سجل. '
                    'يمكنك إيقافها بدلاً من الحذف.',
                    'danger',
                )
                return redirect(url_for('admin.units'))
            db.session.delete(unit)
            db.session.commit()
            flash(f'تم حذف الوحدة "{unit.name_ar}"', 'info')

        elif action == 'add_conversion':
            from_id = int(request.form['from_unit_id'])
            to_id = int(request.form['to_unit_id'])
            if from_id == to_id:
                flash('لا يمكن إضافة تحويل من وحدة إلى نفسها', 'warning')
                return redirect(url_for('admin.units'))
            try:
                factor = float(request.form['factor'])
                if factor <= 0:
                    raise ValueError
            except (ValueError, TypeError):
                flash('المعامل يجب أن يكون رقماً موجباً', 'danger')
                return redirect(url_for('admin.units'))
            existing = UnitConversion.query.filter_by(
                from_unit_id=from_id, to_unit_id=to_id
            ).first()
            if existing:
                existing.factor = factor
                flash('تم تحديث معامل التحويل', 'info')
            else:
                db.session.add(UnitConversion(
                    from_unit_id=from_id, to_unit_id=to_id, factor=factor,
                ))
                flash('تم إضافة التحويل', 'success')
            db.session.commit()

        elif action == 'delete_conversion':
            conv = UnitConversion.query.get_or_404(int(request.form['conv_id']))
            db.session.delete(conv)
            db.session.commit()
            flash('تم حذف التحويل', 'info')

        return redirect(url_for('admin.units'))

    all_units = Unit.query.order_by(Unit.is_active.desc(), Unit.name_ar).all()
    conversions = (
        UnitConversion.query
        .join(Unit, UnitConversion.from_unit_id == Unit.id)
        .order_by(Unit.name_ar)
        .all()
    )
    active_units = [u for u in all_units if u.is_active]
    return render_template(
        'admin/units.html',
        units=all_units,
        active_units=active_units,
        conversions=conversions,
    )
