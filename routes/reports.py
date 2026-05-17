from flask import Blueprint, render_template, request, redirect, url_for, flash, Response
from flask_login import login_required, current_user
from models import Branch, Department, Item, InventoryCount, Unit, User
from datetime import datetime
import io

reports_bp = Blueprint('reports', __name__, url_prefix='/reports')

MONTHS_AR = ['يناير', 'فبراير', 'مارس', 'أبريل', 'مايو', 'يونيو',
             'يوليو', 'أغسطس', 'سبتمبر', 'أكتوبر', 'نوفمبر', 'ديسمبر']

BRAND_GREEN  = '295831'
BRAND_OLIVE  = '4B5947'
BRAND_ORANGE = 'F28705'
BRAND_LIGHT  = 'F4F6F4'
BRAND_WHITE  = 'FFFFFF'


# ─── helpers ────────────────────────────────────────────────────────────────

def _build_count_query(branch_id, dept_id, user_id, month, year, date_from, date_to):
    """Return a filtered InventoryCount query joined to Item and Department."""
    q = InventoryCount.query.join(Item).join(Department)

    if month and year:
        q = q.filter(InventoryCount.month == month, InventoryCount.year == year)
    elif year:
        q = q.filter(InventoryCount.year == year)

    if date_from:
        q = q.filter(InventoryCount.count_date >= date_from)
    if date_to:
        q = q.filter(InventoryCount.count_date <= date_to)

    if branch_id:
        q = q.filter(Department.branch_id == branch_id)
    if dept_id:
        q = q.filter(Item.department_id == dept_id)
    if user_id:
        q = q.filter(InventoryCount.user_id == user_id)

    return q.order_by(Department.branch_id, Department.name_ar, Item.name_ar)


def _parse_export_filters():
    now = datetime.now()
    branch_id  = request.args.get('branch_id', type=int)
    dept_id    = request.args.get('dept_id', type=int)
    user_id    = request.args.get('user_id', type=int)
    month      = request.args.get('month', type=int) or now.month
    year       = request.args.get('year',  type=int) or now.year
    date_from  = None
    date_to    = None
    df_raw = request.args.get('date_from', '').strip()
    dt_raw = request.args.get('date_to', '').strip()
    try:
        date_from = datetime.strptime(df_raw, '%Y-%m-%d').date() if df_raw else None
    except ValueError:
        pass
    try:
        date_to   = datetime.strptime(dt_raw, '%Y-%m-%d').date() if dt_raw else None
    except ValueError:
        pass
    return branch_id, dept_id, user_id, month, year, date_from, date_to


