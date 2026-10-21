"""Collect DerStandard articles for one Vienna date in an isolated CSV run.

The crawler selects entries from the corresponding month sitemap, validates HTTPS
and domain restrictions before every request (including redirects), skips URLs
already recorded in earlier daily CSVs, and atomically writes the four-column
result beneath the configured daily-data root.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import tempfile
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup, FeatureNotFound


VIENNA = ZoneInfo("Europe/Vienna")
COOKIES = {"DSGVO_ZUSAGE_V1": "true"}
CSV_COLUMNS = ("url", "lastmod", "author", "content")
REQUEST_TIMEOUT = (10, 45)
EXPECTED_ARTICLE_DOMAIN = "derstandard.at"
ARTICLE_REQUEST_DELAY_SECONDS = 1.0
MAX_REDIRECTS = 5
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DAILY_OUTPUT_ROOT = Path(
    os.environ.get("AIRFLOW_DAILY_DATA_DIR", str(PROJECT_ROOT / "airflow" / "daily_data"))
)


def parse_target_date(value: str) -> date:
    """Parse a strict ISO calendar date supplied by the DAG or CLI.

    Args:
        value: Date text in canonical ``YYYY-MM-DD`` form.

    Returns:
        The parsed calendar date.

    Raises:
        argparse.ArgumentTypeError: If parsing fails or the input is not
            canonical ISO form.
    """
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("target-date must use YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise argparse.ArgumentTypeError("target-date must use YYYY-MM-DD")
    return parsed


def lastmod_local_date(value: str) -> date:
    """Return lastmod's Europe/Vienna calendar date.

    Date-only or timezone-naive sitemap values are interpreted as Vienna local
    civil time; offset-aware values are converted to Europe/Vienna first.
    """
    normalized = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=VIENNA)
    return parsed.astimezone(VIENNA).date()


def validate_derstandard_url(url: str) -> str:
    """Validate and return an HTTPS URL on DerStandard's domain.

    Args:
        url: Absolute URL from a sitemap entry or HTTP redirect.

    Returns:
        The parsed URL with its original path and query retained.

    Raises:
        ValueError: If the scheme, host, port or user-info fields are outside the
            allow-list. The hostname check also permits true subdomains while
            rejecting lookalike suffixes.
    """
    parsed = urlsplit(url.strip())
    hostname = (parsed.hostname or "").lower().rstrip(".")
    try:
        valid_port = parsed.port in (None, 443)
    except ValueError:
        valid_port = False
    if (
        parsed.scheme.lower() != "https"
        or not hostname
        or not (hostname == EXPECTED_ARTICLE_DOMAIN or hostname.endswith("." + EXPECTED_ARTICLE_DOMAIN))
        or not valid_port
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("URL is outside the allowed HTTPS DerStandard domain")
    return parsed.geturl()


def _get_derstandard_response(url: str, *, delay_before: bool = False):
    """Fetch a validated DerStandard URL without automatically following redirects.

    Args:
        url: Initial URL to validate and request.
        delay_before: Whether to pause before the initial request, used to space
            article requests.

    Returns:
        The successful ``requests.Response`` for the caller to inspect or parse.

    Raises:
        ValueError: If the initial or a redirect URL fails the domain/security
            allow-list.
        requests.RequestException: For transport, timeout or HTTP-status errors.
        RuntimeError: For malformed or excessive redirects.

    Each redirect target is resolved and validated before another request is sent.
    """
    current_url = validate_derstandard_url(url)
    if delay_before:
        time.sleep(ARTICLE_REQUEST_DELAY_SECONDS)

    for redirect_count in range(MAX_REDIRECTS + 1):
        response = requests.get(
            current_url,
            cookies=COOKIES,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=False,
        )
        if not response.is_redirect:
            response.raise_for_status()
            return response

        location = response.headers.get("Location")
        response.close()
        if not location or redirect_count == MAX_REDIRECTS:
            raise RuntimeError("DerStandard request returned an invalid or excessive redirect")
        # Validate the resolved Location before following it; requests must not escape the host allow-list.
        current_url = validate_derstandard_url(urljoin(current_url, location))
        time.sleep(ARTICLE_REQUEST_DELAY_SECONDS)

    raise RuntimeError("DerStandard request exceeded the redirect limit")


def extract_article_content(url: str) -> tuple[str, str]:
    """Fetch an article and extract its paragraph text and normalized byline.

    Args:
        url: Article URL, revalidated before the HTTP request.

    Returns:
        ``(content, author)``. Article paragraphs are joined with spaces; absent
        bylines use ``kein Autor`` and a missing article body yields empty content.

    Raises:
        URL, request and HTTP errors raised by the validated response helper.

    The page's ``article-origins`` byline takes precedence over the simpler header
    byline when both are present. Paragraphs nested inside ``aside`` elements are
    excluded from the body text.
    """
    response = _get_derstandard_response(url)
    article = BeautifulSoup(response.text, "html.parser")

    author = "kein Autor"
    header = article.find("header")
    if header:
        author_span = header.find("span", class_="simple")
        if author_span:
            author = author_span.get_text(strip=True)

    # The origins block is the more complete byline on pages that expose both forms.
    origins_div = article.find("div", class_="article-origins")
    if origins_div:
        author = origins_div.get_text(strip=True)

    if author != "kein Autor":
        author = re.sub(r"\s*(,|oder|&|/|und)\s*", ",", author)
        authors = [item.strip() for item in author.split(",") if item.strip()]
        author = ", ".join(authors)

    article_body = article.find("div", class_="article-body")
    if article_body is None:
        return "", author

    content = []
    for paragraph in article_body.find_all("p"):
        if paragraph.find_parent("aside") is not None:
            continue
        content.append(paragraph.get_text(strip=True))
    return " ".join(content), author


def _validated_output_path(target_date: date, output_path: Path) -> Path:
    """Resolve and validate the output location for this date and run token.

    Args:
        target_date: Date encoded in the parent date directory.
        output_path: Candidate path supplied by the DAG.

    Returns:
        An absolute, resolved ``articles.csv`` path below ``DAILY_OUTPUT_ROOT``.

    Raises:
        ValueError: If the path escapes the configured root or does not match the
            required date/run-token layout.
    """
    root = DAILY_OUTPUT_ROOT.resolve()
    resolved = output_path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("output path must be inside airflow/daily_data") from exc
    if resolved.name != "articles.csv" or resolved.parent.parent.name != target_date.isoformat():
        raise ValueError("output path must be daily_data/YYYY-MM-DD/<run-token>/articles.csv")
    if not re.fullmatch(r"[0-9a-f]{12}", resolved.parent.name):
        raise ValueError("output path must include a 12-character run token")
    return resolved


def write_daily_csv_atomic(output_path: Path, rows: list[tuple[str, str, str, str]]) -> None:
    """Write the daily CSV through a temporary sibling and atomic replacement.

    Args:
        output_path: Validated destination for this run's ``articles.csv``.
        rows: Tuples ordered as URL, last-modified value, author and article body.

    Side effects:
        Creates the parent directory, writes a header and rows to a temporary file
        in that directory, then replaces the destination. On failure it removes
        the temporary file when possible and re-raises the original exception.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=".articles-", suffix=".tmp", dir=str(output_path.parent)
    )
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(CSV_COLUMNS)
            writer.writerows(rows)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, output_path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def read_previously_seen_urls(current_output: Path) -> set[str]:
    """Collect URLs recorded in prior run-scoped daily CSV files.

    Args:
        current_output: CSV being produced now; it is excluded from the scan.

    Returns:
        A set of URLs from valid daily CSVs under ``DAILY_OUTPUT_ROOT``.

    Raises:
        ValueError: If a discovered CSV resolves outside the root, violates the
            date/token layout, has an unexpected header or contains malformed rows.
        OSError: If a prior CSV cannot be read.

    This history is local CSV-based de-duplication; the importer separately checks
    the database before inserting an article.
    """
    root = DAILY_OUTPUT_ROOT.resolve()
    current = current_output.resolve()
    seen_urls: set[str] = set()
    if not root.exists():
        return seen_urls

    # Resolve each candidate to reject symlinked files that point outside daily_data.
    for csv_path in root.rglob("articles.csv"):
        resolved = csv_path.resolve()
        if resolved == current:
            continue
        try:
            relative = resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError("a prior daily CSV resolves outside the daily output root") from exc
        if (
            len(relative.parts) != 3
            or relative.name != "articles.csv"
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", relative.parts[0])
            or not re.fullmatch(r"[0-9a-f]{12}", relative.parts[1])
        ):
            raise ValueError("unexpected prior daily CSV path; refusing ambiguous URL history")
        path_date = date.fromisoformat(relative.parts[0])
        if path_date.isoformat() != relative.parts[0]:
            raise ValueError("unexpected date folder in prior daily CSV")
        with resolved.open("r", newline="", encoding="utf-8") as stream:
            reader = csv.DictReader(stream)
            if tuple(reader.fieldnames or ()) != CSV_COLUMNS:
                raise ValueError(f"invalid header in prior daily CSV: {relative}")
            for row in reader:
                if None in row or not row.get("url"):
                    raise ValueError(f"malformed prior daily CSV row in {relative}")
                seen_urls.add(row["url"])
    return seen_urls


