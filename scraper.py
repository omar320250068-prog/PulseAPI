"""Week 4 -- Polite book scraper.

Turns three pages of messy HTML from a free practice site into clean, checked
JSON, without ever being rude to the server.

Politeness and hygiene rules:

* robots.txt is honoured: it is fetched (with a polite delay) and every page
  URL is checked against it before collection. books.toscrape.com ships no
  robots.txt, so the scraper treats that as "allowed by default" and still
  keeps its own rate limits.
* Every request identifies itself with a real User-Agent.
* One request at a time, with a configurable delay between hits.
* Transient failures are retried with backoff; a permanently broken page is
  recorded and skipped -- the run never crashes partway.
* Nothing is trusted: every record is validated against the Book schema
  (Pydantic) before it reaches the JSON output.

Outputs:
    books.json           -- the validated books (one JSON array)
    scrape_report.json   -- run metadata (pages, skipped rows, failures, time)
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from urllib import robotparser
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, field_validator

BASE_URL = "https://books.toscrape.com"
USER_AGENT = (
    "week4-polite-scraper/1.0 (CS assignment; gracious rate; "
    "see https://github.com/omar320250068-prog/PulseAPI)"
)
DEFAULT_PAGES = 3
DEFAULT_DELAY = 1.0
MAX_RETRIES = 3
TIMEOUT_SECONDS = 20.0

RATING_WORDS = {"One": 1, "Two": 2, "Three": 3, "Four": 4, "Five": 5}
CURRENCY_BY_SYMBOL = {"\u00a3": "GBP", "$": "USD", "\u20ac": "EUR"}


class RobotsDisallowedError(Exception):
    pass


class PageMissingError(Exception):
    pass


class ScrapeError(Exception):
    pass


class Book(BaseModel):
    """Schema every collected record must satisfy before it can be saved."""

    title: str
    price: float
    currency: str
    availability: str
    availability_count: int | None = None
    rating: int
    url: str

    @field_validator("price")
    @classmethod
    def _price_is_non_negative(cls, value: float) -> float:
        if value < 0:
            raise ValueError("price cannot be negative")
        return round(value, 2)

    @field_validator("rating")
    @classmethod
    def _rating_is_in_range(cls, value: int) -> int:
        if not 1 <= value <= 5:
            raise ValueError("rating must be an integer from 1 to 5")
        return value


def parse_price(text: str) -> tuple[float, str]:
    """Turn messy text like '\\u00a351.77' into (51.77, 'GBP')."""
    match = re.search(r"[^\d]*(\d+(?:\.\d{1,2})?)", text or "")
    if not match:
        raise ValueError(f"no price found in {text!r}")
    symbol = re.search(r"[^\w\s]", text or "")
    return round(float(match.group(1)), 2), CURRENCY_BY_SYMBOL.get(
        symbol.group(0) if symbol else "", "GBP"
    )


def parse_availability(text: str) -> tuple[str, int | None]:
    """Normalise whitespace and pull the count out of 'In stock (22 available)'."""
    clean = re.sub(r"\s+", " ", text or "").strip()
    count_match = re.search(r"(\d+)\s+available", clean)
    count = int(count_match.group(1)) if count_match else None
    return clean, count


def parse_rating(element) -> int:
    """Map the 'star-rating Three' class back to the number of stars."""
    classes = {c.lower() for c in (element.get("class") or [])}
    for word, value in RATING_WORDS.items():
        if word.lower() in classes:
            return value
    raise ValueError("no known star-rating class found")


def parse_book(article, page_url: str) -> dict:
    """Extract one book card. Raises ValueError if the card is unusable."""
    title_element = article.select_one("h3 a")
    if title_element is None:
        raise ValueError("book card missing the title link")
    title = (title_element.get("title") or title_element.get_text(strip=True)).strip()
    if not title:
        raise ValueError("book card has an empty title")

    price_element = article.select_one(".price_color")
    if price_element is None:
        raise ValueError(f"book {title!r} is missing its price")
    price, currency = parse_price(price_element.get_text())

    availability_element = article.select_one(".availability")
    availability, availability_count = parse_availability(
        availability_element.get_text() if availability_element else ""
    )
    if not availability:
        raise ValueError(f"book {title!r} is missing its availability")

    rating_element = article.select_one("p.star-rating")
    if rating_element is None:
        raise ValueError(f"book {title!r} is missing its star rating")
    rating = parse_rating(rating_element)

    relative_url = (title_element.get("href") or "").strip()
    if not relative_url:
        raise ValueError(f"book {title!r} is missing its detail URL")

    return {
        "title": title,
        "price": price,
        "currency": currency,
        "availability": availability,
        "availability_count": availability_count,
        "rating": rating,
        "url": urljoin(page_url, relative_url),
    }


def parse_books(html: str, page_url: str) -> tuple[list[dict], list[str]]:
    """Parse every book card on a page.

    Messy or truncated HTML never crashes this function. Usable cards become
    records; broken cards are reported as per-row errors. Returns
    (records, errors).
    """
    soup = BeautifulSoup(html or "", "html.parser")
    records: list[dict] = []
    errors: list[str] = []
    for index, article in enumerate(soup.select("article.product_pod"), start=1):
        try:
            records.append(parse_book(article, page_url))
        except ValueError as exc:
            errors.append(f"card #{index}: {exc}")
    return records, errors


class PoliteScraper:
    """Scrapes the catalogue respecting robots.txt, a User-Agent and delays."""

    def __init__(
        self,
        base_url: str = BASE_URL,
        pages: int = DEFAULT_PAGES,
        delay: float = DEFAULT_DELAY,
        user_agent: str = USER_AGENT,
        max_retries: int = MAX_RETRIES,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.pages = pages
        self.delay = delay
        self.user_agent = user_agent
        self.max_retries = max_retries
        self.client = httpx.Client(
            headers={"User-Agent": user_agent},
            follow_redirects=True,
            timeout=TIMEOUT_SECONDS,
        )
        self.robots = self._load_robots()

    def _load_robots(self) -> robotparser.RobotFileParser:
        """Fetch and honour robots.txt. Absence means "allowed by default"."""
        parser = robotparser.RobotFileParser()
        try:
            response = self.client.get(f"{self.base_url}/robots.txt")
            if response.status_code == 200:
                parser.parse(response.text.splitlines())
            else:
                parser.parse([])  # no robots.txt -> nothing disallowed
        except httpx.HTTPError:
            parser.parse([])
        return parser

    def is_allowed(self, url: str) -> bool:
        return self.robots.can_fetch(self.user_agent, url)

    def _polite_wait(self) -> None:
        time.sleep(self.delay)

    def fetch(self, url: str) -> tuple[str, int]:
        """GET a page politely, with retries and backoff. Returns (html, attempts)."""
        if not self.is_allowed(url):
            raise RobotsDisallowedError(f"robots.txt forbids fetching {url}")
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            self._polite_wait()
            try:
                response = self.client.get(url)
            except httpx.HTTPError as exc:
                last_error = exc
            else:
                if response.status_code == 200 and response.text:
                    return response.text, attempt
                if response.status_code in (404, 410):
                    raise PageMissingError(f"{url} -> HTTP {response.status_code}")
                last_error = ScrapeError(f"{url} -> HTTP {response.status_code}")
            if attempt < self.max_retries:
                time.sleep(self.delay * attempt)  # gentle backoff
        raise ScrapeError(
            f"failed to fetch {url} after {self.max_retries} attempts: {last_error}"
        )

    def scrape(self, output_dir: Path) -> dict:
        """Collect `pages` catalogue pages, validate every record, save JSON.

        Returns the run report. Never raises for a broken page -- the failure
        is captured in the report so the other pages still complete.
        """
        books: list[dict] = []
        page_stats: list[dict] = []
        started = time.time()

        for page_no in range(1, self.pages + 1):
            url = f"{self.base_url}/catalogue/page-{page_no}.html"
            stat: dict = {
                "page": page_no,
                "url": url,
                "status": "ok",
                "rows": 0,
                "validated": 0,
                "skipped_rows": [],
                "error": None,
            }
            try:
                html, attempts = self.fetch(url)
                rows, row_errors = parse_books(html, url)
                stat["rows"] = len(rows)
                stat["skipped_rows"] = row_errors
                stat["attempts"] = attempts
                for row in rows:
                    try:
                        books.append(Book(**row).model_dump())
                        stat["validated"] += 1
                    except Exception as exc:  # noqa: BLE001 - schema violations
                        stat["skipped_rows"].append(f"schema error: {exc}")
            except (RobotsDisallowedError, PageMissingError, ScrapeError) as exc:
                stat["status"] = "failed"
                stat["error"] = str(exc)
            page_stats.append(stat)

        report = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "base_url": self.base_url,
            "user_agent": self.user_agent,
            "delay_seconds": self.delay,
            "robots_checked": self.robots is not None,
            "pages_requested": self.pages,
            "pages_ok": sum(1 for s in page_stats if s["status"] == "ok"),
            "pages_failed": sum(1 for s in page_stats if s["status"] == "failed"),
            "books_collected": len(books),
            "elapsed_seconds": round(time.time() - started, 2),
            "pages": page_stats,
        }

        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "books.json").write_text(
            json.dumps(books, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (output_dir / "scrape_report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pages", type=int, default=DEFAULT_PAGES, help="pages to scrape")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY, help="seconds between requests")
    parser.add_argument("--output", type=Path, default=Path("."), help="output directory")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    scraper = PoliteScraper(pages=args.pages, delay=args.delay)
    report = scraper.scrape(args.output)
    summary = (
        f"Scraped {report['pages_ok']}/{report['pages_requested']} pages, "
        f"{report['books_collected']} books collected in {report['elapsed_seconds']}s."
    )
    print(summary)
    failed = [s for s in report["pages"] if s["status"] == "failed"]
    for stat in failed:
        print(f"  FAILED page {stat['page']}: {stat['error']}")
    print(f"Output written to: {args.output / 'books.json'}")
    print(f"Report written to: {args.output / 'scrape_report.json'}")


if __name__ == "__main__":
    main()