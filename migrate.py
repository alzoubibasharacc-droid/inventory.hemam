"""
Migration script — safe to run multiple times.

Cumulative changes applied:
  v1  ItemUnitConversion system, item_code, multi-entry counts (SQLite only)
  v2  Performance indexes, base_unit_id backfill (SQLite + PostgreSQL)
  v3  minimum_stock column, backfilled from min_quantity (SQLite + PostgreSQL)
  v4  Unit.symbol, Unit.is_active, Unit.created_at columns

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