def select_sitemap_articles(
    sitemap_content: bytes, target_date: date, seen_urls: set[str]
) -> list[tuple[str, str]]:
    """Return unseen, allow-listed sitemap URLs whose lastmod matches the target day.

    Args:
        sitemap_content: Response body of the month's XML sitemap.
        target_date: Target calendar date in ``Europe/Vienna``.
        seen_urls: URLs already present in earlier daily CSVs.

    Returns:
        ``(url, lastmod)`` pairs for entries with a parseable timestamp on the
        requested Vienna date. URLs are validated before being returned.

    Raises:
        RuntimeError: If the sitemap has no URL entries or an entry lacks a URL.
        ValueError: If a selected URL is not on the allowed HTTPS domain.
    """
    try:
        soup = BeautifulSoup(sitemap_content, "xml")
    except FeatureNotFound:
        # Fall back to Python's standard-library parser when the XML parser is unavailable.
        soup = BeautifulSoup(sitemap_content, "html.parser")
    url_tags = soup.find_all("url")
    if not url_tags:
        raise RuntimeError("sitemap response contained no url entries")

    selected: list[tuple[str, str]] = []
    for url_tag in url_tags:
        loc_tag = url_tag.find("loc")
        if loc_tag is None or not loc_tag.text:
            raise RuntimeError("sitemap contains a url entry without loc")
        url = loc_tag.text.strip()
        if url in seen_urls:
            continue
        lastmod_tag = url_tag.find("lastmod")
        if lastmod_tag is None or not lastmod_tag.text:
            continue
        lastmod = lastmod_tag.text.strip()
        # Compare the local calendar day, not the timestamp's source timezone date.
        try:
            matches_target_date = lastmod_local_date(lastmod) == target_date
        except (TypeError, ValueError):
            continue
        if matches_target_date:
            selected.append((validate_derstandard_url(url), lastmod))
    return selected


