"""Validate and import one run-scoped daily article CSV into MySQL.

The command accepts only CSV files inside the configured daily-data tree, checks
that every row belongs to the requested Vienna date, resolves credentials from an
Airflow Connection, and inserts unseen article URLs in one transaction.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import sessionmaker

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DAILY_OUTPUT_ROOT = Path(
    os.environ.get("AIRFLOW_DAILY_DATA_DIR", str(PROJECT_ROOT / "airflow" / "daily_data"))
)
VIENNA = ZoneInfo("Europe/Vienna")
CSV_COLUMNS = ("url", "lastmod", "author", "content")
EXPECTED_SCHEMA = "diplomarbeit"

# models.py defines ORM metadata only and does not create a database engine.
sys.path.insert(0, str(PROJECT_ROOT))
from models import Article, Author, extract_authors  # noqa: E402


def parse_target_date(value: str) -> date:
    """Parse a strict ``YYYY-MM-DD`` calendar date for CLI validation.

    Args:
        value: Date text supplied to ``--target-date``.

    Returns:
        A ``datetime.date`` value.

    Raises:
        argparse.ArgumentTypeError: If the input is invalid or not in canonical
            ISO date form.
    """
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("target-date must use YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise argparse.ArgumentTypeError("target-date must use YYYY-MM-DD")
    return parsed


def lastmod_local_date(value: str) -> date:
    """Convert a CSV last-modified timestamp to its Vienna calendar date.

    Naive timestamps are interpreted as local Vienna time; timestamps with an
    explicit offset are converted to that timezone before taking the date.

    Args:
        value: ISO-formatted timestamp from a sitemap-derived CSV row.

    Returns:
        The corresponding ``Europe/Vienna`` calendar date.

    Raises:
        TypeError or ValueError: If the timestamp is not parseable.
    """
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=VIENNA)
    return parsed.astimezone(VIENNA).date()


def read_daily_rows(csv_path: Path, target_date: date) -> list[dict[str, str]]:
    """Validate and load the rows for one date from a run-scoped CSV.

    Args:
        csv_path: Expected ``articles.csv`` below ``daily_data/YYYY-MM-DD/<token>``.
        target_date: Date every row must represent in Vienna local time.

    Returns:
        CSV rows as dictionaries with the exact ``url``, ``lastmod``, ``author``
        and ``content`` fields.

    Raises:
        ValueError: If the resolved path escapes the daily-data root, its run
            structure is invalid, the header/row shape is malformed, or a row is
            empty or dated outside the requested day.
        OSError: If the CSV cannot be opened.
    """
    resolved_root = DAILY_OUTPUT_ROOT.resolve()
    resolved_path = csv_path.resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("CSV must be inside airflow/daily_data") from exc
    if (
        resolved_path.name != "articles.csv"
        or resolved_path.parent.parent.name != target_date.isoformat()
        or not re.fullmatch(r"[0-9a-f]{12}", resolved_path.parent.name)
    ):
        raise ValueError("CSV path does not identify this target date and run")

    # DictReader makes the header part of the contract, so reject schema drift before import.
    with resolved_path.open("r", newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != CSV_COLUMNS:
            raise ValueError("CSV header must be url,lastmod,author,content")
        rows: list[dict[str, str]] = []
        for row_number, row in enumerate(reader, start=2):
            if None in row or any(row.get(column) is None for column in CSV_COLUMNS):
                raise ValueError(f"malformed CSV row {row_number}")
            if not row["url"] or not row["content"]:
                raise ValueError(f"CSV row {row_number} has an empty URL or article body")
            try:
                row_date = lastmod_local_date(row["lastmod"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid lastmod in CSV row {row_number}") from exc
            if row_date != target_date:
                raise ValueError(f"CSV row {row_number} is outside the target date")
            rows.append(row)
    return rows


def create_database_engine(connection_id: str):
    """Build an engine from an Airflow Connection pinned to the daily schema.

    Args:
        connection_id: Airflow connection ID containing the MySQL endpoint and
            credentials.

    Returns:
        A SQLAlchemy engine using the MySQL Connector driver.

    Raises:
        ValueError: If the connection does not target ``diplomarbeit`` or lacks
            required fields.
        Airflow/SQLAlchemy errors: If the connection cannot be resolved or an
            engine cannot be configured.

    Credentials remain in Airflow's connection backend and are not logged here.
    """
    from airflow.hooks.base import BaseHook

    connection = BaseHook.get_connection(connection_id)
    if connection.schema != EXPECTED_SCHEMA:
        raise ValueError(f"Airflow connection must target schema {EXPECTED_SCHEMA!r}")
    if not all((connection.host, connection.login, connection.password, connection.schema)):
        raise ValueError("Airflow connection is missing required database fields")
    url = URL.create(
        "mysql+mysqlconnector",
        username=connection.login,
        password=connection.password,
        host=connection.host,
        port=connection.port or 3306,
        database=connection.schema,
        query={"charset": "utf8mb4"},
    )
    return create_engine(url, pool_pre_ping=True, echo=False)


def import_daily_rows(rows: list[dict[str, str]], engine) -> tuple[int, int]:
    """Insert new articles and authors in a single database transaction.

    Args:
        rows: Validated CSV dictionaries with the four daily pipeline columns.
        engine: SQLAlchemy engine connected to the expected daily schema.

    Returns:
        ``(inserted, skipped)`` counts. Skipped rows are URLs already in MySQL or
        repeated within this CSV.

    Side effects:
        Inserts ``Article`` and any missing ``Author`` rows and their association
        rows. The transaction context commits all changes together or rolls them
        back if an exception escapes.
    """
    if not rows:
        return 0, 0

    Session = sessionmaker(bind=engine, expire_on_commit=False)
    inserted = 0
    skipped = 0
    # Track file-local repeats as well as existing database URLs in the same pass.
    seen_in_file: set[str] = set()
    with Session.begin() as session:
        for row in rows:
            url = row["url"]
            if url in seen_in_file or session.query(Article.id).filter(Article.url == url).first():
                skipped += 1
                seen_in_file.add(url)
                continue
            seen_in_file.add(url)

            article = Article(url=url, lastmod=row["lastmod"], content=row["content"])
            for author_name in extract_authors(row["author"]):
                author = session.query(Author).filter(Author.name == author_name).one_or_none()
                if author is None:
                    author = Author(name=author_name)
                    session.add(author)
                    session.flush()
                article.authors.append(author)
            session.add(article)
            inserted += 1
    return inserted, skipped


def main() -> int:
    """Validate command-line input, import the CSV, and return an exit status.

    Empty, valid CSVs finish without opening a database connection. For non-empty
    input, credentials are loaded from the named Airflow connection and the engine
    is always disposed. Failure output includes only the exception class, avoiding
    driver messages that could contain connection details.

    Returns:
        ``0`` on success or ``1`` when validation/import raises an exception.

    Side effects:
        Reads the daily CSV and, when rows are present, invokes the transactional
        database importer. It does not start a crawler or create database tables.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-date", required=True, type=parse_target_date)
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--airflow-connection-id", default="diplomarbeit_daily_mysql")
    args = parser.parse_args()
    engine = None
    try:
        rows = read_daily_rows(args.csv, args.target_date)
        if not rows:
            print(f"No articles to import for {args.target_date.isoformat()}")
            return 0
        engine = create_database_engine(args.airflow_connection_id)
        inserted, skipped = import_daily_rows(rows, engine)
        print(f"Daily import complete: inserted={inserted}, skipped_existing_or_duplicate={skipped}")
        return 0
    except Exception as exc:
        # Avoid printing exception text because database drivers may include connection details.
        print(f"Daily import failed ({type(exc).__name__}); transaction rolled back.", file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
