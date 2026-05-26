import csv
import io

from flask import render_template, request, redirect, url_for, flash
from models import db, Item, Unit, Branch, Department, Category, ItemUnitConversion
from utils.decorators import manager_required
from routes.admin import admin_bp


@admin_bp.route('/import', methods=['GET', 'POST'])
@manager_required
def import_items():
    branches     = Branch.query.all()
    departments  = Department.query.all()
    units        = Unit.query.order_by(Unit.name_ar).all()
    active_units = Unit.query.filter_by(is_active=True).order_by(Unit.name_ar).all()

    if request.method == 'GET':
        return render_template(
            'admin/import.html',
            branches=branches, departments=departments,
            units=units, active_units=active_units,
            errors=None, notices=None,
            imported=None, updated=None, skipped=None,
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
    rows, errors, notices = [], [], []

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

    unit_map     = {u.name_ar.strip(): u for u in units}
    # dept_map keyed by "branch_name / dept_name" to handle same-named depts in different branches
    dept_map     = {d.name_ar.strip(): d for d in departments}          # fallback: name only
    dept_map_full = {
        f'{d.branch.name_ar.strip()} / {d.name_ar.strip()}': d
        for d in departments
    }
    cat_map      = {c.name_ar.strip(): c for c in Category.query.all()}
    units_created      = 0
    imported = updated = skipped = 0
    file_codes_seen    = set()   # tracks (item_code, department_id) pairs within this file
    processed_item_ids = set()

    for i, row in enumerate(rows, start=2):
        name_ar   = (row.get('الصنف')         or row.get('name_ar')        or '').strip()
        unit_name = (row.get('الوحدة')         or row.get('unit')           or '').strip()
        min_qty_s = (
            row.get('minimum_stock') or row.get('min_stock') or
            row.get('الحد الأدنى')  or row.get('min_quantity') or ''
        ).strip()
        item_code = (row.get('الكود')           or row.get('item_code')      or '').strip() or None
        pkg_note  = (row.get('ملاحظة التغليف') or row.get('packaging_note') or '').strip() or None
        name_en   = (row.get('name_en')                                      or '').strip() or None
        cat_name  = (row.get('الفئة')           or row.get('category')       or '').strip() or None
        dept_name = (row.get('القسم')           or row.get('department')     or '').strip() or None

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

        # Resolve per-row department (optional override of form selection)
        # Accepts either "فرع / قسم" (unambiguous) or "قسم" alone (first match)
        row_dept_id = dept_id
        if dept_name:
            d_obj = dept_map_full.get(dept_name) or dept_map.get(dept_name)
            if d_obj:
                row_dept_id = d_obj.id
            else:
                errors.append(f'السطر {i}: قسم "{dept_name}" غير موجود — استخدام القسم المختار في النموذج')

        # Resolve unit (required only for new items; optional for updates)
        unit_obj = unit_map.get(unit_name) if unit_name else None
        if unit_name and not unit_obj:
            if auto_create_units:
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

        # Resolve category (optional)
        cat_obj = cat_map.get(cat_name) if cat_name else None
        if cat_name and not cat_obj:
            errors.append(f'السطر {i}: فئة "{cat_name}" غير موجودة — تم تجاهل الفئة')

        # Parse minimum stock — None means "not provided", skip update
        min_qty = None
        if min_qty_s:
            try:
                min_qty = float(min_qty_s)
            except ValueError:
                errors.append(f'السطر {i}: الحد الأدنى "{min_qty_s}" غير صالح — تم تجاهله')

        # ── Detect: find existing item scoped to name + department ──────────
        # Matching is always dept-scoped so items in different sections are
        # treated as independent records even when they share the same name.
        existing_item = None

        if item_code:
            if (item_code, row_dept_id) in file_codes_seen:
                errors.append(f'السطر {i}: كود "{item_code}" مكرر في الملف لنفس القسم — تخطي')
                skipped += 1
                continue
            file_codes_seen.add((item_code, row_dept_id))
            # Match code within the target department only — same code in a different
            # department is an independent item and is never touched here.
            existing_item = Item.query.filter_by(
                item_code=item_code, department_id=row_dept_id
            ).first()

        if existing_item is None:
            # Name match is always dept-scoped — same name in a different section
            # is a completely separate item and will never be touched here.
            existing_item = Item.query.filter_by(
                name_ar=name_ar, department_id=row_dept_id
            ).first()

        # Prevent processing the same item twice in one import
        if existing_item and existing_item.id in processed_item_ids:
            errors.append(f'السطر {i}: الصنف "{name_ar}" تمت معالجته مسبقاً في هذا الملف — تخطي')
            skipped += 1
            continue

        # Validate extra conversion units
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

        if existing_item:
            # ── UPDATE path: only overwrite fields when imported value is non-empty
            changed = []

            if pkg_note is not None:
                existing_item.packaging_note = pkg_note
                changed.append('ملاحظة التغليف')
            if name_en:
                existing_item.name_en = name_en
                changed.append('الاسم الإنجليزي')
            if min_qty is not None:
                existing_item.minimum_stock = min_qty
                existing_item.min_quantity  = min_qty
                changed.append('الحد الأدنى')
            if cat_obj:
                existing_item.category_id = cat_obj.id
                changed.append('الفئة')
            if unit_obj:
                existing_item.unit_id      = unit_obj.id
                existing_item.base_unit_id = unit_obj.id
                changed.append('الوحدة الأساسية')
                # Ensure base unit conversion at multiplier=1.0
                base_conv = ItemUnitConversion.query.filter_by(
                    item_id=existing_item.id, unit_id=unit_obj.id
                ).first()
                if not base_conv:
                    db.session.add(ItemUnitConversion(
                        item_id=existing_item.id, unit_id=unit_obj.id, multiplier=1.0
                    ))
                elif base_conv.multiplier != 1.0:
                    base_conv.multiplier = 1.0

            # Assign code only if item has none (never overwrite an existing code)
            if item_code and not existing_item.item_code:
                existing_item.item_code = item_code
                changed.append('الكود')
            elif item_code and existing_item.item_code and existing_item.item_code != item_code:
                errors.append(
                    f'السطر {i}: تحذير — الكود "{item_code}" يختلف عن '
                    f'الكود الموجود "{existing_item.item_code}" — لم يتم تغييره'
                )

            # Upsert extra unit conversions
            for u_obj, mult in conv_pairs:
                if u_obj.id == (existing_item.base_unit_id or existing_item.unit_id):
                    continue
                conv = ItemUnitConversion.query.filter_by(
                    item_id=existing_item.id, unit_id=u_obj.id
                ).first()
                if conv:
                    if conv.multiplier != mult:
                        conv.multiplier = mult
                        changed.append(f'معامل {u_obj.name_ar}')
                else:
                    db.session.add(ItemUnitConversion(
                        item_id=existing_item.id, unit_id=u_obj.id, multiplier=mult
                    ))
                    changed.append(f'وحدة {u_obj.name_ar}')

            processed_item_ids.add(existing_item.id)
            updated += 1
            detail = '، '.join(changed) if changed else 'لا تغييرات'
            notices.append(f'السطر {i}: "{name_ar}" — تم التحديث ({detail})')

        else:
            # ── CREATE path ───────────────────────────────────────────────────
            if not unit_obj:
                errors.append(
                    f'السطر {i}: "{name_ar}" — لا توجد وحدة محددة لإنشاء الصنف الجديد — تخطي'
                )
                skipped += 1
                continue

            item = Item(
                item_code=item_code,
                name_ar=name_ar,
                name_en=name_en or '',
                packaging_note=pkg_note,
                unit_id=unit_obj.id,
                base_unit_id=unit_obj.id,
                department_id=row_dept_id,
                min_quantity=min_qty or 0.0,
                minimum_stock=min_qty or 0.0,
                category_id=cat_obj.id if cat_obj else None,
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

            processed_item_ids.add(item.id)
            imported += 1

    db.session.commit()

    parts = []
    if imported:
        parts.append(f'تم إنشاء {imported} صنف جديد')
    if updated:
        parts.append(f'تحديث {updated} صنف موجود')
    if units_created:
        parts.append(f'أُنشئت {units_created} وحدة جديدة')
    if skipped:
        parts.append(f'تخطي {skipped}')
    if not parts:
        parts = ['لم يتم معالجة أي صنف']
    flash(' — '.join(parts), 'success' if (imported or updated) else 'warning')

    active_units = Unit.query.filter_by(is_active=True).order_by(Unit.name_ar).all()
    return render_template(
        'admin/import.html',
        branches=branches, departments=departments,
        units=units, active_units=active_units,
        errors=errors, notices=notices,
        imported=imported, updated=updated, skipped=skipped,
        units_created=units_created,
    )