def crawl_day(target_date: date, output_path: Path) -> int:
    """Fetch all unseen sitemap articles dated for one day and write the run CSV.

    Args:
        target_date: Date to match against sitemap ``lastmod`` values in Vienna.
        output_path: Run-specific CSV path constrained to the daily-data root.

    Returns:
        Number of articles with extracted body text written to the CSV.

    Side effects:
        Makes one monthly-sitemap request and article requests for selected URLs,
        observes the configured delay between article requests, reads prior daily
        CSVs for de-duplication, and atomically writes the result CSV.

    Raises:
        URL/request/HTTP, CSV validation or filesystem errors encountered during
        selection, fetching or output writing.
    """
    output_path = _validated_output_path(target_date, output_path)
    sitemap_url = (
        f"https://www.derstandard.at/sitemaps/"
        f"sitemap-{target_date.year}-{target_date.month:02}.xml"
    )
    response = _get_derstandard_response(sitemap_url)
    rows: list[tuple[str, str, str, str]] = []
    previously_seen = read_previously_seen_urls(output_path)
    selected = select_sitemap_articles(response.content, target_date, previously_seen)
    # Retain rows in memory until all selected requests finish, then publish one complete CSV.
    for article_index, (url, lastmod) in enumerate(selected):
        if url in previously_seen:
            continue
        if article_index:
            time.sleep(ARTICLE_REQUEST_DELAY_SECONDS)
        content, author = extract_article_content(url)
        if content:
            rows.append((url, lastmod, author, content))
            previously_seen.add(url)

    write_daily_csv_atomic(output_path, rows)
    print(f"Collected {len(rows)} articles for {target_date.isoformat()}")
    print(f"CSV: {output_path}")
    return len(rows)


def main() -> int:
    """Parse CLI arguments, execute one bounded-by-date crawl and return a status.

    Returns:
        ``0`` when the crawl and CSV write finish; ``1`` when an exception occurs.

    Side effects:
        Performs the network and filesystem work described by ``crawl_day``. A
        non-zero result is intended to prevent the downstream importer from running.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-date", required=True, type=parse_target_date)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        crawl_day(args.target_date, args.output)
    except Exception as exc:
        print(f"Daily crawl failed ({type(exc).__name__}); no import should follow.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
