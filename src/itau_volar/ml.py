"""Mercado Libre Uruguay pages saved as visible text.

Mercado Libre refuses plain HTTP clients, so this tool never fetches it. Instead it reads the
visible text of pages opened in a real browser (the Firefox MCP's `get_page_text` saves it to
a file). Two page kinds are understood; anything else raises `ParseError` instead of storing
guesses:

- a search page (`listado.mercadolibre.com.uy/<query>`): one row per result card. The text
  carries no item ids, links or condition, so rows get a stable synthetic id (title + seller),
  the search page as their link, and may include used items.
- a listing page (one publication): the real publication number, condition and seller.
"""

import hashlib
import re
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum

from .db import fold

SEARCH_URL = "https://listado.mercadolibre.com.uy/{slug}"
LISTING_URL = "https://articulo.mercadolibre.com.uy/MLU-{id}"

# ML writes dollars as "US$"; Tienda Volar (and so the database) as "U$S". Pesos are "$" in both.
_CURRENCIES = {"$": "$", "US$": "U$S"}

_RESULT_COUNT = re.compile(r"^([\d.]+) resultados?$")
_PUBLICATION = re.compile(r"^Publicación #(\d+)$")
_CONDITION = re.compile(r"^(Nuevo|Usado|Reacondicionado)\b")
_INTEGER_PART = re.compile(r"^\d{1,3}(?:\.\d{3})*$")
_DECIMAL_PART = re.compile(r"^\d{1,2}$")
_RATING = re.compile(r"^ \d(?:\.\d)?$")
_DISCOUNT = re.compile(r"\d+% OFF")
# 63 bits, so a synthetic id fits SQLite's signed INTEGER.
_ID_MASK = (1 << 63) - 1


type Price = tuple[str, float]  # (currency, value)
type Card = tuple[str, str | None, list[Price]]  # (title, seller, prices)


class ParseError(ValueError):
    """The text is not a Mercado Libre page this module understands."""


class PageKind(StrEnum):
    SEARCH = "search"
    LISTING = "listing"


@dataclass(frozen=True, slots=True)
class Listing:
    id: int
    title: str
    currency: str
    price: float
    url: str
    seller: str | None = None
    condition: str | None = None


@dataclass(frozen=True, slots=True)
class Page:
    kind: PageKind
    # 'search:<slug>' or 'listing:<id>': what was imported, so repeated imports compare.
    scope: str
    listings: list[Listing]


def parse_page(text: str) -> Page:
    lines = text.splitlines()
    if any(_PUBLICATION.match(line) for line in lines) and "Comprar ahora" in lines:
        return _parse_listing(lines)
    if any(_RESULT_COUNT.match(line) for line in lines) and "Ordenar por" in lines:
        return _parse_search(lines)
    raise ParseError("neither a Mercado Libre search page nor a listing page")


def synthetic_id(title: str, seller: str | None) -> int:
    """Stable across runs (unlike hash(), which Python salts per process)."""
    key = f"{fold(title)}|{fold(seller)}".encode()
    return int.from_bytes(hashlib.sha256(key).digest()[:8], "big") & _ID_MASK