def _build_xlsx(counts, filters_label):
    """Build and return an openpyxl Workbook bytes for the given counts (individual entries)."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'تقرير الجرد'
    ws.sheet_view.rightToLeft = True

    header_fill  = PatternFill('solid', fgColor=BRAND_OLIVE)
    alt_fill     = PatternFill('solid', fgColor='EEF0EE')
    total_fill   = PatternFill('solid', fgColor='D4E6D6')

    header_font  = Font(name='Cairo', bold=True, color=BRAND_WHITE, size=11)
    title_font   = Font(name='Cairo', bold=True, color=BRAND_OLIVE,  size=14)
    sub_font     = Font(name='Cairo', italic=True, color='666666',   size=10)
    data_font    = Font(name='Cairo', size=10)
    total_font   = Font(name='Cairo', bold=True, color=BRAND_GREEN,  size=11)

    center_align = Alignment(horizontal='center', vertical='center', wrap_text=True, readingOrder=2)
    right_align  = Alignment(horizontal='right',  vertical='center', wrap_text=True, readingOrder=2)

    thin  = Side(style='thin',   color='CCCCCC')
    thick = Side(style='medium', color=BRAND_OLIVE)
    cell_border   = Border(left=thin,  right=thin,  top=thin,  bottom=thin)
    header_border = Border(left=thick, right=thick, top=thick, bottom=thick)

    COLS = 11
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=COLS)
    t = ws.cell(1, 1, 'تقرير الجرد - مطاعم همم')
    t.font = title_font
    t.alignment = center_align
    t.fill = PatternFill('solid', fgColor='F4F6F4')

    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=COLS)
    s = ws.cell(2, 1, filters_label)
    s.font = sub_font
    s.alignment = center_align

    ws.row_dimensions[1].height = 30
    ws.row_dimensions[2].height = 18

    headers = [
        'كود الصنف',
        'اسم الصنف',
        'ملاحظة التغليف',
        'الفرع',
        'القسم',
        'الكمية المُدخَلة',
        'وحدة الإدخال',
        'الكمية (وحدة أساسية)',
        'الموظف',
        'تاريخ ووقت الجرد',
        'ملاحظات',
    ]
    for col, h in enumerate(headers, start=1):
        c = ws.cell(3, col, h)
        c.font = header_font
        c.fill = header_fill
        c.alignment = center_align
        c.border = header_border
    ws.row_dimensions[3].height = 24

    total_base = 0.0
    for row_i, cnt in enumerate(counts, start=4):
        fill = PatternFill('solid', fgColor=BRAND_WHITE) if row_i % 2 == 0 else alt_fill
        entered_qty  = cnt.entered_quantity if cnt.entered_quantity is not None else cnt.quantity
        entered_unit = cnt.entered_unit.name_ar if cnt.entered_unit else cnt.item.effective_base_unit.name_ar
        row_data = [
            cnt.item.item_code or '',
            cnt.item.name_ar,
            cnt.item.packaging_note or '',
            cnt.item.department.branch.name_ar,
            cnt.item.department.name_ar,
            entered_qty,
            entered_unit,
            cnt.quantity,
            cnt.user.full_name,
            cnt.created_at.strftime('%Y-%m-%d  %H:%M') if cnt.created_at else '',
            cnt.notes or '',
        ]
        for col, val in enumerate(row_data, start=1):
            c = ws.cell(row_i, col, val)
            c.font = data_font
            c.fill = fill
            c.border = cell_border
            c.alignment = center_align if col in (6, 8) else right_align
            if col in (6, 8):
                c.number_format = '#,##0.###'
        total_base += cnt.quantity
        ws.row_dimensions[row_i].height = 18

    total_row = 4 + len(counts)
    ws.merge_cells(start_row=total_row, start_column=1, end_row=total_row, end_column=7)
    label_c = ws.cell(total_row, 1, f'الإجمالي  ({len(counts)} إدخال)')
    label_c.font = total_font
    label_c.fill = total_fill
    label_c.alignment = center_align
    label_c.border = cell_border

    qty_c = ws.cell(total_row, 8, total_base)
    qty_c.font = total_font
    qty_c.fill = total_fill
    qty_c.alignment = center_align
    qty_c.number_format = '#,##0.###'
    qty_c.border = cell_border

    for col in range(9, COLS + 1):
        c = ws.cell(total_row, col)
        c.fill = total_fill
        c.border = cell_border
    ws.row_dimensions[total_row].height = 22

    col_min_widths = [14, 28, 22, 18, 16, 14, 14, 18, 18, 22, 30]
    for col_i, min_w in enumerate(col_min_widths, start=1):
        max_len = min_w
        for row in ws.iter_rows(min_row=3, min_col=col_i, max_col=col_i):
            for cell in row:
                if cell.value:
                    val_len = len(str(cell.value)) * 1.4
                    max_len = max(max_len, val_len)
        ws.column_dimensions[get_column_letter(col_i)].width = min(max_len, 45)

    ws.freeze_panes = 'A4'

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()


# ─── routes ─────────────────────────────────────────────────────────────────

@reports_bp.route('/')
@login_required
def index():
    now = datetime.now()
    month     = int(request.args.get('month', now.month))
    year      = int(request.args.get('year',  now.year))
    branch_id = request.args.get('branch_id', type=int)
    dept_id   = request.args.get('dept_id',   type=int)

    if not current_user.is_admin and current_user.branch_id:
        branch_id = current_user.branch_id

    branches    = Branch.query.all()
    departments = Department.query.filter_by(branch_id=branch_id).all() if branch_id else []

    query  = InventoryCount.query.join(Item).join(Department)
    query  = query.filter(InventoryCount.month == month, InventoryCount.year == year)
    if branch_id:
        query = query.filter(Department.branch_id == branch_id)
    if dept_id:
        query = query.filter(Item.department_id == dept_id)

    all_entries = query.order_by(Department.name_ar, Item.name_ar, InventoryCount.created_at).all()

    # Aggregate: grouped[dept_name] = [{item, total_base, entries}]
    # Use ordered dict logic: dept → item_id → aggregate
    dept_order  = []
    item_agg    = {}   # item_id → {item, total_base, entries}

    for entry in all_entries:
        dept_name = entry.item.department.name_ar
        if dept_name not in dept_order:
            dept_order.append(dept_name)
        iid = entry.item_id
        if iid not in item_agg:
            item_agg[iid] = {'item': entry.item, 'total_base': 0.0, 'entries': [], 'dept': dept_name}
        item_agg[iid]['total_base'] += entry.quantity
        item_agg[iid]['entries'].append(entry)

    grouped = {d: [] for d in dept_order}
    for agg in item_agg.values():
        grouped[agg['dept']].append(agg)

    return render_template('inventory/reports.html',
                           grouped=grouped,
                           branches=branches,
                           departments=departments,
                           selected_branch=branch_id,
                           selected_dept=dept_id,
                           month=month, year=year,
                           months_ar=MONTHS_AR, now=now)


@reports_bp.route('/export')
@login_required
def export_page():
    if not current_user.is_admin:
        flash('صلاحية المدير مطلوبة لتصدير البيانات', 'danger')
        return redirect(url_for('reports.index'))

    now = datetime.now()
    branches = Branch.query.all()
    departments = Department.query.all()
    employees = User.query.filter_by(is_active=True).order_by(User.full_name).all()

    # Pre-fill filter values for re-display after submit
    branch_id = request.args.get('branch_id', type=int)
    dept_id   = request.args.get('dept_id',   type=int)
    user_id   = request.args.get('user_id',   type=int)
    month     = request.args.get('month', type=int) or now.month
    year      = request.args.get('year',  type=int) or now.year

    # Count matching records for the preview badge
    preview_count = None
    if request.args:
        q = _build_count_query(branch_id, dept_id, user_id, month, year, None, None)
        preview_count = q.count()

    return render_template('inventory/export.html',
                           branches=branches,
                           departments=departments,
                           employees=employees,
                           months_ar=MONTHS_AR,
                           now=now,
                           selected_branch=branch_id,
                           selected_dept=dept_id,
                           selected_user=user_id,
                           sel_month=month,
                           sel_year=year,
                           preview_count=preview_count)


@reports_bp.route('/export/download')
@login_required
def export_download():
    if not current_user.is_admin:
        flash('صلاحية المدير مطلوبة لتصدير البيانات', 'danger')
        return redirect(url_for('reports.index'))

    branch_id, dept_id, user_id, month, year, date_from, date_to = _parse_export_filters()

    counts = _build_count_query(
        branch_id, dept_id, user_id, month, year, date_from, date_to
    ).all()

    if not counts:
        flash('لا توجد بيانات مطابقة للفلتر المحدد', 'warning')
        return redirect(url_for('reports.export_page', **request.args))

    # Build human-readable filter label for the Excel title row
    parts = [MONTHS_AR[month - 1] + ' ' + str(year)]
    if branch_id:
        b = Branch.query.get(branch_id)
        if b:
            parts.append(b.name_ar)
    if dept_id:
        d = Department.query.get(dept_id)
        if d:
            parts.append(d.name_ar)
    if user_id:
        u = User.query.get(user_id)
        if u:
            parts.append(u.full_name)
    filters_label = '  |  '.join(parts)

    xlsx_bytes = _build_xlsx(counts, filters_label)

    filename = f"inventory_{year}_{month:02d}.xlsx"
    return Response(
        xlsx_bytes,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'}
    )


@reports_bp.route('/converter')
@login_required
def converter():
    units = Unit.query.order_by(Unit.name_ar).all()
    return render_template('inventory/converter.html', units=units)
