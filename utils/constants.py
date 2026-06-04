from datetime import datetime
from zoneinfo import ZoneInfo

_JORDAN = ZoneInfo('Asia/Amman')


def now_jordan() -> datetime:
    """Return current naive datetime in Asia/Amman timezone."""
    return datetime.now(_JORDAN).replace(tzinfo=None)


MONTHS_AR = [
    'يناير', 'فبراير', 'مارس', 'أبريل', 'مايو', 'يونيو',
    'يوليو', 'أغسطس', 'سبتمبر', 'أكتوبر', 'نوفمبر', 'ديسمبر',
]
