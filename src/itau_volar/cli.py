"""`volar` command line: sync both catalogs into SQLite and query them."""

import argparse
import dataclasses
import json
import sqlite3
import sys
import urllib.error
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path

from logger import get_logger, init_logging

from . import ml, tienda, volar
from .db import (
    CHANGES_SQL,
    SYNCABLE_SOURCES,
    Product,
    Source,
    connect,
    db_path,
    fold,
    last_two_runs,
    record_run,
)
from .http import DEFAULT_DELAY_SECONDS, Client

log = get_logger("volar")

_MAX_CELL_WIDTH = 70

_EXIT_ERROR = 1
_EXIT_BLOCKED = 3
_BLOCKED_HINT = (
    "the site refused the request; this is not a bug in the parser. "
    "Wait a while, raise --delay, or check whether a VPN is on."
)
_BLOCKING_STATUSES = frozenset({HTTPStatus.FORBIDDEN, HTTPStatus.TOO_MANY_REQUESTS})

_TABLE_COLUMNS = """source, id, name, currency AS cur, price, miles,
    price_with_miles AS cash_left, CASE WHEN in_stock THEN '' ELSE 'out' END AS stock, badges,
    seller"""
_JSON_COLUMNS = (
    "source, id, name, category, currency, price, miles, price_with_miles, in_stock, badges, "
    "seller, condition, url"
)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    # stderr: stdout carries the results (tables, --json) and must stay clean.
    init_logging(stream=sys.stderr)
    try:
        return args.run(args)
    except tienda.BlockedError as error:
        log.error("blocked: %s. %s", error, _BLOCKED_HINT)
        return _EXIT_BLOCKED
    except urllib.error.HTTPError as error:
        if error.code in _BLOCKING_STATUSES:
            log.error("blocked: HTTP %d. %s", error.code, _BLOCKED_HINT)
            return _EXIT_BLOCKED
        log.error("the site answered HTTP %d for %s", error.code, error.url)
    except (urllib.error.URLError, TimeoutError) as error:
        log.error("network error: %s", error)
    except tienda.ParseError as error:
        log.error("could not read the page: %s", error)
    except sqlite3.Error as error:
        log.error("database error: %s", error)
    return _EXIT_ERROR


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="volar", description=__doc__)
    commands = parser.add_subparsers(required=True, metavar="COMMAND")

    sync = commands.add_parser("sync", help="download a catalog into the local database")
    sync.add_argument("source", choices=[*SYNCABLE_SOURCES, "all"], nargs="?", default="all")
    sync.add_argument(
        "--category",
        type=int,
        default=tienda.TECNOLOGIA,
        help=f"Tienda Volar category id (default {tienda.TECNOLOGIA} = Tecnología, "
        f"{tienda.ALL_CATEGORIES} = whole store)",
    )
    sync.add_argument(
        "--delay", type=float, default=DEFAULT_DELAY_SECONDS, help="seconds between requests"
    )
    sync.set_defaults(run=_sync)

    import_ = commands.add_parser(
        "import", help="store Mercado Libre pages saved as text (e.g. by the Firefox MCP)"
    )
    import_.add_argument("source", choices=[Source.ML])
    import_.add_argument("files", nargs="+", type=Path, help="page text files, one page each")
    import_.set_defaults(run=_import)

    search = commands.add_parser("search", help="search the local database by name")
    search.add_argument("words", nargs="*", help="all words must appear in the name")
    search.add_argument("--source", choices=list(Source))
    search.add_argument("--min-price", type=float)
    search.add_argument("--max-price", type=float)
    search.add_argument("--limit", type=int, default=50)
    search.add_argument("--json", action="store_true", help="print JSON instead of a table")
    search.set_defaults(run=_search)

    show = commands.add_parser("show", help="fetch one Tienda Volar product page, live")
    show.add_argument("id", type=int)
    show.add_argument("--json", action="store_true", help="print JSON instead of text")
    show.set_defaults(run=_show)

    changes = commands.add_parser("changes", help="what changed between the last two syncs")
    changes.add_argument("source", choices=list(Source), nargs="?", default=Source.TIENDA)
    changes.set_defaults(run=_changes)

    sql = commands.add_parser("sql", help="run a read-only SQL query on the local database")
    sql.add_argument("query")
    sql.set_defaults(run=_sql)
    return parser


def _sync(args: argparse.Namespace) -> int:
    client = Client(delay=args.delay)
    connection = connect(db_path())
    sources = list(SYNCABLE_SOURCES) if args.source == "all" else [Source(args.source)]
    for source in sources:
        if source is Source.TIENDA:
            scope = f"category:{args.category}"
            products = _tienda_products(client, args.category)
        else:
            scope = "catalog"
            log.info("volar: downloading the catalog (about 15 seconds)")
            products = _volar_products(client)
        record_run(connection, source, scope, products)
        log.info("%s: stored %d products (%s)", source, len(products), scope)
    return 0


