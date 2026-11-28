"""SQLite store: the current product list plus one observation per product per sync.

Observations are what make price drops, new products and restocks visible.
"""

import os
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

DB_PATH_ENV = "VOLAR_DB"
DEFAULT_DB_PATH = Path.home() / ".local" / "share" / "itau-volar" / "volar.db"


class Source(StrEnum):
    TIENDA = "tienda"
    VOLAR = "volar"
    ML = "ml"  # Mercado Libre: imported from saved pages (`volar import`), never synced


SYNCABLE_SOURCES = (Source.TIENDA, Source.VOLAR)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    source TEXT NOT NULL,
    id INTEGER NOT NULL,
    name TEXT NOT NULL,
    category TEXT,
    url TEXT,
    currency TEXT,
    price REAL,              -- cash price; NULL for miles-only products
    miles INTEGER,           -- volar: full cost in miles; tienda: miles in the card's split
    price_with_miles REAL,   -- tienda: cash left to pay when using `miles`
    in_stock INTEGER NOT NULL DEFAULT 1,
    badges TEXT,
    seller TEXT,             -- ml only
    condition TEXT,          -- ml only: Nuevo / Usado / Reacondicionado, when the page says
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    PRIMARY KEY (source, id)
);
CREATE TABLE IF NOT EXISTS runs (
    run_id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    scope TEXT NOT NULL,     -- what was synced, e.g. 'category:257'; runs compare within a scope
    started_at TEXT NOT NULL,
    product_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS observations (
    run_id INTEGER NOT NULL REFERENCES runs(run_id),
    source TEXT NOT NULL,
    id INTEGER NOT NULL,
    price REAL,
    miles INTEGER,
    in_stock INTEGER NOT NULL,
    PRIMARY KEY (run_id, source, id)
);
"""

# Columns added after the first release: `CREATE TABLE IF NOT EXISTS` skips existing
# databases, so these get added with ALTER TABLE when missing.
_ADDED_PRODUCT_COLUMNS = {"seller": "TEXT", "condition": "TEXT"}


@dataclass(frozen=True, slots=True)
class Product:
    source: Source
    id: int
    name: str
    category: str | None = None
    url: str | None = None
    currency: str | None = None
    price: float | None = None
    miles: int | None = None
    price_with_miles: float | None = None
    in_stock: bool = True
    badges: str | None = None
    seller: str | None = None
    condition: str | None = None


def db_path() -> Path:
    return Path(os.environ.get(DB_PATH_ENV) or DEFAULT_DB_PATH)


def fold(text: str | None) -> str:
    """Lowercase and strip accents, so 'tecnologia' matches 'Tecnología'."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.create_function("fold", 1, fold, deterministic=True)
    connection.executescript(_SCHEMA)
    existing = {row["name"] for row in connection.execute("PRAGMA table_info(products)")}
    for column, kind in _ADDED_PRODUCT_COLUMNS.items():
        if column not in existing:
            connection.execute(f"ALTER TABLE products ADD COLUMN {column} {kind}")
    return connection


def record_run(
    connection: sqlite3.Connection,
    source: Source,
    scope: str,
    products: list[Product],
    at: datetime | None = None,
) -> int:
    """Store one sync: upsert the products and add an observation for each.

    `at` is when the sync happened; it defaults to the current time.
    """
    now = (at or datetime.now(UTC)).isoformat(timespec="seconds")
    with connection:
        run_id = connection.execute(
            "INSERT INTO runs (source, scope, started_at, product_count) VALUES (?, ?, ?, ?)",
            (source, scope, now, len(products)),
        ).lastrowid
        for p in products:
            connection.execute(
                """
                INSERT INTO products (source, id, name, category, url, currency, price, miles,
                                      price_with_miles, in_stock, badges, seller, condition,
                                      first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (source, id) DO UPDATE SET
                    name = excluded.name,
                    category = COALESCE(excluded.category, products.category),
                    url = excluded.url, currency = excluded.currency, price = excluded.price,
                    miles = excluded.miles, price_with_miles = excluded.price_with_miles,
                    in_stock = excluded.in_stock, badges = excluded.badges,
                    seller = excluded.seller,
                    condition = COALESCE(excluded.condition, products.condition),
                    last_seen = excluded.last_seen
                """,
                (
                    p.source,
                    p.id,
                    p.name,
                    p.category,
                    p.url,
                    p.currency,
                    p.price,
                    p.miles,
                    p.price_with_miles,
                    p.in_stock,
                    p.badges,
                    p.seller,
                    p.condition,
                    now,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO observations (run_id, source, id, price, miles, in_stock) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, p.source, p.id, p.price, p.miles, p.in_stock),
            )
    if run_id is None:
        raise RuntimeError("sqlite did not return a run id")
    return run_id


def last_two_runs(
    connection: sqlite3.Connection, source: Source
) -> tuple[sqlite3.Row, sqlite3.Row] | None:
    """The newest run of `source` and the previous run with the same scope."""
    newest = connection.execute(
        "SELECT * FROM runs WHERE source = ? ORDER BY run_id DESC LIMIT 1", (source,)
    ).fetchone()
    if newest is None:
        return None
    previous = connection.execute(
        "SELECT * FROM runs WHERE source = ? AND scope = ? AND run_id < ? "
        "ORDER BY run_id DESC LIMIT 1",
        (source, newest["scope"], newest["run_id"]),
    ).fetchone()
    return None if previous is None else (previous, newest)


CHANGES_SQL = """
SELECT CASE
         WHEN old.id IS NULL THEN 'new'
         WHEN new.id IS NULL THEN 'gone'
         WHEN new.in_stock AND NOT old.in_stock THEN 'restocked'
         WHEN old.in_stock AND NOT new.in_stock THEN 'out of stock'
         WHEN COALESCE(new.price, new.miles) < COALESCE(old.price, old.miles) THEN 'cheaper'
         ELSE 'pricier'
       END AS change,
       p.id, p.name,
       COALESCE(old.price, old.miles) AS before,
       COALESCE(new.price, new.miles) AS after
FROM products p
LEFT JOIN observations old ON old.source = p.source AND old.id = p.id AND old.run_id = :old
LEFT JOIN observations new ON new.source = p.source AND new.id = p.id AND new.run_id = :new
WHERE p.source = :source
  AND (old.id IS NOT NULL OR new.id IS NOT NULL)
  AND (old.id IS NULL OR new.id IS NULL
       OR old.in_stock != new.in_stock
       OR COALESCE(old.price, old.miles, -1) != COALESCE(new.price, new.miles, -1))
ORDER BY change, p.name
"""
