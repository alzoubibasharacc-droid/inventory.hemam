from functools import wraps
from flask import redirect, url_for, flash
from flask_login import login_required, current_user


def manager_required(f):
    """Allow admin and manager roles."""
    @wraps(f)
    @login_required
    def decorated(*args, **kwargs):
        if not current_user.is_manager:
            flash('ليس لديك صلاحية الوصول', 'danger')
            return redirect(url_for('inventory.dashboard'))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    """Allow admin role only."""
    @wraps(f)
    @login_required
    def decorated(*args, **kwargs):
        if not current_user.is_admin:
            flash('هذه الصفحة للمدير فقط', 'danger')
            return redirect(url_for('inventory.dashboard'))
        return f(*args, **kwargs)
    return decorated


def role_required(*roles):
    """
    Generic role guard for future roles (finance, auditor, supervisor, etc.).

    Usage:
        @role_required('admin', 'finance')
        def my_view(): ...
    """
    def decorator(f):
        @wraps(f)
        @login_required
        def decorated(*args, **kwargs):
            if current_user.role not in roles:
                flash('ليس لديك صلاحية الوصول', 'danger')
                return redirect(url_for('inventory.dashboard'))
            return f(*args, **kwargs)
        return decorated
    return decorator


def get_scope():
    """
    Returns (branch_id, dept_id) that must be enforced for the current user.

    Admins:    (None, None)               — unrestricted.
    Managers:  (branch_id or None, None)  — branch-scoped, all departments.
    Employees: (branch_id, dept_id)       — branch + department scoped.
    """
    if current_user.is_admin:
        return None, None
    if current_user.is_manager:
        return current_user.branch_id, None
    return current_user.branch_id, current_user.department_id
