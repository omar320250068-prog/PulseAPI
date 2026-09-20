"""Offline tests for scraper.py -- no network required.

Plain asserts, matching the style of the repo's other smoke test.
Run with:  python test_scraper.py
"""

from urllib import robotparser

from scraper import (
    BASE_URL,
    Book,
    PoliteScraper,
    parse_availability,
    parse_books,
    parse_price,
    parse_rating,
)


class _FakeElement:
    def __init__(self, classes):  # noqa: ANN001
        self._classes = classes

    def get(self, key, default=None):  # noqa: ANN001
        return self._classes if key == "class" else default


VALID_CARD = """
<article class="product_pod">
  <div class="image_container"><a href="a-light-in-the-attic_1000/index.html">
    <img alt="A Light in the Attic" class="thumbnail" src="x.jpg"/></a></div>
  <p class="star-rating Three"><i class="icon-star"></i></p>
  <h3><a href="a-light-in-the-attic_1000/index.html" title="A Light in the Attic">A Light in the ...</a></h3>
  <div class="product_price">
    <p class="price_color">\u00a351.77</p>
    <p class="instock availability"><i class="icon-ok"></i>In stock</p>
  </div>
</article>
"""

AVAILABLE_CARD = """
<article class="product_pod">
  <p class="star-rating Five"><i class="icon-star"></i></p>
  <h3><a href="sharp-objects_997/index.html" title="Sharp Objects">Sharp Objects</a></h3>
  <div class="product_price">
    <p class="price_color">\u00a347.82</p>
    <p class="instock availability"><i class="icon-ok"></i>In stock (22 available)</p>
  </div>
</article>
"""

BROKEN_CARD = """
<article class="product_pod">
  <h3><a href="#" title="Broken no price">Broken no price</a></h3>
  <p class="instock availability">In stock (1 available)</p>
</article>
"""


def test_parse_price():
    assert parse_price("\u00a351.77") == (51.77, "GBP")
    assert parse_price("\u00a3 13.99") == (13.99, "GBP")
    assert parse_price("$9.99") == (9.99, "USD")
    try:
        parse_price("")
        raise AssertionError("empty price should have been rejected")
    except Exception as exc:
        assert "no price" in str(exc)


def test_parse_availability():
    assert parse_availability("In stock (22 available)") == ("In stock (22 available)", 22)
    assert parse_availability("In stock") == ("In stock", None)
    assert parse_availability("  In\n  stock (3 available)  ") == ("In stock (3 available)", 3)


def test_parse_rating():
    assert parse_rating(_FakeElement(["star-rating", "Three"])) == 3
    assert parse_rating(_FakeElement(["star-rating", "Five"])) == 5
    assert parse_rating(_FakeElement(["star-rating", "One"])) == 1


def test_book_schema():
    good = Book(
        title="Sharp Objects",
        price=47.82,
        currency="GBP",
        availability="In stock (22 available)",
        availability_count=22,
        rating=5,
        url="https://books.toscrape.com/catalogue/sharp-objects_997/index.html",
    )
    assert good.model_dump()["price"] == 47.82

    try:
        Book(
            title="Bad rating",
            price=1.0,
            currency="GBP",
            availability="In stock",
            rating=9,
            url="http://x",
        )
        raise AssertionError("rating=9 should have been rejected")
    except Exception as exc:
        assert "rating" in str(exc)

    try:
        Book(
            title="Bad price",
            price=-3.0,
            currency="GBP",
            availability="In stock",
            rating=3,
            url="http://x",
        )
        raise AssertionError("negative price should have been rejected")
    except Exception as exc:
        assert "price" in str(exc)


def test_parse_books_counts_and_fields():
    html = VALID_CARD + AVAILABLE_CARD
    records, errors = parse_books(html, BASE_URL + "/catalogue/page-1.html")
    assert errors == []
    assert len(records) == 2

    first = records[0]
    assert first["title"] == "A Light in the Attic"
    assert first["price"] == 51.77
    assert first["currency"] == "GBP"
    assert first["rating"] == 3
    assert first["availability"] == "In stock"
    assert first["availability_count"] is None
    assert first["url"] == "https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html"

    second = records[1]
    assert second["rating"] == 5
    assert second["availability_count"] == 22
    assert second["url"] == "https://books.toscrape.com/catalogue/sharp-objects_997/index.html"


def test_parse_books_survives_broken_page():
    records, errors = parse_books("<div>this is not a real page</div>", "https://x/")
    assert records == []
    assert errors == []


def test_parse_books_skips_broken_cards():
    html = VALID_CARD + BROKEN_CARD
    records, errors = parse_books(html, BASE_URL + "/catalogue/page-2.html")
    assert len(records) == 1
    assert len(errors) == 1
    assert "missing its price" in errors[0]


def test_robots_rules_are_respected():
    class FakeRulesScraper(PoliteScraper):
        def _load_robots(self) -> robotparser.RobotFileParser:
            parser = robotparser.RobotFileParser()
            parser.parse(["User-agent: *", "Disallow: /catalogue/page-3.html"])
            return parser

    scraper = FakeRulesScraper()
    assert scraper.is_allowed(BASE_URL + "/catalogue/page-1.html")
    assert not scraper.is_allowed(BASE_URL + "/catalogue/page-3.html")


def test_unreachable_server_is_survived():
    """A dead server must produce a failed report, never a crash."""
    scraper = PoliteScraper(
        base_url="http://127.0.0.1:9", pages=2, delay=0, max_retries=1
    )
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        report = scraper.scrape(__import__("pathlib").Path(tmp))
    assert report["pages_failed"] == 2
    assert report["books_collected"] == 0
    assert all(page["status"] == "failed" for page in report["pages"])


def main() -> None:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"\n{len(tests)} scraper tests passed")


if __name__ == "__main__":
    main()