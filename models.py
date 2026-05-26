from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timezone


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)

db = SQLAlchemy()


class Branch(db.Model):
    __tablename__ = 'branches'
    id = db.Column(db.Integer, primary_key=True)
    name_ar = db.Column(db.String(100), nullable=False)
    name_en = db.Column(db.String(100), nullable=False)
    departments = db.relationship('Department', backref='branch', lazy=True)
    users = db.relationship('User', backref='branch', lazy=True)


class Department(db.Model):
    __tablename__ = 'departments'
    id = db.Column(db.Integer, primary_key=True)
    name_ar = db.Column(db.String(100), nullable=False)
    name_en = db.Column(db.String(100), nullable=False)
    branch_id = db.Column(db.Integer, db.ForeignKey('branches.id'), nullable=False)
    items = db.relationship('Item', backref='department', lazy=True)


class Unit(db.Model):
    __tablename__ = 'units'
    id = db.Column(db.Integer, primary_key=True)
    name_ar = db.Column(db.String(50), nullable=False)
    name_en = db.Column(db.String(50), nullable=False)
    symbol = db.Column(db.String(20), nullable=True)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=_now)
    conversions = db.relationship(
        'UnitConversion', foreign_keys='UnitConversion.from_unit_id',
        backref='from_unit', lazy=True
    )


class UnitConversion(db.Model):
    """Global unit conversions (used by the converter widget only)."""
    __tablename__ = 'unit_conversions'
    id = db.Column(db.Integer, primary_key=True)
    from_unit_id = db.Column(db.Integer, db.ForeignKey('units.id'), nullable=False)
    to_unit_id = db.Column(db.Integer, db.ForeignKey('units.id'), nullable=False)
    factor = db.Column(db.Float, nullable=False)
    to_unit = db.relationship('Unit', foreign_keys=[to_unit_id])


class Category(db.Model):
    __tablename__ = 'categories'
    id = db.Column(db.Integer, primary_key=True)
    name_ar = db.Column(db.String(100), nullable=False)
    name_en = db.Column(db.String(100), nullable=False)
    items = db.relationship('Item', backref='category', lazy=True)


class ItemUnitConversion(db.Model):
    """
    Per-item unit conversion rules.
    multiplier = how many BASE UNITS equal 1 of this unit.

    Example (item=Hummus, base_unit=gram):
      gram       → multiplier=1
      kilogram   → multiplier=1000

    Example (item=Pepsi, base_unit=piece):
      piece      → multiplier=1
      pack       → multiplier=6
      carton     → multiplier=24
    """
    __tablename__ = 'item_unit_conversions'
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(
        db.Integer, db.ForeignKey('items.id', ondelete='CASCADE'), nullable=False
    )
    unit_id = db.Column(db.Integer, db.ForeignKey('units.id'), nullable=False)
    multiplier = db.Column(db.Float, nullable=False, default=1.0)
    unit = db.relationship('Unit', lazy='joined')

    __table_args__ = (
        db.UniqueConstraint('item_id', 'unit_id', name='uq_item_unit_conv'),
    )


class Item(db.Model):
    __tablename__ = 'items'

    id = db.Column(db.Integer, primary_key=True)
    # item_code is visible to admin/manager ONLY — never shown to employees
    item_code = db.Column(db.String(50), nullable=True)
    name_ar = db.Column(db.String(200), nullable=False)
    name_en = db.Column(db.String(200), nullable=False)
    # packaging_note is visible to ALL users during counting
    packaging_note = db.Column(db.String(300), nullable=True)

    # unit_id: legacy field kept for backward compatibility.
    # Do NOT use in new code — use base_unit_id instead.
    # Will be removed in a future migration after full data validation.
    unit_id = db.Column(db.Integer, db.ForeignKey('units.id'), nullable=True)

    # base_unit_id: single authoritative storage unit for all inventory quantities.
    # All InventoryCount.quantity values are stored in this unit.
    base_unit_id = db.Column(db.Integer, db.ForeignKey('units.id'), nullable=True)

    category_id = db.Column(db.Integer, db.ForeignKey('categories.id'), nullable=True)
    department_id = db.Column(db.Integer, db.ForeignKey('departments.id'), nullable=False)
    min_quantity  = db.Column(db.Float, default=0)        # legacy — superseded by minimum_stock
    minimum_stock = db.Column(db.Float, default=0.0)      # authoritative low-stock threshold
    is_active = db.Column(db.Boolean, default=True)

    unit = db.relationship('Unit', foreign_keys=[unit_id], backref='items')
    base_unit = db.relationship('Unit', foreign_keys=[base_unit_id], backref='base_items')
    counts = db.relationship('InventoryCount', backref='item', lazy=True)
    conversions = db.relationship(
        'ItemUnitConversion',
        cascade='all, delete-orphan',
        order_by='ItemUnitConversion.multiplier',
        lazy=True,
        foreign_keys='ItemUnitConversion.item_id'
    )

    __table_args__ = (
        db.Index('ix_items_department_id', 'department_id'),
        # item_code unique per department — same code is allowed in different sections
        db.UniqueConstraint('item_code', 'department_id', name='uq_items_code_dept'),
    )

    @property
    def effective_base_unit(self):
        """Authoritative storage unit. Falls back to legacy unit if base not set."""
        return self.base_unit or self.unit

    @property
    def effective_base_unit_id(self):
        return self.base_unit_id or self.unit_id

    @property
    def effective_minimum_stock(self):
        """Returns minimum_stock, treating NULL as 0."""
        return self.minimum_stock or 0.0

    def get_multiplier(self, unit_id):
        """Return multiplier for a given unit_id (base-units per 1 of that unit)."""
        for conv in self.conversions:
            if conv.unit_id == unit_id:
                return conv.multiplier
        return 1.0


class InventoryCount(db.Model):
    """
    Each row is ONE count entry (employees may add multiple per item per month).
    quantity          → always stored in base units (after conversion).
    entered_quantity  → what the employee typed (preserved for audit).
    entered_unit_id   → which unit the employee selected.
    """
    __tablename__ = 'inventory_counts'

    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey('items.id'), nullable=False)

    # Internal storage — always in item's base unit
    quantity = db.Column(db.Float, nullable=False)

    # Audit fields — what the employee actually entered
    entered_quantity = db.Column(db.Float, nullable=True)
    entered_unit_id = db.Column(db.Integer, db.ForeignKey('units.id'), nullable=True)

    count_date = db.Column(db.Date, nullable=False, default=lambda: _now().date())
    month = db.Column(db.Integer, nullable=False)
    year = db.Column(db.Integer, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    user = db.relationship('User', backref='counts')
    entered_unit = db.relationship('Unit', foreign_keys=[entered_unit_id])

    # NOTE: No unique constraint — multiple entries per item per month are intentional.

    __table_args__ = (
        db.Index('ix_inv_counts_month_year', 'month', 'year'),
        db.Index('ix_inv_counts_item_month_year', 'item_id', 'month', 'year'),
        db.Index('ix_inv_counts_count_date', 'count_date'),
    )


class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    # username has a unique=True constraint which creates an implicit index in both
    # SQLite and PostgreSQL — no separate index needed.
    username = db.Column(db.String(80), unique=True, nullable=False)
    full_name = db.Column(db.String(150), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default='employee')
    branch_id = db.Column(db.Integer, db.ForeignKey('branches.id'), nullable=True)
    department_id = db.Column(db.Integer, db.ForeignKey('departments.id'), nullable=True)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=_now)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_admin(self):
        return self.role == 'admin'

    @property
    def is_manager(self):
        return self.role in ('admin', 'manager')
