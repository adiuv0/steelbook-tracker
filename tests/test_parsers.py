from pathlib import Path

import pytest

from app.bluray import (PageError, _encoding, is_unavailable, parse_exclusive, parse_price_block,
                        parse_product, parse_search, split_title)
from app.notify import digest_payload, preorder_payload, restock_payload

FIX = Path(__file__).parent / "fixtures"


def read(name: str) -> str:
    return (FIX / name).read_text()


def test_split_title():
    assert split_title("The 'Burbs 4K\n (Collector's Edition)") == ("The 'Burbs 4K", "Collector's Edition")
    assert split_title("Die Hard 4K\n") == ("Die Hard 4K", None)


def test_parse_search():
    items = parse_search(read("search_simple.html"))
    assert [i["id"] for i in items] == [422840, 422773, 422467, 421959]
    other_mommy, die_hard, horror, burbs = items
    assert other_mommy["release_date"] is None
    assert other_mommy["edition"] == "Limited Edition"
    assert other_mommy["studio"] == "Universal Studios"
    assert die_hard["release_date"] == "2026-12-01"
    assert die_hard["year"] == "1988"
    assert horror["year"] is None
    assert burbs["title"] == "The 'Burbs 4K"


def test_parse_product_preorder():
    p = parse_product(read("product_preorder.html"))
    assert p["is_steelbook"] == 1
    assert p["preorder"] == 1
    assert p["price"] == "$31.27"
    assert p["availability"] == "Available for pre-order"
    assert p["release_date"] == "2026-11-17"
    assert p["buy_url"].startswith("https://www.blu-ray.com/link/click.php")
    assert p["cover"].endswith("421738_large.jpg")


def test_parse_product_announced():
    p = parse_product(read("product_announced.html"))
    assert p["is_steelbook"] == 1
    assert p["preorder"] == 0
    assert p["price"] is None
    assert p["release_date"] == "2026-12-01"


def test_parse_product_not_steelbook():
    assert parse_product(read("product_not_steelbook.html"))["is_steelbook"] == 0


def test_parse_product_currently_unavailable():
    p = parse_product(read("product_unavailable.html"))
    assert p["preorder"] == 0
    assert p["price"] == "$47.99"
    assert p["availability"] == "Currently unavailable"
    assert p["orderable"] is False


def test_parse_product_ebay_only_has_no_price():
    p = parse_product(read("product_ebay_only.html"))
    assert p["price"] is None
    assert p["availability"] is None
    assert p["buy_url"] is None
    assert p["preorder"] == 0
    assert p["orderable"] is None  # no store status at all: nothing is known


def test_parse_product_refuses_pages_that_are_not_product_pages():
    """A block, captcha or maintenance page must not be read as 'not a steelbook'."""
    with pytest.raises(PageError):
        parse_product("<html><head><title>Just a moment...</title></head><body>Checking your browser</body></html>")
    with pytest.raises(PageError):
        parse_product("")


def test_parse_product_deleted_listing_is_dropped():
    # What blu-ray.com returns (HTTP 200) for a product id that no longer exists.
    gone = ('<html><head><meta name="robots" content="noindex,nofollow,noodp,noydir"></head>'
            "<body>No such movie: 499999999</body></html>")
    p = parse_product(gone)
    assert p["is_steelbook"] == 0
    assert p["preorder"] == 0


def test_pages_without_a_charset_are_read_as_windows_1252():
    assert _encoding(b"Am\xe9lie 4K (Limited Collector\x92s Edition)") == "cp1252"
    assert _encoding("Amélie 4K".encode()) == "utf-8"
    assert _encoding(b"Die Hard 4K") == "utf-8"


def test_digest_asked_for_by_hand_with_nothing_new_says_so():
    payload = digest_payload([])
    assert payload["title"] == "Steelbook digest"
    assert payload["message"] == "Nothing new since the last digest."


