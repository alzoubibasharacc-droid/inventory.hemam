from flask import render_template, request
from sqlalchemy import func, distinct
from models import db, Branch, Department, Item, InventoryCount
from utils.decorators import admin_required
from routes.admin import admin_bp
from datetime import datetime, date
import calendar


def _parse_period():
    now = datetime.now()
    date_from_str = request.args.get('date_from', '')
    date_to_str   = request.args.get('date_to',   '')

    date_from = date_to = None
    try:
        date_from = datetime.strptime(date_from_str, '%Y-%m-%d').date() if date_from_str else None
    except ValueError:
        pass
    try:
        date_to = datetime.strptime(date_to_str, '%Y-%m-%d').date() if date_to_str else None
    except ValueError:
        pass

    if not date_from and not date_to:
        date_from = date(now.year, now.month, 1)
        date_to   = date(now.year, now.month, calendar.monthrange(now.year, now.month)[1])
        date_from_str = date_from.isoformat()
        date_to_str   = date_to.isoformat()

    return date_from, date_to, date_from_str, date_to_str


# ── Kanban overview ────────────────────────────────────────────────────────────

@admin_bp.route('/sessions')
@admin_required
def sessions():
    date_from, date_to, date_from_str, date_to_str = _parse_period()

    # Subquery: total active items per branch
    total_sq = (
        db.session.query(
            Department.branch_id.label('branch_id'),
            func.count(Item.id).label('total_items'),
        )
        .join(Item, Item.department_id == Department.id)
        .filter(Item.is_active == True)
        .group_by(Department.branch_id)
        .subquery('total')
    )

    # Subquery: distinct counted items + last activity per branch in period
    counted_sq = (
        db.session.query(
            Department.branch_id.label('branch_id'),
            func.count(distinct(InventoryCount.item_id)).label('counted_items'),
            func.max(InventoryCount.count_date).label('last_activity'),
        )
        .join(Item,       InventoryCount.item_id    == Item.id)
        .join(Department, Item.department_id         == Department.id)
        .filter(
            InventoryCount.count_date >= date_from,
            InventoryCount.count_date <= date_to,
        )
        .group_by(Department.branch_id)
        .subquery('counted')
    )

    rows = (
        db.session.query(
            Branch,
            func.coalesce(total_sq.c.total_items,   0).label('total_items'),
            func.coalesce(counted_sq.c.counted_items, 0).label('counted_items'),
            counted_sq.c.last_activity,
        )
        .outerjoin(total_sq,  Branch.id == total_sq.c.branch_id)
        .outerjoin(counted_sq, Branch.id == counted_sq.c.branch_id)
        .order_by(Branch.name_ar)
        .all()
    )

    not_started = []
    in_progress = []
    completed   = []

    for branch, total_items, counted_items, last_activity in rows:
        if total_items == 0:
            continue  # branch has no active items — nothing to track

        pct = int(counted_items / total_items * 100)
        card = {
            'branch':        branch,
            'total_items':   total_items,
            'counted_items': counted_items,
            'last_activity': last_activity,
            'pct':           pct,
        }

        if counted_items == 0:
            not_started.append(card)
        elif counted_items >= total_items:
            completed.append(card)
        else:
            in_progress.append(card)

    return render_template(
        'admin/sessions.html',
        not_started=not_started,
        in_progress=in_progress,
        completed=completed,
        date_from=date_from_str,
        date_to=date_to_str,
        total_branches=len(not_started) + len(in_progress) + len(completed),
    )


# ── Drill-down: single branch ──────────────────────────────────────────────────

@admin_bp.route('/sessions/<int:branch_id>')
@admin_required
def session_detail(branch_id):
    date_from, date_to, date_from_str, date_to_str = _parse_period()

    branch = Branch.query.get_or_404(branch_id)

    # All active items in this branch, ordered by department then item name
    items_and_depts = (
        db.session.query(Item, Department)
        .join(Department, Item.department_id == Department.id)
        .filter(
            Department.branch_id == branch_id,
            Item.is_active == True,
        )
        .order_by(Department.name_ar, Item.name_ar)
        .all()
    )

    # All count entries for this branch+period — loaded once, grouped in Python
    all_entries = (
        db.session.query(InventoryCount)
        .join(Item,       InventoryCount.item_id    == Item.id)
        .join(Department, Item.department_id         == Department.id)
        .filter(
            Department.branch_id == branch_id,
            InventoryCount.count_date >= date_from,
            InventoryCount.count_date <= date_to,
        )
        .order_by(InventoryCount.created_at.desc())
        .all()
    )

    # Per-item: latest entry + entry count (entries are ordered newest-first)
    item_latest = {}     # item_id → most-recent InventoryCount row
    item_count  = {}     # item_id → total entry count in period

    for entry in all_entries:
        item_count[entry.item_id] = item_count.get(entry.item_id, 0) + 1
        if entry.item_id not in item_latest:
            item_latest[entry.item_id] = entry

    # Organise into per-department groups
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
            dept_map[dept.id] = {
                'dept':    dept,
                'rows':    [],
                'counted': 0,
                'total':   0,
            }
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

    return render_template(
        'admin/session_detail.html',
        branch=branch,
        departments=[dept_map[k] for k in dept_order],
        total_items=total_items,
        counted_items=counted_items,
        pct=pct,
        status=status,
        date_from=date_from_str,
        date_to=date_to_str,
    )
