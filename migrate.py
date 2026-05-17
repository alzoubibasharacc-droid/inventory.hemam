"""
Migration: add ItemUnitConversion system + item_code + multi-entry counts.

Safe to run multiple times — checks for existing columns before altering.
Run once after pulling this version:
    python migrate.py
"""
from app import create_app
from models import db

app = create_app()


def column_exists(conn, table, column):
    rows = conn.execute(db.text(f"PRAGMA table_info({table})")).fetchall()
    return any(r[1] == column for r in rows)


def run():
    with app.app_context():
        conn = db.engine.connect()
        trans = conn.begin()

        try:
            # ── 1. items: add item_code ───────────────────────────────────────
            if not column_exists(conn, 'items', 'item_code'):
                conn.execute(db.text('ALTER TABLE items ADD COLUMN item_code TEXT'))
                print('  + items.item_code')

            # ── 2. items: add base_unit_id (defaults to existing unit_id) ─────
            if not column_exists(conn, 'items', 'base_unit_id'):
                conn.execute(db.text('ALTER TABLE items ADD COLUMN base_unit_id INTEGER'))
                conn.execute(db.text('UPDATE items SET base_unit_id = unit_id'))
                print('  + items.base_unit_id  (seeded from unit_id)')

            # ── 3. inventory_counts: add entered_quantity / entered_unit_id ───
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
                # Back-fill: set entered_unit_id = item's unit_id for each existing row
                conn.execute(db.text('''
                    UPDATE inventory_counts
                    SET entered_unit_id = (
                        SELECT unit_id FROM items WHERE items.id = inventory_counts.item_id
                    )
                '''))
                print('  + inventory_counts.entered_unit_id  (seeded from item.unit_id)')

            # ── 4. Remove unique constraint on inventory_counts ───────────────
            #    SQLite can't DROP CONSTRAINT, so we recreate the table.
            #    Detect: if the old CREATE TABLE still contains the UNIQUE clause.
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

            # ── 4b. items: add packaging_note ────────────────────────────────
            if not column_exists(conn, 'items', 'packaging_note'):
                conn.execute(db.text('ALTER TABLE items ADD COLUMN packaging_note TEXT'))
                print('  + items.packaging_note')

            # ── 5. Create item_unit_conversions table ─────────────────────────
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

            # ── 6. Seed default conversion (base unit, multiplier=1) ──────────
            inserted = conn.execute(db.text('''
                INSERT OR IGNORE INTO item_unit_conversions (item_id, unit_id, multiplier)
                SELECT id, unit_id, 1.0 FROM items WHERE unit_id IS NOT NULL
            '''))
            print(f'  + seeded {inserted.rowcount} default conversions (base unit × 1)')

            trans.commit()
            print('\nMigration complete.')

        except Exception as exc:
            trans.rollback()
            print(f'\nMigration FAILED — rolled back.\n{exc}')
            raise
        finally:
            conn.close()


if __name__ == '__main__':
    print('Running migration …')
    run()
