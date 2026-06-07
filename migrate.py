"""
Migration script — safe to run multiple times.

Cumulative changes applied:
  v1  ItemUnitConversion system, item_code, multi-entry counts (SQLite only)
  v2  Performance indexes, base_unit_id backfill (SQLite + PostgreSQL)
  v3  minimum_stock column, backfilled from min_quantity (SQLite + PostgreSQL)
  v4  Unit.symbol, Unit.is_active, Unit.created_at columns
  v5  item_code unique per department, not global
  v6  items.created_at column
  v7  InventorySession table, session_id on inventory_counts, baseline sessions,
      historical data backfill, partial unique index for active sessions
  v8  session_id NOT NULL at DB level (PostgreSQL); application-layer guard for SQLite
  v9  session_departments join table; backfill baseline sessions with all branch departments
  v10 session_audit_log table for admin corrections on count quantities

Run after pulling a new version:
    python migrate.py
"""
from app import create_app
from models import db

app = create_app()


# ── Database helpers ──────────────────────────────────────────────────────────

def _is_sqlite(conn):
    return conn.engine.dialect.name == 'sqlite'


def _column_exists_sqlite(conn, table, column):
    rows = conn.execute(db.text(f'PRAGMA table_info({table})')).fetchall()
    return any(r[1] == column for r in rows)


def _column_exists_pg(conn, table, column):
    row = conn.execute(db.text(
        'SELECT 1 FROM information_schema.columns '
        'WHERE table_name = :t AND column_name = :c'
    ), {'t': table, 'c': column}).fetchone()
    return row is not None


def column_exists(conn, table, column):
    if _is_sqlite(conn):
        return _column_exists_sqlite(conn, table, column)
    return _column_exists_pg(conn, table, column)


def _table_exists(conn, table):
    if _is_sqlite(conn):
        row = conn.execute(db.text(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=:t"
        ), {'t': table}).fetchone()
    else:
        row = conn.execute(db.text(
            'SELECT 1 FROM information_schema.tables '
            'WHERE table_schema = current_schema() AND table_name = :t'
        ), {'t': table}).fetchone()
    return row is not None


def _index_exists_sqlite(conn, name):
    row = conn.execute(db.text(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=:n"
    ), {'n': name}).fetchone()
    return row is not None


def _index_exists_pg(conn, name):
    row = conn.execute(db.text(
        'SELECT 1 FROM pg_indexes WHERE indexname = :n'
    ), {'n': name}).fetchone()
    return row is not None


def index_exists(conn, name):
    if _is_sqlite(conn):
        return _index_exists_sqlite(conn, name)
    return _index_exists_pg(conn, name)


# ── Migration steps ───────────────────────────────────────────────────────────

