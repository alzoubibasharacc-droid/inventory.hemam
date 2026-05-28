from flask import render_template, request, redirect, url_for, flash, abort
from flask_login import current_user
from models import db, User, Branch, Department
from utils.decorators import admin_required
from routes.admin import admin_bp


@admin_bp.route('/users', methods=['GET', 'POST'])
@admin_required
def users():
    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'add':
            if User.query.filter_by(username=request.form['username'].strip()).first():
                flash('اسم المستخدم موجود مسبقاً', 'danger')
            else:
                user = User(
                    username=request.form['username'].strip(),
                    full_name=request.form['full_name'].strip(),
                    role=request.form['role'],
                    branch_id=request.form.get('branch_id') or None,
                    department_id=request.form.get('department_id') or None,
                )
                user.set_password(request.form['password'])
                db.session.add(user)
                db.session.commit()
                flash('تم إضافة المستخدم بنجاح', 'success')

        elif action == 'edit':
            if current_user.role != 'admin':
                abort(403)
            user = User.query.get_or_404(int(request.form['user_id']))
            new_username = request.form['username'].strip()
            existing = User.query.filter_by(username=new_username).first()
            if existing and existing.id != user.id:
                flash('اسم المستخدم موجود مسبقاً', 'danger')
                return redirect(url_for('admin.users'))
            user.full_name = request.form['full_name'].strip()
            user.username = new_username
            user.role = request.form['role']
            user.branch_id = request.form.get('branch_id') or None
            user.department_id = request.form.get('department_id') or None
            if user.id != current_user.id:
                user.is_active = request.form.get('is_active') == '1'
            new_password = request.form.get('password', '').strip()
            if new_password:
                if len(new_password) < 6:
                    flash('كلمة المرور يجب أن تكون 6 أحرف على الأقل', 'danger')
                    return redirect(url_for('admin.users'))
                user.set_password(new_password)
            db.session.commit()
            flash('تم تحديث بيانات المستخدم', 'success')

        elif action == 'toggle':
            if current_user.role != 'admin':
                abort(403)
            user = User.query.get_or_404(int(request.form['user_id']))
            if user.id != current_user.id:
                user.is_active = not user.is_active
                db.session.commit()
                flash('تم تحديث حالة المستخدم', 'info')

        elif action == 'delete':
            if current_user.role != 'admin':
                abort(403)
            user = User.query.get_or_404(int(request.form['user_id']))
            if user.id == current_user.id:
                flash('لا يمكن حذف حسابك الحالي', 'danger')
            else:
                db.session.delete(user)
                db.session.commit()
                flash('تم حذف المستخدم بنجاح', 'success')

        elif action == 'reset_password':
            if current_user.role != 'admin':
                abort(403)
            user = User.query.get_or_404(int(request.form['user_id']))
            user.set_password(request.form['new_password'])
            db.session.commit()
            flash('تم تغيير كلمة المرور', 'success')

        return redirect(url_for('admin.users'))

    return render_template(
        'admin/users.html',
        users=User.query.order_by(User.full_name).all(),
        branches=Branch.query.all(),
        departments=Department.query.all(),
    )