def slug(query: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", fold(query)).strip("-")


def _parse_search(lines: list[str]) -> Page:
    count_at = next(i for i, line in enumerate(lines) if _RESULT_COUNT.match(line))
    query = lines[count_at - 1].strip()
    if not query:
        raise ParseError("search page without a query above the result count")
    url = SEARCH_URL.format(slug=slug(query))
    start = lines.index("Ordenar por", count_at)
    listings: dict[int, Listing] = {}
    for title, seller, prices in _cards(lines, start):
        currency, price = prices[-1]  # with a discount: the old price, then the current one
        listing = Listing(
            id=synthetic_id(title, seller),
            title=title,
            currency=currency,
            price=price,
            url=url,
            seller=seller,
        )
        # Search pages repeat cards (ads and organic results); keep the first.
        listings.setdefault(listing.id, listing)
    if not listings:
        raise ParseError(f"search page for {query!r} has no result cards")
    return Page(PageKind.SEARCH, f"search:{slug(query)}", list(listings.values()))


def _cards(lines: list[str], start: int) -> Iterator[Card]:
    """Each result card: title, seller (or None) and its prices before the installments."""
    card: Card | None = None
    last_price_end = -1
    i = start
    while i < len(lines):
        price = _price_at(lines, i)
        if price is None:
            i += 1
            continue
        value, end = price
        if card is not None and i == last_price_end:
            card[2].append(value)  # the price right after the crossed-out one
        else:
            if card is not None:
                yield card
            header = _card_header(lines, i, start)
            # None for prices outside a card, e.g. "10 cuotas de $ 667".
            card = None if header is None else (*header, [value])
        last_price_end = end
        i = end
    if card is not None:
        yield card


def _card_header(lines: list[str], price_at: int, start: int) -> tuple[str, str | None] | None:
    """Walk up from a card's first price, past its rating and seller, to its title."""
    seller = None
    j = price_at - 1
    while j > start:
        line = lines[j]
        if _RATING.match(line):
            j -= 1
        elif seller is None and line.endswith(" ") and line.strip() and not line[0].isspace():
            # Sellers are the only card lines ML renders with a trailing space.
            if "cuotas" in line:
                return None
            seller = line.strip()
            j -= 1
        else:
            break
    title = lines[j]
    if seller is None and title.isupper() and j - 1 > start and _looks_like_title(lines[j - 1]):
        # Some ad cards print the seller without the trailing space; it is still in capitals.
        seller, title = title.strip(), lines[j - 1]
    if not _looks_like_title(title):
        return None
    return title.strip(), seller


def _looks_like_title(line: str) -> bool:
    return (
        len(line.strip()) >= 8
        and not line[0].isspace()
        and any(c.isalpha() for c in line)
        and "cuotas" not in line
        and not _DISCOUNT.search(line)
    )


def _price_at(lines: list[str], i: int) -> tuple[Price, int] | None:
    """A price written over lines: '$' or 'US$', '6.672', and optionally ',' '20'.

    Returns ((currency, value), index after the price) or None.
    """
    if lines[i] not in _CURRENCIES or i + 1 >= len(lines):
        return None
    if not _INTEGER_PART.match(lines[i + 1]):
        return None
    value = float(lines[i + 1].replace(".", ""))
    end = i + 2
    if end + 1 < len(lines) and lines[end] == "," and _DECIMAL_PART.match(lines[end + 1]):
        value += int(lines[end + 1].ljust(2, "0")) / 100
        end += 2
    return (_CURRENCIES[lines[i]], value), end


def _parse_listing(lines: list[str]) -> Page:
    publication = next(int(m[1]) for m in map(_PUBLICATION.match, lines) if m)
    head, condition = next(
        ((i, m[1]) for i, m in enumerate(map(_CONDITION.match, lines)) if m), (None, None)
    )
    if head is None or head + 1 >= len(lines):
        raise ParseError(f"listing {publication}: no condition line (Nuevo/Usado) above the title")
    title = lines[head + 1].strip()
    prices: list[Price] = []
    i = head + 2
    while i < len(lines) and "cuotas" not in lines[i]:
        price = _price_at(lines, i)
        if price is None:
            i += 1
            continue
        prices.append(price[0])
        i = price[1]
    if not prices:
        raise ParseError(f"listing {publication}: no price below the title")
    currency, price = prices[-1]
    return Page(
        PageKind.LISTING,
        f"listing:{publication}",
        [
            Listing(
                id=publication,
                title=title,
                currency=currency,
                price=price,
                url=LISTING_URL.format(id=publication),
                seller=_listing_seller(lines),
                condition=condition,
            )
        ],
    )


def _listing_seller(lines: list[str]) -> str | None:
    for i, line in enumerate(lines[:-1]):
        if line == "Tienda oficial" or line == "Vendido por":
            return lines[i + 1].strip() or None
    return None