def run_v1_sqlite(conn):
    """
    Original SQLite-only migration.
    Adds item_code, base_unit_id, audit fields, removes unique constraint,
    creates item_unit_conversions, and seeds default conversions.
    """
    if not _is_sqlite(conn):
        print('  (v1 SQLite steps skipped — running on PostgreSQL)')
        return

    # 1. items.item_code
    if not column_exists(conn, 'items', 'item_code'):
        conn.execute(db.text('ALTER TABLE items ADD COLUMN item_code TEXT'))
        print('  + items.item_code')

    # 2. items.base_unit_id
    if not column_exists(conn, 'items', 'base_unit_id'):
        conn.execute(db.text('ALTER TABLE items ADD COLUMN base_unit_id INTEGER'))
        conn.execute(db.text('UPDATE items SET base_unit_id = unit_id'))
        print('  + items.base_unit_id  (seeded from unit_id)')

    # 3. inventory_counts audit fields
    if not column_exists(conn, 'inventory_counts', 'entered_quantity'):
        conn.execute(db.text(
            'ALTER TABLE inventory_counts ADD COLUMN entered_quantity REAL'
        ))
        conn.execute(db.text(
            'UPDATE inventory_counts SET entered_quantity = quantity'
        ))
        print('  + inventory_counts.entered_quantity')

    if not column_exists(conn, 'inventory_counts', 'entered_unit_id'):
        conn.execute(db.text(
            'ALTER TABLE inventory_counts ADD COLUMN entered_unit_id INTEGER'
        ))
        conn.execute(db.text('''
            UPDATE inventory_counts
            SET entered_unit_id = (
                SELECT unit_id FROM items WHERE items.id = inventory_counts.item_id
            )
        '''))
        print('  + inventory_counts.entered_unit_id  (seeded from item.unit_id)')

    # 4. Remove unique constraint on inventory_counts (recreate table)
    schema_row = conn.execute(db.text(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='inventory_counts'"
    )).fetchone()
    if schema_row and 'UNIQUE' in (schema_row[0] or '').upper():
        print('  ~ removing unique constraint on inventory_counts …')
        conn.execute(db.text(
            'ALTER TABLE inventory_counts RENAME TO inventory_counts_old'
        ))
        conn.execute(db.text('''
            CREATE TABLE inventory_counts (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id          INTEGER NOT NULL REFERENCES items(id),
                quantity         REAL    NOT NULL,
                entered_quantity REAL,
                entered_unit_id  INTEGER REFERENCES units(id),
                count_date       DATE    NOT NULL,
                month            INTEGER NOT NULL,
                year             INTEGER NOT NULL,
                user_id          INTEGER NOT NULL REFERENCES users(id),
                notes            TEXT,
                created_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at       DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        '''))
        conn.execute(db.text('''
            INSERT INTO inventory_counts
                (id, item_id, quantity, entered_quantity, entered_unit_id,
                 count_date, month, year, user_id, notes, created_at, updated_at)
            SELECT
                id, item_id, quantity,
                COALESCE(entered_quantity, quantity),
                COALESCE(entered_unit_id,
                    (SELECT unit_id FROM items WHERE items.id = item_id)),
                count_date, month, year, user_id, notes, created_at, updated_at
            FROM inventory_counts_old
        '''))
        conn.execute(db.text('DROP TABLE inventory_counts_old'))
        print('  ~ unique constraint removed, data preserved')
    else:
        print('  = inventory_counts unique constraint already removed')

    # 4b. items.packaging_note
    if not column_exists(conn, 'items', 'packaging_note'):
        conn.execute(db.text('ALTER TABLE items ADD COLUMN packaging_note TEXT'))
        print('  + items.packaging_note')

    # 5. item_unit_conversions table
    conn.execute(db.text('''
        CREATE TABLE IF NOT EXISTS item_unit_conversions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id     INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            unit_id     INTEGER NOT NULL REFERENCES units(id),
            multiplier  REAL    NOT NULL DEFAULT 1.0,
            UNIQUE(item_id, unit_id)
        )
    '''))
    print('  + item_unit_conversions table (or already exists)')

    # 6. Seed base-unit conversion (multiplier=1) for any item that has none
    inserted = conn.execute(db.text('''
        INSERT OR IGNORE INTO item_unit_conversions (item_id, unit_id, multiplier)
        SELECT id, unit_id, 1.0 FROM items WHERE unit_id IS NOT NULL
    '''))
    print(f'  + seeded {inserted.rowcount} default conversions (base unit × 1)')


def run_v2(conn):
    """
    v2: Performance indexes + base_unit_id backfill. SQLite + PostgreSQL safe.
    """
    # ── Backfill base_unit_id from unit_id where still NULL ──────────────────
    result = conn.execute(db.text(
        'UPDATE items SET base_unit_id = unit_id '
        'WHERE base_unit_id IS NULL AND unit_id IS NOT NULL'
    ))
    if result.rowcount:
        print(f'  + backfilled items.base_unit_id for {result.rowcount} rows')
    else:
        print('  = items.base_unit_id already backfilled')

    # ── Performance indexes ───────────────────────────────────────────────────
    # CREATE INDEX IF NOT EXISTS is supported by SQLite 3.3.0+ and PostgreSQL 9.5+
    indexes = [
        ('ix_inv_counts_month_year',      'inventory_counts', '(month, year)'),
        ('ix_inv_counts_item_month_year',  'inventory_counts', '(item_id, month, year)'),
        ('ix_items_department_id',         'items',            '(department_id)'),
        ('ix_inv_counts_count_date',       'inventory_counts', '(count_date)'),
    ]
    for idx_name, table, cols in indexes:
        if not index_exists(conn, idx_name):
            conn.execute(db.text(
                f'CREATE INDEX IF NOT EXISTS {idx_name} ON {table} {cols}'
            ))
            print(f'  + index {idx_name}')
        else:
            print(f'  = index {idx_name} already exists')


