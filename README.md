# itau-volar-cli

Personal CLI to browse and compare what Itaú Volar miles can buy. It reads two public sites,
without login, and keeps a local SQLite history so price drops and new products show up.
Mercado Libre prices can be added from pages saved in a browser, to compare against.

| Source | Site | What it has | How it is read |
| --- | --- | --- | --- |
| `tienda` | tiendavolar.com.uy | Physical products, paid in cash and/or miles | `GXState` JSON embedded in each HTML page |
| `volar` | itauvolar.com.uy/volar | Vouchers and experiences, paid in miles | `POST /volar/api/catalog/search` |
| `ml` | mercadolibre.com.uy | Mercado Libre results and listings, to compare prices | Imported from page text saved in a browser (`volar import ml`); never fetched |

It never logs in: no miles balance, no redemptions.

## Use

```bash
uv sync
uv run volar sync                  # Tecnología (category 257) + the Volar catalog, ~1 minute
uv run volar sync tienda --category 0   # whole Tienda Volar store
uv run volar search auriculares jbl --max-price 150
uv run volar show 1595883          # live product page: brand, specs, stock, max miles
uv run volar changes tienda        # new / gone / cheaper / pricier since the previous sync
uv run volar sql "SELECT name, price FROM products WHERE source = 'tienda' ORDER BY price LIMIT 5"
uv run volar import ml ~/.firefox-mcp/output/page-text-*.txt   # Mercado Libre pages, see below
```

`search` and `show` take `--json` for structured output. Exit codes: `0` ok, `1` error
(network, changed page layout, database), `3` the site refused or challenged the request.

The database lives at `~/.local/share/itau-volar/volar.db` (override with `VOLAR_DB`).
Tables: `products` (current state), `runs`, `observations` (one row per product per sync).
`search` and `sql` match accents loosely through the `fold()` SQL function.

## Mercado Libre

Mercado Libre refuses plain HTTP clients, so `volar` never requests it. Open the page in
Firefox (the [Firefox MCP](https://github.com/mozilla/firefox-mcp) works: `get_page_text`
with `saveTo` writes the visible text to `~/.firefox-mcp/output/`), then import the files.
Each file becomes one run, dated by the file's modification time.

- **Search pages** (`listado.mercadolibre.com.uy/<query>`) give one row per result card: title,
  seller, current price. The text has no item links, ids or condition, so a row's id is a hash
  of title and seller, its `url` is the search page, and **it may be a used item**.
- **Listing pages** (one publication) give the publication number, a direct link
  (`articulo.mercadolibre.com.uy/MLU-<n>`), the seller and the condition (Nuevo/Usado).
- Anything else (a snapshot, another site) is skipped with an error and stores nothing.
- Dollar prices use Tienda Volar's `U$S`; peso prices are `$` in both sources.

## Things worth knowing

- **Miles have a fixed value in Tienda Volar.** The product page gives it (`SellPointValue`,
  U$S 0.0125 per mile when checked on 2026-11-17), and the slider only chooses how much of the
  price to cover. A good deal there is a low cash price compared with other shops, not a
  particular miles split. In `products`, `miles` and `price_with_miles` are the split the card
  advertises (about half), not the maximum.
- **Requests are sequential and one second apart.** Tienda Volar's footer says product and
  price data is for strictly personal use and forbids collecting it for reproduction or resale.
  Keep this tool personal and low-volume.
- **Both parsers depend on undocumented page structure.** A site change raises `ParseError`
  instead of storing wrong data.

## Develop

```bash
uv run pytest
uv run ruff check .
```
