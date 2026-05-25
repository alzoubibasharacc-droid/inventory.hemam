from flask import render_template, request, redirect, url_for, flash
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
            if User.query.filter_by(username=request.form['username']).first():
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

        elif action == 'toggle':
            user = User.query.get_or_404(int(request.form['user_id']))
            if user.id != current_user.id:
                user.is_active = not user.is_active
                db.session.commit()
                flash('تم تحديث حالة المستخدم', 'info')

        elif action == 'reset_password':
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