def run_v3(conn):
    """
    v3: Add minimum_stock column, backfill from min_quantity.
    Safe to run on SQLite and PostgreSQL.
    """
    if not column_exists(conn, 'items', 'minimum_stock'):
        conn.execute(db.text(
            'ALTER TABLE items ADD COLUMN minimum_stock FLOAT DEFAULT 0'
        ))
        conn.execute(db.text(
            'UPDATE items SET minimum_stock = COALESCE(min_quantity, 0)'
        ))
        print('  + items.minimum_stock  (backfilled from min_quantity)')
    else:
        # Backfill any NULLs that may have slipped through
        result = conn.execute(db.text(
            'UPDATE items SET minimum_stock = COALESCE(min_quantity, 0) '
            'WHERE minimum_stock IS NULL'
        ))
        if result.rowcount:
            print(f'  + backfilled minimum_stock for {result.rowcount} NULL rows')
        else:
            print('  = items.minimum_stock already present')


def run_v4(conn):
    """
    v4: Add symbol, is_active, created_at to units table.
    Safe to run on SQLite and PostgreSQL.
    """
    if not column_exists(conn, 'units', 'symbol'):
        conn.execute(db.text('ALTER TABLE units ADD COLUMN symbol VARCHAR(20)'))
        conn.execute(db.text('UPDATE units SET symbol = name_en'))
        print('  + units.symbol  (seeded from name_en)')
    else:
        print('  = units.symbol already present')

    if not column_exists(conn, 'units', 'is_active'):
        col_type = 'BOOLEAN DEFAULT TRUE' if not _is_sqlite(conn) else 'INTEGER DEFAULT 1'
        conn.execute(db.text(f'ALTER TABLE units ADD COLUMN is_active {col_type}'))
        conn.execute(db.text('UPDATE units SET is_active = TRUE WHERE is_active IS NULL'))
        print('  + units.is_active  (all existing set to active)')
    elif not _is_sqlite(conn):
        col_type_row = conn.execute(db.text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'units' AND column_name = 'is_active'"
        )).fetchone()
        if col_type_row and col_type_row[0] != 'boolean':
            conn.execute(db.text('ALTER TABLE units ALTER COLUMN is_active DROP DEFAULT'))
            conn.execute(db.text('ALTER TABLE units ALTER COLUMN is_active TYPE BOOLEAN USING is_active::boolean'))
            conn.execute(db.text('ALTER TABLE units ALTER COLUMN is_active SET DEFAULT TRUE'))
            print('  ~ units.is_active converted INTEGER → BOOLEAN')
        else:
            print('  = units.is_active already present')
    else:
        print('  = units.is_active already present')

    if not column_exists(conn, 'units', 'created_at'):
        col_type = 'TIMESTAMP' if not _is_sqlite(conn) else 'DATETIME'
        conn.execute(db.text(f'ALTER TABLE units ADD COLUMN created_at {col_type}'))
        conn.execute(db.text('UPDATE units SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL'))
        print('  + units.created_at')
    else:
        print('  = units.created_at already present')


