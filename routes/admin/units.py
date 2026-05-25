from flask import render_template, request, redirect, url_for, flash
from models import db, Unit, UnitConversion
from utils.decorators import admin_required
from routes.admin import admin_bp


@admin_bp.route('/units', methods=['GET', 'POST'])
@admin_required
def units():
    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'add_unit':
            db.session.add(Unit(
                name_ar=request.form['name_ar'].strip(),
                name_en=request.form.get('name_en', '').strip(),
            ))
            db.session.commit()
            flash('تم إضافة وحدة القياس', 'success')

        elif action == 'add_conversion':
            db.session.add(UnitConversion(
                from_unit_id=int(request.form['from_unit_id']),
                to_unit_id=int(request.form['to_unit_id']),
                factor=float(request.form['factor']),
            ))
            db.session.commit()
            flash('تم إضافة التحويل', 'success')

        return redirect(url_for('admin.units'))

    return render_template(
        'admin/units.html',
        units=Unit.query.order_by(Unit.name_ar).all(),
        conversions=UnitConversion.query.all(),
    )
