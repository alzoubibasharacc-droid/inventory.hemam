import csv
import io

from flask import render_template, request, redirect, url_for, flash
from models import db, Item, Unit, Branch, Department, ItemUnitConversion
from utils.decorators import manager_required
from routes.admin import admin_bp


@admin_bp.route('/import', methods=['GET', 'POST'])
@manager_required
def import_items():
    branches    = Branch.query.all()
    departments = Department.query.all()
    units       = Unit.query.order_by(Unit.name_ar).all()

    active_units = Unit.query.filter_by(is_active=True).order_by(Unit.name_ar).all()

    if request.method == 'GET':
        return render_template(
            'admin/import.html',
            branches=branches, departments=departments,
            units=units, active_units=active_units,
            errors=None, imported=None, skipped=None,
        )

    # ── POST: process uploaded file ───────────────────────────────────────────
    uploaded          = request.files.get('file')
    dept_id           = request.form.get('department_id', type=int)
    auto_create_units = request.form.get('auto_create_units') == '1'

    if not uploaded or uploaded.filename == '':
        flash('يرجى اختيار ملف', 'warning')
        return redirect(url_for('admin.import_items'))
    if not dept_id:
        flash('يرجى اختيار القسم', 'warning')
        return redirect(url_for('admin.import_items'))

    filename = uploaded.filename.lower()
    rows, errors = [], []

    if filename.endswith('.csv'):
        try:
            text = uploaded.read().decode('utf-8-sig')
            rows = list(csv.DictReader(io.StringIO(text)))
        except Exception as e:
            flash(f'خطأ في قراءة ملف CSV: {e}', 'danger')
            return redirect(url_for('admin.import_items'))

    elif filename.endswith('.xlsx'):
        try:
            import openpyxl
            wb = openpyxl.load_workbook(uploaded)
            ws = wb.active
            headers = [
                str(c.value).strip() if c.value else ''
                for c in next(ws.iter_rows(min_row=1, max_row=1))
            ]
            for row in ws.iter_rows(min_row=2, values_only=True):
                rows.append(dict(zip(
                    headers,
                    [str(v).strip() if v is not None else '' for v in row]
                )))
        except Exception as e:
            flash(f'خطأ في قراءة ملف Excel: {e}', 'danger')
            return redirect(url_for('admin.import_items'))

    else:
        flash('صيغة غير مدعومة — يُسمح بـ .xlsx و .csv فقط', 'danger')
        return redirect(url_for('admin.import_items'))

    unit_map        = {u.name_ar.strip(): u for u in units}
    units_created   = 0
    imported        = skipped = 0
    file_codes_seen = set()

    for i, row in enumerate(rows, start=2):
        name_ar   = (row.get('الصنف')         or row.get('name_ar')        or '').strip()
        unit_name = (row.get('الوحدة')         or row.get('unit')           or '').strip()
        min_qty_s = (
            row.get('minimum_stock') or row.get('min_stock') or
            row.get('الحد الأدنى')  or row.get('min_quantity') or '0'
        ).strip()
        item_code = (row.get('الكود')           or row.get('item_code')      or '').strip() or None
        pkg_note  = (row.get('ملاحظة التغليف') or row.get('packaging_note') or '').strip() or None
        name_en   = (row.get('name_en')                                      or '').strip()

        # Extra conversion columns: الوحدة2/المعامل2, الوحدة3/المعامل3, الوحدة4/المعامل4
        extra_convs = []
        for n in ('2', '3', '4'):
            u_name = (row.get(f'الوحدة{n}') or '').strip()
            mult_s = (row.get(f'المعامل{n}') or '').strip()
            if u_name and mult_s:
                extra_convs.append((u_name, mult_s, n))

        # ── Validation ────────────────────────────────────────────────────────
        if not name_ar:
            errors.append(f'السطر {i}: اسم الصنف فارغ — تخطي')
            skipped += 1
            continue

        unit_obj = unit_map.get(unit_name)
        if not unit_obj:
            if auto_create_units and unit_name:
                unit_obj = Unit(name_ar=unit_name, name_en=unit_name, symbol=unit_name, is_active=True)
                db.session.add(unit_obj)
                db.session.flush()
                unit_map[unit_name] = unit_obj
                units_created += 1
                errors.append(f'السطر {i}: وحدة "{unit_name}" غير موجودة — تم إنشاؤها تلقائياً')
            else:
                errors.append(f'السطر {i}: وحدة "{unit_name}" غير موجودة — تخطي')
                skipped += 1
                continue

        try:
            min_qty = float(min_qty_s or '0')
        except ValueError:
            min_qty = 0.0

        if item_code:
            if item_code in file_codes_seen:
                errors.append(f'السطر {i}: كود "{item_code}" مكرر في الملف — تخطي')
                skipped += 1
                continue
            if Item.query.filter_by(item_code=item_code).first():
                errors.append(f'السطر {i}: كود "{item_code}" موجود مسبقاً في قاعدة البيانات — تخطي')
                skipped += 1
                continue
            file_codes_seen.add(item_code)

        if Item.query.filter_by(name_ar=name_ar, department_id=dept_id).first():
            errors.append(f'السطر {i}: "{name_ar}" موجود مسبقاً في هذا القسم — تخطي')
            skipped += 1
            continue

        # Validate extra conversion units before inserting
        conv_pairs = []
        for u_name, mult_s, col_n in extra_convs:
            u_obj = unit_map.get(u_name)
            if not u_obj:
                if auto_create_units and u_name:
                    u_obj = Unit(name_ar=u_name, name_en=u_name, symbol=u_name, is_active=True)
                    db.session.add(u_obj)
                    db.session.flush()
                    unit_map[u_name] = u_obj
                    units_created += 1
                    errors.append(
                        f'السطر {i}: وحدة{col_n} "{u_name}" غير موجودة — تم إنشاؤها تلقائياً'
                    )
                else:
                    errors.append(
                        f'السطر {i}: وحدة{col_n} "{u_name}" غير موجودة — تم إضافة الصنف بدون هذه الوحدة'
                    )
                    continue
            try:
                mult = float(mult_s)
                if mult <= 0:
                    raise ValueError
            except ValueError:
                errors.append(
                    f'السطر {i}: معامل{col_n} "{mult_s}" غير صالح — تم إضافة الصنف بدون هذه الوحدة'
                )
                continue
            conv_pairs.append((u_obj, mult))

        # ── Insert ────────────────────────────────────────────────────────────
        item = Item(
            item_code=item_code,
            name_ar=name_ar,
            name_en=name_en,
            packaging_note=pkg_note,
            unit_id=unit_obj.id,
            base_unit_id=unit_obj.id,
            department_id=dept_id,
            min_quantity=min_qty,
            minimum_stock=min_qty,
        )
        db.session.add(item)
        db.session.flush()

        db.session.add(ItemUnitConversion(
            item_id=item.id, unit_id=unit_obj.id, multiplier=1.0
        ))

        for u_obj, mult in conv_pairs:
            if u_obj.id != unit_obj.id:
                db.session.add(ItemUnitConversion(
                    item_id=item.id, unit_id=u_obj.id, multiplier=mult
                ))

        imported += 1

    db.session.commit()

    parts = [f'تم استيراد {imported} صنف']
    if units_created:
        parts.append(f'أُنشئت {units_created} وحدة جديدة')
    if skipped:
        parts.append(f'تخطي {skipped}')
    flash(' — '.join(parts), 'success' if imported else 'warning')

    active_units = Unit.query.filter_by(is_active=True).order_by(Unit.name_ar).all()
    return render_template(
        'admin/import.html',
        branches=branches, departments=departments,
        units=units, active_units=active_units,
        errors=errors, imported=imported, skipped=skipped,
        units_created=units_created,
    )