def run_v5(conn):
    """
    v5: Change item_code uniqueness from global to per-department.
    The same code is now allowed in different sections/branches.
    """
    if _is_sqlite(conn):
        # SQLite stores the UNIQUE constraint in the CREATE TABLE DDL.
        # The only way to drop a column-level UNIQUE in SQLite is to recreate the table.
        schema_row = conn.execute(db.text(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='items'"
        )).fetchone()
        ddl = (schema_row[0] or '') if schema_row else ''

        if 'item_code' in ddl and 'UNIQUE' in ddl.upper():
            print('  ~ rebuilding items table to change item_code uniqueness (SQLite) …')
            conn.execute(db.text('ALTER TABLE items RENAME TO _items_v5_old'))
            conn.execute(db.text('''
                CREATE TABLE items (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_code      VARCHAR(50),
                    name_ar        VARCHAR(200) NOT NULL,
                    name_en        VARCHAR(200) NOT NULL,
                    packaging_note VARCHAR(300),
                    unit_id        INTEGER REFERENCES units(id),
                    base_unit_id   INTEGER REFERENCES units(id),
                    category_id    INTEGER REFERENCES categories(id),
                    department_id  INTEGER NOT NULL REFERENCES departments(id),
                    min_quantity   FLOAT DEFAULT 0,
                    minimum_stock  FLOAT DEFAULT 0,
                    is_active      INTEGER DEFAULT 1
                )
            '''))
            conn.execute(db.text('''
                INSERT INTO items
                    (id, item_code, name_ar, name_en, packaging_note,
                     unit_id, base_unit_id, category_id, department_id,
                     min_quantity, minimum_stock, is_active)
                SELECT
                    id, item_code, name_ar, name_en, packaging_note,
                    unit_id, base_unit_id, category_id, department_id,
                    min_quantity, minimum_stock, is_active
                FROM _items_v5_old
            '''))
            conn.execute(db.text('DROP TABLE _items_v5_old'))
            print('  + items table rebuilt without global item_code UNIQUE')
        else:
            print('  = items.item_code global UNIQUE already absent')

        # Add composite unique index (per-department)
        if not index_exists(conn, 'uq_items_code_dept'):
            conn.execute(db.text(
                'CREATE UNIQUE INDEX uq_items_code_dept '
                'ON items (item_code, department_id) '
                'WHERE item_code IS NOT NULL'
            ))
            print('  + uq_items_code_dept (item_code unique per department)')
        else:
            print('  = uq_items_code_dept already exists')

        # Restore performance index (lost during table rebuild)
        if not index_exists(conn, 'ix_items_department_id'):
            conn.execute(db.text(
                'CREATE INDEX ix_items_department_id ON items (department_id)'
            ))
            print('  + ix_items_department_id restored')

    else:
        # PostgreSQL — drop old global constraint, add per-department one
        for old in ('items_item_code_key', 'uq_items_item_code'):
            conn.execute(db.text(
                f'ALTER TABLE items DROP CONSTRAINT IF EXISTS {old}'
            ))
        print('  ~ dropped global item_code constraint (if existed)')

        if not index_exists(conn, 'uq_items_code_dept'):
            conn.execute(db.text(
                'CREATE UNIQUE INDEX uq_items_code_dept '
                'ON items (item_code, department_id) '
                'WHERE item_code IS NOT NULL'
            ))
            print('  + uq_items_code_dept (item_code unique per department)')
        else:
            print('  = uq_items_code_dept already exists')


def run_v6(conn):
    """
    v6: Add created_at to items table.
    Safe to run on SQLite and PostgreSQL.
    """
    if not column_exists(conn, 'items', 'created_at'):
        col_type = 'TIMESTAMP' if not _is_sqlite(conn) else 'DATETIME'
        conn.execute(db.text(f'ALTER TABLE items ADD COLUMN created_at {col_type}'))
        conn.execute(db.text(
            'UPDATE items SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL'
        ))
        print('  + items.created_at  (backfilled with CURRENT_TIMESTAMP)')
    else:
        print('  = items.created_at already present')


