import os
import sys

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

_secret = os.environ.get('SECRET_KEY', 'hamam-dev-only-secret-change-me')
# Fail loudly in production if the default key is still in use
if _secret == 'hamam-dev-only-secret-change-me' and not os.environ.get('FLASK_DEBUG'):
    print(
        'WARNING: SECRET_KEY is not set. Using insecure default. '
        'Set the SECRET_KEY environment variable in production.',
        file=sys.stderr,
    )


class Config:
    SECRET_KEY = _secret

    _db_url = os.environ.get('DATABASE_URL', f"sqlite:///{os.path.join(BASE_DIR, 'inventory.db')}")
    # Render provides postgres:// but SQLAlchemy 2.x requires postgresql://
    if _db_url.startswith('postgres://'):
        _db_url = _db_url.replace('postgres://', 'postgresql://', 1)
    SQLALCHEMY_DATABASE_URI = _db_url

    SQLALCHEMY_TRACK_MODIFICATIONS = False
    WTF_CSRF_ENABLED = True
