from flask import Blueprint

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')

# Sub-modules are imported AFTER admin_bp is created to avoid circular imports.
# Each sub-module registers its routes directly on admin_bp.
from routes.admin import items, users, units, imports, sessions  # noqa: E402, F401