def run_v7(conn):
    """
    v7: Inventory Session foundation.

    Steps (all idempotent):
      1. Create inventory_sessions table.
      2. Add session_id (nullable FK) to inventory_counts.
      3. Add ix_inv_counts_session_id performance index.
      4. Create one Baseline session per branch (idempotent — skips existing).
      5. Backfill session_id on all orphan inventory_counts.
      6. Add partial unique index: only one active session per branch.
      7. Verify zero orphan counts remain.
    """
    sqlite = _is_sqlite(conn)

    # ── 1. Create inventory_sessions table ───────────────────────────────────
    if not _table_exists(conn, 'inventory_sessions'):
        if sqlite:
            conn.execute(db.text('''
                CREATE TABLE inventory_sessions (
                    id           INTEGER  PRIMARY KEY AUTOINCREMENT,
                    name         VARCHAR(200) NOT NULL,
                    branch_id    INTEGER  NOT NULL REFERENCES branches(id),
                    session_type VARCHAR(20)  NOT NULL DEFAULT 'official',
                    status       VARCHAR(20)  NOT NULL DEFAULT 'draft',
                    count_date   DATE         NOT NULL,
                    notes        TEXT,
                    created_by   INTEGER  NOT NULL REFERENCES users(id),
                    created_at   DATETIME,
                    opened_at    DATETIME,
                    closed_at    DATETIME
                )
            '''))
        else:
            conn.execute(db.text('''
                CREATE TABLE inventory_sessions (
                    id           SERIAL       PRIMARY KEY,
                    name         VARCHAR(200) NOT NULL,
                    branch_id    INTEGER      NOT NULL REFERENCES branches(id),
                    session_type VARCHAR(20)  NOT NULL DEFAULT 'official',
                    status       VARCHAR(20)  NOT NULL DEFAULT 'draft',
                    count_date   DATE         NOT NULL,
                    notes        TEXT,
                    created_by   INTEGER      NOT NULL REFERENCES users(id),
                    created_at   TIMESTAMP,
                    opened_at    TIMESTAMP,
                    closed_at    TIMESTAMP
                )
            '''))
        print('  + inventory_sessions table created')
    else:
        print('  = inventory_sessions table already exists')

    # ── 2. Add session_id (nullable) to inventory_counts ─────────────────────
    if not column_exists(conn, 'inventory_counts', 'session_id'):
        conn.execute(db.text(
            'ALTER TABLE inventory_counts '
            'ADD COLUMN session_id INTEGER REFERENCES inventory_sessions(id)'
        ))
        print('  + inventory_counts.session_id (nullable FK)')
    else:
        print('  = inventory_counts.session_id already present')

    # ── 3. Performance index on session_id ───────────────────────────────────
    if not index_exists(conn, 'ix_inv_counts_session_id'):
        conn.execute(db.text(
            'CREATE INDEX ix_inv_counts_session_id '
            'ON inventory_counts (session_id)'
        ))
        print('  + ix_inv_counts_session_id')
    else:
        print('  = ix_inv_counts_session_id already exists')

    # ── 4. Resolve created_by: first admin, else first user ──────────────────
    admin_row = conn.execute(db.text(
        "SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1"
    )).fetchone()
    if not admin_row:
        admin_row = conn.execute(db.text(
            'SELECT id FROM users ORDER BY id LIMIT 1'
        )).fetchone()
    if not admin_row:
        print('  ! No users found — skipping baseline session creation')
        return
    created_by_id = admin_row[0]

    # ── 5. Create one Baseline session per branch (idempotent) ───────────────
    branches = conn.execute(db.text(
        'SELECT id, name_ar FROM branches ORDER BY id'
    )).fetchall()

    created_count = 0
    for branch_id, name_ar in branches:
        existing = conn.execute(db.text(
            "SELECT id FROM inventory_sessions "
            "WHERE branch_id = :b AND session_type = 'baseline' LIMIT 1"
        ), {'b': branch_id}).fetchone()

        if existing:
            continue

        session_name = f'جرد أساسي — {name_ar}'
        conn.execute(db.text('''
            INSERT INTO inventory_sessions
                (name, branch_id, session_type, status, count_date,
                 notes, created_by, created_at)
            VALUES
                (:name, :branch_id, 'baseline', 'completed', '2026-05-31',
                 'سجل تاريخي — تم إنشاؤه تلقائياً عند تفعيل نظام الجلسات',
                 :created_by, CURRENT_TIMESTAMP)
        '''), {
            'name':       session_name,
            'branch_id':  branch_id,
            'created_by': created_by_id,
        })
        created_count += 1

    if created_count:
        print(f'  + created {created_count} baseline session(s)')
    else:
        print('  = baseline sessions already exist for all branches')

    # ── 6. Backfill session_id on orphan inventory_counts ────────────────────
    if sqlite:
        # SQLite does not support UPDATE … FROM; use correlated subquery instead.
        result = conn.execute(db.text('''
            UPDATE inventory_counts
            SET session_id = (
                SELECT s.id
                FROM inventory_sessions s,
                     items              i,
                     departments        d
                WHERE i.id        = inventory_counts.item_id
                  AND d.id        = i.department_id
                  AND s.branch_id = d.branch_id
                  AND s.session_type = 'baseline'
                LIMIT 1
            )
            WHERE session_id IS NULL
        '''))
    else:
        # PostgreSQL UPDATE … FROM with comma-style joins.
        # The target table alias cannot appear inside FROM clause JOINs in PG,
        # so we list all tables in FROM and correlate via WHERE instead.
        result = conn.execute(db.text('''
            UPDATE inventory_counts
            SET    session_id = s.id
            FROM   inventory_sessions s,
                   items              i,
                   departments        d
            WHERE  i.id            = inventory_counts.item_id
              AND  d.id            = i.department_id
              AND  s.branch_id     = d.branch_id
              AND  s.session_type  = 'baseline'
              AND  inventory_counts.session_id IS NULL
        '''))
    print(f'  + backfilled session_id on {result.rowcount} count record(s)')

    # ── 7. Partial unique index: max one active session per branch ────────────
    # Syntax is identical on SQLite 3.8.9+ and PostgreSQL 8.2+.
    if not index_exists(conn, 'uq_one_active_session_per_branch'):
        conn.execute(db.text(
            "CREATE UNIQUE INDEX uq_one_active_session_per_branch "
            "ON inventory_sessions (branch_id) "
            "WHERE status = 'active'"
        ))
        print('  + uq_one_active_session_per_branch (partial unique index)')
    else:
        print('  = uq_one_active_session_per_branch already exists')

    # ── 8. Safety check: no orphan counts ────────────────────────────────────
    orphan_row = conn.execute(db.text(
        'SELECT COUNT(*) FROM inventory_counts WHERE session_id IS NULL'
    )).fetchone()
    orphan_count = orphan_row[0] if orphan_row else 0
    if orphan_count:
        raise RuntimeError(
            f'v7 safety check FAILED: {orphan_count} inventory_count row(s) '
            'still have session_id = NULL after backfill. '
            'Likely cause: count records linked to items whose department has '
            'no matching branch, or items with no department. '
            'Investigate before retrying.'
        )
    print('  ✓ safety check passed — zero orphan count records')


