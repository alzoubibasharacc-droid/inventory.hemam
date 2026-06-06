from datetime import datetime
from zoneinfo import ZoneInfo

_JORDAN = ZoneInfo('Asia/Amman')


def now_jordan() -> datetime:
    """Return current naive datetime in Asia/Amman timezone."""
    return datetime.now(_JORDAN).replace(tzinfo=None)


def get_active_session(branch_id: int):
    """Return the active InventorySession for branch_id, or None."""
    from models import InventorySession
    return (
        InventorySession.query
        .filter_by(branch_id=branch_id, status='active')
        .first()
    )


def activate_session(inv_session) -> None:
    """Thin wrapper — delegates to InventorySession.open(). Caller must commit."""
    inv_session.open()


def resolve_session_for_branch(branch_id: int):
    """
    Return the InventorySession that should receive new counts for branch_id.

    Priority:
      1. The single active session for that branch (status='active').
      2. The most-recently-created baseline session for that branch.

    Raises RuntimeError if neither exists — callers must not create counts
    without a session_id.
    """
    from models import InventorySession  # local import to avoid circular deps

    active = (
        InventorySession.query
        .filter_by(branch_id=branch_id, status='active')
        .first()
    )
    if active:
        return active

    baseline = (
        InventorySession.query
        .filter_by(branch_id=branch_id, session_type='baseline')
        .order_by(InventorySession.id.desc())
        .first()
    )
    if baseline:
        return baseline

    raise RuntimeError(
        f'لا توجد جلسة جرد نشطة أو أساسية للفرع {branch_id}. '
        'يرجى إنشاء جلسة جرد أولاً.'
    )


MONTHS_AR = [
    'يناير', 'فبراير', 'مارس', 'أبريل', 'مايو', 'يونيو',
    'يوليو', 'أغسطس', 'سبتمبر', 'أكتوبر', 'نوفمبر', 'ديسمبر',
]