def test_store_exclusives_are_read_from_the_page_title():
    # Titles as Blu-ray.com had them on 2026-11-17.
    assert parse_exclusive("Street Fighter 4K Blu-ray (Amazon Exclusive SteelBook)") == "Amazon"
    assert parse_exclusive("Gone with the Wind 4K Blu-ray (Wal-Mart Exclusive SteelBook)") == "Walmart"
    assert parse_exclusive("Paddington 4K Blu-ray (Lionsgate Limited Exclusive SteelBook)") == "Lionsgate Limited"
    assert (parse_exclusive("Swamp Thing 4K Blu-ray (MVD Entertainment Group Exclusive SteelBook)")
            == "MVD Entertainment Group")
    assert parse_exclusive("Resident Evil 4K Blu-ray (SteelBook)") is None
    # Shapes not seen in the wild yet: only the brackets count, and the store may come second.
    assert parse_exclusive("The Exclusive 4K Blu-ray (SteelBook)") is None
    assert parse_exclusive("Some Film 4K Blu-ray (SteelBook / Target Exclusive)") == "Target"


def test_parse_product_amazon_exclusive_on_preorder():
    p = parse_product(read("product_amazon_exclusive.html"))
    assert p["is_steelbook"] == 1
    assert p["exclusive"] == "Amazon"
    assert p["preorder"] == 1 and p["orderable"] is True
    assert p["availability"] == "Available for pre-order"
    assert p["price"] == "$47.99"
    assert p["release_date"] is None  # the menu's "Release calendar" link is not a date


def test_parse_product_exclusive_to_a_store_blu_ray_com_has_no_stock_for():
    """Blu-ray.com only reports Amazon's stock, so a Walmart exclusive has no status."""
    p = parse_product(read("product_walmart_exclusive.html"))
    assert p["is_steelbook"] == 1
    assert p["exclusive"] == "Walmart"
    assert p["availability"] is None and p["orderable"] is None
    assert p["preorder"] == 0
    assert p["release_date"] == "2026-11-03"


def test_store_status_wording():
    block = ('<span class="subheading">Price</span><br> Amazon: <a href="x"><b>$33.33</b></a> <br>'
             '<font color="#006600">{}</font><br><span class="subheading">Movie rating</span>')
    # Both spellings were live on 2026-11-17.
    assert parse_price_block(block.format("Currently unavailable."))[1] == "Currently unavailable"
    assert parse_price_block(block.format("Currently unavailable, <b>FREE</b> shipping"))[1] == "Currently unavailable"
    assert parse_price_block(block.format("Temporarily out of stock, <b>FREE</b> shipping"))[1] == "Temporarily out of stock"
    assert is_unavailable("Currently unavailable")
    # These all still take an order.
    for status in ("Available for pre-order", "In Stock", "Temporarily out of stock"):
        assert not is_unavailable(status), status
    assert not is_unavailable(None)


def test_alerts_name_the_store_of_an_exclusive():
    row = {"id": 423651, "title": "Street Fighter 4K", "edition": None, "release_date": None, "price": "$47.99",
           "availability": "Available for pre-order", "exclusive": "Amazon", "cover": None,
           "buy_url": "https://www.blu-ray.com/link/click.php?p=2167174&tid=011&c=7", "url": "https://x/423651/"}
    assert preorder_payload(row)["message"] == "Street Fighter 4K (date TBA) · $47.99 · Amazon exclusive"
    restock = restock_payload({**row, "edition": "Limited Edition", "exclusive": None, "availability": "In Stock"})
    assert restock["type"] == "restock"
    assert restock["message"] == "Street Fighter 4K (Limited Edition) · In Stock · $47.99"
    assert restock["url"] == row["buy_url"]
    digest = digest_payload([{**row, "exclusive": "Walmart", "release_date": "2026-11-03"}])
    assert digest["message"] == "• Street Fighter 4K (2026-11-03) · Walmart exclusive"
