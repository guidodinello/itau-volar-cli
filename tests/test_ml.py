import pytest

from itau_volar.ml import Listing, PageKind, ParseError, parse_page, synthetic_id

from .ml_pages import OFFICIAL_STORE_LISTING, SEARCH_PAGE, SNAPSHOT, USED_LISTING

SEARCH_URL = "https://listado.mercadolibre.com.uy/beats-fit-pro"


def test_a_search_page_gives_one_row_per_distinct_card_at_its_current_price():
    page = parse_page(SEARCH_PAGE)

    assert (page.kind, page.scope) == (PageKind.SEARCH, "search:beats-fit-pro")
    assert [(item.title, item.seller, item.currency, item.price) for item in page.listings] == [
        ("Auriculares Inalámbricos Beats Fit Pro Cancelación Activa De", "GLIC", "$", 6672.0),
        ("Router Tp-link Archer Be400 Wifi 7 Tri-band 7.2gbps", "Tienda Ejemplo", "U$S", 236.55),
        (
            "Auriculares Beats By Dr. Dre Beats Fit Pro con cancelación de ruido, color violeta",
            None,
            "$",
            14900.0,
        ),
        ("Kit Extensiones De Pestañas Negras Con Pinza Y Pegamento Box", "TIENDA MVD", "$", 890.0),
    ]


def test_search_rows_link_to_the_search_and_carry_no_condition():
    # The card text has no item link and no condition; the page cannot tell new from used.
    page = parse_page(SEARCH_PAGE)

    assert {item.url for item in page.listings} == {SEARCH_URL}
    assert {item.condition for item in page.listings} == {None}


def test_a_search_row_id_is_the_same_in_every_run():
    # Pinned: a changed id would orphan the row's price history.
    assert synthetic_id("Auriculares Beats Fit Pro", "GLIC") == 4066676695885144858


def test_a_search_row_id_tells_the_same_title_from_different_sellers_apart():
    assert synthetic_id("Beats Fit Pro", "GLIC") != synthetic_id("Beats Fit Pro", "MIAMIBOX")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            OFFICIAL_STORE_LISTING,
            Listing(
                id=1519754514,
                title="Auriculares Inalámbricos Beats Powerbeats Pro 2 Cancelación",
                currency="$",
                price=9344.0,
                url="https://articulo.mercadolibre.com.uy/MLU-1519754514",
                seller="GLIC",
                condition="Nuevo",
            ),
        ),
        (
            USED_LISTING,
            Listing(
                id=1545682996,
                title="Reloj Inteligente Garmin Forerunner 165 Negro Caja Original (Usado)",
                currency="$",
                price=7500.0,
                url="https://articulo.mercadolibre.com.uy/MLU-1545682996",
                seller="VENDEDOR123",
                condition="Usado",
            ),
        ),
    ],
    ids=["official-store-discounted", "used-private-seller"],
)
def test_a_listing_page_gives_its_publication_with_condition_and_seller(text, expected):
    page = parse_page(text)

    assert (page.kind, page.scope) == (PageKind.LISTING, f"listing:{expected.id}")
    assert page.listings == [expected]


@pytest.mark.parametrize(
    "text",
    [
        SNAPSHOT,
        "",
        "Resultados\nOrdenar por\nMás relevantes",
        "Beats fit pro\n33 resultados\nFiltros",
    ],
    ids=["snapshot", "empty", "search-without-result-count", "result-count-without-results"],
)
def test_text_that_is_not_a_known_page_is_refused(text):
    with pytest.raises(ParseError, match="neither a Mercado Libre search page nor a listing"):
        parse_page(text)


def test_a_search_page_whose_cards_cannot_be_read_is_refused():
    no_cards = SEARCH_PAGE.split("Más relevantes")[0] + "Más relevantes\nOtras personas buscaron"

    with pytest.raises(ParseError, match="has no result cards"):
        parse_page(no_cards)
