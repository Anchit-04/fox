import sqlite3
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql")

MIGRATIONS: list[str] = [
    SCHEMA.read_text(encoding="utf-8"),
]


class StoreError(Exception):
    pass


def version(db: sqlite3.Connection) -> int:
    return db.execute("PRAGMA user_version").fetchone()[0]


def migrate(db: sqlite3.Connection) -> int:
    current = version(db)
    if current > len(MIGRATIONS):
        raise StoreError(f"fox.db is at schema version {current}, but this Fox only understands up to "
                         f"{len(MIGRATIONS)}. Update Fox before opening it.")
    for number in range(current + 1, len(MIGRATIONS) + 1):
        script = f"BEGIN;\n{MIGRATIONS[number - 1]}\nPRAGMA user_version = {number};\nCOMMIT;"
        try:
            db.executescript(script)
        except sqlite3.Error:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
    return version(db)