def _tienda_products(client: Client, category: int) -> list[Product]:
    def progress(done: int, total: int) -> None:
        log.info("tienda: %d / %d", done, total)

    return [
        Product(
            source=Source.TIENDA,
            id=item.id,
            name=item.name,
            url=item.url,
            currency=item.currency,
            price=item.price,
            miles=item.card_miles,
            price_with_miles=item.price_with_card_miles,
            badges=", ".join(item.badges) or None,
        )
        for item in tienda.iter_search(client.get, category=category, on_page=progress)
    ]


def _volar_products(client: Client) -> list[Product]:
    return [
        Product(
            source=Source.VOLAR,
            id=item.id,
            name=item.name,
            category=item.category,
            url=item.url,
            miles=item.miles,
            in_stock=item.in_stock,
        )
        for item in volar.fetch_catalog(client.post_json)
    ]


def _import(args: argparse.Namespace) -> int:
    """Each file is one run, dated when the page was saved. Unreadable files are skipped."""
    connection = connect(db_path())
    failed = 0
    for path in args.files:
        try:
            page = ml.parse_page(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ml.ParseError) as error:
            log.error("%s: skipped: %s", path, error)
            failed += 1
            continue
        products = [
            Product(
                source=Source.ML,
                id=item.id,
                name=item.title,
                url=item.url,
                currency=item.currency,
                price=item.price,
                seller=item.seller,
                condition=item.condition,
            )
            for item in page.listings
        ]
        saved_at = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        record_run(connection, Source.ML, page.scope, products, at=saved_at)
        log.info("%s: stored %d products (%s)", path.name, len(products), page.scope)
    return _EXIT_ERROR if failed else 0


def _search(args: argparse.Namespace) -> int:
    where, params = ["1 = 1"], []
    for word in args.words:
        where.append("fold(name) LIKE ?")
        params.append(f"%{fold(word)}%")
    if args.source:
        where.append("source = ?")
        params.append(args.source)
    if args.min_price is not None:
        where.append("price >= ?")
        params.append(args.min_price)
    if args.max_price is not None:
        where.append("price <= ?")
        params.append(args.max_price)
    rows = (
        connect(db_path())
        .execute(
            f"""
        SELECT {_JSON_COLUMNS if args.json else _TABLE_COLUMNS}
        FROM products WHERE {" AND ".join(where)}
        ORDER BY price IS NULL, currency DESC, price, miles LIMIT ?
        """,
            [*params, args.limit],
        )
        .fetchall()
    )
    if args.json:
        _print_json([dict(row) | {"in_stock": bool(row["in_stock"])} for row in rows])
    else:
        _print_table(rows)
    return 0


def _show(args: argparse.Namespace) -> int:
    url = tienda.product_url(args.id)
    detail = tienda.parse_detail(Client(delay=0).get(url), url)
    if args.json:
        _print_json(dataclasses.asdict(detail))
        return 0
    lines = [
        detail.name,
        f"id {detail.id} | brand {detail.brand or '?'} | EAN {detail.barcode or '?'}",
        " / ".join(detail.category_path),
        f"price: {detail.currency} {_number(detail.price)}",
        f"miles: up to {_number(detail.max_miles)} at {detail.currency} {detail.mile_value} each",
        f"stock: {_number(detail.stock_max)}",
        detail.url,
        "",
        *(f"{name}: {value}" for name, value in detail.attributes.items()),
        "",
        detail.description,
    ]
    print("\n".join(lines))
    return 0


def _changes(args: argparse.Namespace) -> int:
    connection = connect(db_path())
    runs = last_two_runs(connection, args.source)
    if runs is None:
        log.error("need two syncs of '%s' with the same scope to compare", args.source)
        return 1
    previous, newest = runs
    log.info("%s: %s -> %s", newest["scope"], previous["started_at"], newest["started_at"])
    rows = connection.execute(
        CHANGES_SQL,
        {"old": previous["run_id"], "new": newest["run_id"], "source": args.source},
    ).fetchall()
    _print_table(rows)
    return 0


def _sql(args: argparse.Namespace) -> int:
    connection = connect(db_path())
    connection.execute("PRAGMA query_only = ON")
    _print_table(connection.execute(args.query).fetchall())
    return 0


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _number(value: float | None) -> str:
    if value is None:
        return "?"
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"


def _cell(value: object) -> str:
    if value is None:
        return ""
    text = _number(value) if isinstance(value, float) else str(value)
    return text if len(text) <= _MAX_CELL_WIDTH else text[: _MAX_CELL_WIDTH - 1] + "…"


def _print_table(rows: Iterable[sqlite3.Row]) -> None:
    rows = list(rows)
    if not rows:
        print("(no rows)")
        return
    header = list(rows[0].keys())
    cells = [[_cell(value) for value in row] for row in rows]
    widths = [max(len(header[i]), *(len(r[i]) for r in cells)) for i in range(len(header))]
    for line in [header, *cells]:
        padded = (text.ljust(width) for text, width in zip(line, widths, strict=True))
        print("  ".join(padded).rstrip())
    print(f"({len(rows)} rows)")