def run_v8(conn):
    """
    Make inventory_counts.session_id NOT NULL at the database level.

    Prerequisites: v7 must have backfilled every row (no NULLs may remain).
    SQLite does not support ALTER COLUMN — constraint is enforced at the
    application layer (before_insert / before_update listeners) and via
    PRAGMA foreign_keys = ON per connection.
    """
    # Safety pre-check — abort before touching any DDL
    null_row = conn.execute(db.text(
        'SELECT COUNT(*) FROM inventory_counts WHERE session_id IS NULL'
    )).fetchone()
    null_count = null_row[0] if null_row else 0
    if null_count:
        raise RuntimeError(
            f'v8 aborted: {null_count} inventory_count row(s) still have '
            'session_id = NULL. Ensure v7 backfill completed successfully.'
        )
    print('  ✓ pre-check passed — no NULL session_id rows')

    if _is_sqlite(conn):
        print('  = SQLite: ALTER COLUMN not supported — '
              'NOT NULL enforced at application layer')
        return

    # PostgreSQL: check current nullability before issuing DDL
    row = conn.execute(db.text(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name = 'inventory_counts' AND column_name = 'session_id'"
    )).fetchone()
    if row and row[0] == 'NO':
        print('  = session_id already NOT NULL on PostgreSQL')
    else:
        conn.execute(db.text(
            'ALTER TABLE inventory_counts '
            'ALTER COLUMN session_id SET NOT NULL'
        ))
        print('  + session_id SET NOT NULL on PostgreSQL')


def run_v9(conn):
    """
    v9: session_departments — department scope per InventorySession.

    Steps (all idempotent):
      1. Create session_departments table with unique(session_id, department_id).
      2. Backfill baseline sessions with every department in their branch.
    """
    sqlite = _is_sqlite(conn)

    # ── 1. Create table ───────────────────────────────────────────────────────
    if not _table_exists(conn, 'session_departments'):
        if sqlite:
            conn.execute(db.text('''
                CREATE TABLE session_departments (
                    id            INTEGER  PRIMARY KEY AUTOINCREMENT,
                    session_id    INTEGER  NOT NULL
                                  REFERENCES inventory_sessions(id) ON DELETE CASCADE,
                    department_id INTEGER  NOT NULL REFERENCES departments(id),
                    created_at    DATETIME,
                    UNIQUE(session_id, department_id)
                )
            '''))
        else:
            conn.execute(db.text('''
                CREATE TABLE session_departments (
                    id            SERIAL    PRIMARY KEY,
                    session_id    INTEGER   NOT NULL
                                  REFERENCES inventory_sessions(id) ON DELETE CASCADE,
                    department_id INTEGER   NOT NULL REFERENCES departments(id),
                    created_at    TIMESTAMP,
                    UNIQUE(session_id, department_id)
                )
            '''))
        print('  + session_departments table created')
    else:
        print('  = session_departments table already exists')

    # ── 2. Backfill baseline sessions with all branch departments ─────────────
    baselines = conn.execute(db.text(
        "SELECT id, branch_id FROM inventory_sessions WHERE session_type = 'baseline'"
    )).fetchall()

    inserted_total = 0
    for session_id, branch_id in baselines:
        dept_rows = conn.execute(db.text(
            'SELECT id FROM departments WHERE branch_id = :b ORDER BY id'
        ), {'b': branch_id}).fetchall()
        for (dept_id,) in dept_rows:
            existing = conn.execute(db.text(
                'SELECT 1 FROM session_departments '
                'WHERE session_id = :s AND department_id = :d'
            ), {'s': session_id, 'd': dept_id}).fetchone()
            if not existing:
                conn.execute(db.text(
                    'INSERT INTO session_departments '
                    '(session_id, department_id, created_at) '
                    'VALUES (:s, :d, CURRENT_TIMESTAMP)'
                ), {'s': session_id, 'd': dept_id})
                inserted_total += 1

    if inserted_total:
        print(f'  + backfilled {inserted_total} department assignment(s) for baseline sessions')
    else:
        print('  = baseline session departments already assigned')


