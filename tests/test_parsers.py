from pathlib import Path

import pytest

from app.bluray import PageError, _encoding, parse_product, parse_search, split_title
from app.notify import digest_payload
from app.reddit import is_relevant, parse_feed

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


def test_reddit_filter():
    posts = [p for p in parse_feed(read("reddit.xml")) if is_relevant(p["title"])]
    assert [p["id"] for p in posts] == ["t3_aaa", "t3_ccc"]


def test_parse_product_currently_unavailable():
    p = parse_product(read("product_unavailable.html"))
    assert p["preorder"] == 0
    assert p["price"] == "$47.99"
    assert p["availability"] == "Currently unavailable"


def test_parse_product_ebay_only_has_no_price():
    p = parse_product(read("product_ebay_only.html"))
    assert p["price"] is None
    assert p["availability"] is None
    assert p["buy_url"] is None
    assert p["preorder"] == 0


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
    payload = digest_payload([], [])
    assert payload["title"] == "Steelbook digest"
    assert payload["message"] == "Nothing new since the last digest."
    community_only = digest_payload([], [{"title": "Sinners restock at Target"}])
    assert community_only["title"] == "Steelbook community news"
    assert community_only["message"].startswith("From r/steelbooks:")
