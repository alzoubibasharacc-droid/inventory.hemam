from flask import render_template, request, redirect, url_for, flash
from sqlalchemy import func
from models import db, InventorySession, Branch, InventoryCount
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

        # count_date is fixed for baseline sessions; editable for others
        if not inv_session.is_baseline:
            count_date_str = request.form.get('count_date', '').strip()
            try:
                inv_session.count_date = date.fromisoformat(count_date_str)
            except (ValueError, TypeError):
                flash('تاريخ الجرد غير صحيح', 'danger')
                return redirect(request.url)

        db.session.commit()
        flash('تم حفظ التعديلات بنجاح', 'success')
        return redirect(url_for('admin.inv_sessions_list'))

    return render_template(
        'admin/inv_session_edit.html',
        inv_session=inv_session,
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