def run_v10(conn):
    """Create session_audit_log table for admin corrections on count quantities."""
    if _table_exists(conn, 'session_audit_log'):
        print('  = session_audit_log already exists')
        return

    if _is_sqlite(conn):
        conn.execute(db.text('''
            CREATE TABLE session_audit_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id    INTEGER NOT NULL REFERENCES inventory_sessions(id) ON DELETE CASCADE,
                item_id       INTEGER REFERENCES items(id) ON DELETE SET NULL,
                changed_by    INTEGER NOT NULL REFERENCES users(id),
                changed_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                field_changed VARCHAR(100) NOT NULL DEFAULT \'quantity\',
                old_value     TEXT,
                new_value     TEXT,
                reason        TEXT
            )
        '''))
        conn.execute(db.text(
            'CREATE INDEX ix_audit_log_session_id ON session_audit_log(session_id)'
        ))
    else:
        conn.execute(db.text('''
            CREATE TABLE session_audit_log (
                id            SERIAL PRIMARY KEY,
                session_id    INTEGER NOT NULL REFERENCES inventory_sessions(id) ON DELETE CASCADE,
                item_id       INTEGER REFERENCES items(id) ON DELETE SET NULL,
                changed_by    INTEGER NOT NULL REFERENCES users(id),
                changed_at    TIMESTAMP NOT NULL DEFAULT NOW(),
                field_changed VARCHAR(100) NOT NULL DEFAULT \'quantity\',
                old_value     TEXT,
                new_value     TEXT,
                reason        TEXT
            )
        '''))
        conn.execute(db.text(
            'CREATE INDEX ix_audit_log_session_id ON session_audit_log(session_id)'
        ))

    print('  + created session_audit_log table')


# ── Entry point ───────────────────────────────────────────────────────────────

def run():
    with app.app_context():
        conn = db.engine.connect()
        trans = conn.begin()
        try:
            print('\n-- v1 (ItemUnitConversion + audit fields) --')
            run_v1_sqlite(conn)

            print('\n-- v2 (indexes + base_unit_id backfill) --')
            run_v2(conn)

            print('\n-- v3 (minimum_stock column) --')
            run_v3(conn)

            print('\n-- v4 (Unit.symbol / is_active / created_at) --')
            run_v4(conn)

            print('\n-- v5 (item_code unique per department, not global) --')
            run_v5(conn)

            print('\n-- v6 (items.created_at) --')
            run_v6(conn)

            print('\n-- v7 (InventorySession table, baseline sessions, backfill) --')
            run_v7(conn)

            print('\n-- v8 (session_id NOT NULL constraint) --')
            run_v8(conn)

            print('\n-- v9 (session_departments table + baseline backfill) --')
            run_v9(conn)

            print('\n-- v10 (session_audit_log table) --')
            run_v10(conn)

            trans.commit()
            print('\nMigration complete.\n')
        except Exception as exc:
            trans.rollback()
            print(f'\nMigration FAILED — rolled back.\n{exc}')
            raise
        finally:
            conn.close()


if __name__ == '__main__':
    print('Running migration …')
    run()
