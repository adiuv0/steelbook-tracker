from pathlib import Path

from app.bluray import parse_product, parse_search, split_title
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
