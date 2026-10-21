"""Load the reconstructed article CSV into the isolated historical MySQL schema.

The guarded command validates saved CSV and BERT preflight evidence before opening
a database connection. It then imports ORM records in 50-article transactions and
writes a local status file so a long-running import can be audited.
"""

from __future__ import annotations

import csv
import getpass
import json
import sys
import time
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import sessionmaker


ROOT = Path(__file__).resolve().parent
RUN_DIR = ROOT / "data"
METADATA_DIR = ROOT / "results" / "metadata"
CSV_PATH = RUN_DIR / "ks.csv"
AUDIT_PATH = METADATA_DIR / "csv_audit.json"
PREFLIGHT_PATH = METADATA_DIR / "import_preflight.json"
FILTER_PREFLIGHT_PATH = METADATA_DIR / "bert_filter_preflight.json"
STATUS_PATH = METADATA_DIR / "full_import_exact_status.json"
DATABASE_NAME = "diplomarbeit_rekonstruktion_full_2025_exact"
EXPECTED_TABLES = {"articles", "authors", "placeholder"}
EXPECTED_COUNTS = {"articles": 633_278, "authors": 12_262, "placeholder": 650_236}
EXPECTED_BERT = {
    "join_rows": 650_236,
    "without_kein_autor": 230_800,
    "authors_after_no_author_filter": 12_261,
    "authors_min_10": 1_135,
    "rows_min_10": 212_096,
}
EXPECTED_COLUMNS = ["url", "lastmod", "author", "content"]

# Reuse the project ORM module without loading the original database runner.
sys.path.insert(0, str(ROOT))
import models as historical_db  # noqa: E402


def table_counts(connection: object) -> dict[str, int]:
    """Return row counts for the three tables used by the reconstruction.

    Args:
        connection: An open SQLAlchemy connection to the target schema.

    Returns:
        Counts keyed by ``articles``, ``authors`` and ``placeholder``.

    The helper issues only ``SELECT COUNT(*)`` statements; it does not mutate data.
    """
    return {
        table: int(connection.execute(text(f"SELECT COUNT(*) FROM `{table}`")).scalar_one())
        for table in ("articles", "authors", "placeholder")
    }


def write_status(status: dict[str, object]) -> None:
    """Persist the current import status as formatted UTF-8 JSON.

    Args:
        status: JSON-serializable progress or completion fields.

    Side effects:
        Creates or replaces ``STATUS_PATH``. This status file is local audit
        output and is separate from the database transaction state.
    """
    STATUS_PATH.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")


def read_preflight() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    """Load and validate the saved CSV, import and BERT-filter preflight reports.

    Returns:
        The three parsed JSON objects, in audit/import/BERT order.

    Raises:
        OSError or json.JSONDecodeError: If a required report cannot be read.
        RuntimeError: If row counts, CSV structure or author/filter counts differ
            from the recorded reconstruction expectations.
    """
    audit = json.loads(AUDIT_PATH.read_text(encoding="utf-8"))
    import_check = json.loads(PREFLIGHT_PATH.read_text(encoding="utf-8"))
    bert_check = json.loads(FILTER_PREFLIGHT_PATH.read_text(encoding="utf-8"))
    if audit.get("rows_read_and_written") != EXPECTED_COUNTS["articles"]:
        raise RuntimeError("CSV-Audit meldet eine unerwartete Artikelanzahl.")
    if audit.get("output_columns") != EXPECTED_COLUMNS or audit.get("malformed_rows") != 0:
        raise RuntimeError("CSV-Audit meldet ein unerwartetes Format oder fehlerhafte Zeilen.")
    if import_check.get("rows_csv_dictreader") != EXPECTED_COUNTS["articles"]:
        raise RuntimeError("CSV-Importvorprüfung meldet eine unerwartete Zeilenzahl.")
    if import_check.get("field_parse_anomalies") != 0:
        raise RuntimeError("CSV-Importvorprüfung meldet fehlerhafte Felder.")
    if import_check.get("unique_extracted_authors") != EXPECTED_COUNTS["authors"]:
        raise RuntimeError("Autorenanzahl weicht von der Vorprüfung ab.")
    if import_check.get("author_article_associations_expected") != EXPECTED_COUNTS["placeholder"]:
        raise RuntimeError("Zuordnungsanzahl weicht von der Vorprüfung ab.")
    if bert_check.get("joined_rows_estimated_before_filter") != EXPECTED_BERT["join_rows"]:
        raise RuntimeError("BERT-JOIN-Vorprüfung weicht ab.")
    checked_bert = {
        "without_kein_autor": bert_check.get("join_rows_after_no_author_filter"),
        "authors_after_no_author_filter": bert_check.get("distinct_authors_after_no_author_filter"),
        "authors_min_10": bert_check.get("authors_with_at_least_10_associations"),
        "rows_min_10": bert_check.get("rows_after_original_minimum_filter"),
    }
    if checked_bert != {key: EXPECTED_BERT[key] for key in checked_bert}:
        raise RuntimeError(f"BERT-Filtervorprüfung weicht ab: {checked_bert}")
    return audit, import_check, bert_check


def main() -> int:
    """Run the guarded full import and verify its resulting database counts.

    The command requires ``--run-import``, validates all local preflight records,
    obtains credentials interactively, and refuses a non-empty or mismatched
    target schema. It commits in batches of 50 articles, writes progress to
    ``STATUS_PATH``, then checks the BERT query counts used by data preparation.

    Returns:
        ``0`` when the import and expected counts match; ``1`` for a count
        mismatch. Without the explicit flag, returns ``2`` without connecting.

    Side effects:
        Writes the local status JSON and inserts ORM rows into the configured
        reconstruction database. Earlier 50-row commits may remain if a later
        batch fails; the status records that condition for manual recovery.
    """
    if "--run-import" not in sys.argv[1:]:
        print("Full import is guarded; no database connection or import was started.")
        return 2
    if not CSV_PATH.is_file() or STATUS_PATH.exists():
        raise RuntimeError("CSV fehlt oder ein Importstatus existiert bereits; Sicherheitsstopp.")
    audit, import_check, bert_check = read_preflight()

    # Validate the target and preflight evidence before requesting credentials.
    username = input("Lokaler MySQL-Benutzername: ")
    password = getpass.getpass("Passwort (verdeckt): ")
    db_url = URL.create(
        "mysql+pymysql",
        username=username,
        password=password,
        host="127.0.0.1",
        database=DATABASE_NAME,
        query={"charset": "utf8mb4"},
    )
    engine = create_engine(db_url, pool_pre_ping=True, pool_recycle=1800)
    session = None
    article_count = 0
    commit_count = 0
    start = time.time()
    status: dict[str, object] = {
        "status": "running",
        "database": DATABASE_NAME,
        "csv": str(CSV_PATH),
        "expected_articles": EXPECTED_COUNTS["articles"],
        "committed_articles": 0,
        "started_unix": start,
        "import_logic": "db.py-derived models.py ORM classes, extract_authors and 50-article transaction cadence",
    }
    write_status(status)

    try:
        with engine.connect() as connection:
            existing_tables = set(
                connection.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema=:db"
                    ),
                    {"db": DATABASE_NAME},
                ).scalars()
            )
            before = table_counts(connection)
            author_collation = connection.execute(text(
                "SELECT collation_name FROM information_schema.columns "
                "WHERE table_schema=:db AND table_name='authors' AND column_name='name'"
            ), {"db": DATABASE_NAME}).scalar_one_or_none()
            if author_collation != "utf8mb4_0900_bin":
                raise RuntimeError(f"Autorenkollation unerwartet: {author_collation!r}")
        if existing_tables != EXPECTED_TABLES or any(before.values()):
            status.update({"status": "stopped_preimport", "tables": sorted(existing_tables), "counts_before": before})
            write_status(status)
            raise RuntimeError("Tabellen fehlen/unerwartet oder enthalten Daten; Import nicht gestartet.")

        # Cache author ORM objects by the exact stored name to avoid repeated queries.
        Session = sessionmaker(bind=engine)
        session = Session()
        existing_authors = {author.name: author for author in session.query(historical_db.Author).all()}
        with CSV_PATH.open("r", encoding="utf-8", newline="") as csv_file:
            reader = csv.DictReader(csv_file)
            if reader.fieldnames != EXPECTED_COLUMNS:
                raise RuntimeError(f"Unerwartete CSV-Kopfzeile: {reader.fieldnames!r}")
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    raise RuntimeError(f"Ungültige CSV-Zeile vor Datensatz {article_count + 1}.")
                article = historical_db.Article(
                    url=row["url"], lastmod=row["lastmod"], content=row["content"]
                )
                session.add(article)
                session.flush()
                for author_name in historical_db.extract_authors(row["author"]):
                    author = existing_authors.get(author_name)
                    if author is None:
                        author = historical_db.Author(name=author_name)
                        session.add(author)
                        session.flush()
                        existing_authors[author_name] = author
                    article.authors.append(author)
                article_count += 1
                # Commit bounded batches so the import does not hold every row in one transaction.
                if article_count % 50 == 0:
                    session.commit()
                    commit_count += 1
                    if article_count % 5_000 == 0:
                        status.update({"committed_articles": article_count, "commit_batches": commit_count})
                        write_status(status)
                        print(f"{article_count} Artikel verarbeitet.", flush=True)
        session.commit()
        commit_count += 1
        session.close()
        session = None

        # Verify the joins and filters used by the BERT preparation query after insertion.
        with engine.connect() as connection:
            after = table_counts(connection)
            join_rows = int(connection.execute(text(
                "SELECT COUNT(*) FROM articles a "
                "JOIN placeholder p ON a.id=p.article_id "
                "JOIN authors auth ON p.author_id=auth.id"
            )).scalar_one())
            non_placeholder_rows = int(connection.execute(text(
                "SELECT COUNT(*) FROM articles a "
                "JOIN placeholder p ON a.id=p.article_id "
                "JOIN authors auth ON p.author_id=auth.id WHERE auth.name <> 'kein Autor'"
            )).scalar_one())
            non_placeholder_authors = int(connection.execute(text(
                "SELECT COUNT(DISTINCT auth.name) FROM articles a "
                "JOIN placeholder p ON a.id=p.article_id "
                "JOIN authors auth ON p.author_id=auth.id WHERE auth.name <> 'kein Autor'"
            )).scalar_one())
            min10_authors = int(connection.execute(text(
                "SELECT COUNT(*) FROM (SELECT auth.name FROM articles a "
                "JOIN placeholder p ON a.id=p.article_id "
                "JOIN authors auth ON p.author_id=auth.id "
                "WHERE auth.name <> 'kein Autor' GROUP BY auth.name HAVING COUNT(*) >= 10) q"
            )).scalar_one())
            min10_rows = int(connection.execute(text(
                "SELECT COUNT(*) FROM articles a "
                "JOIN placeholder p ON a.id=p.article_id "
                "JOIN authors auth ON p.author_id=auth.id "
                "WHERE auth.name <> 'kein Autor' AND auth.name IN "
                "(SELECT name FROM (SELECT auth2.name AS name FROM articles a2 "
                "JOIN placeholder p2 ON a2.id=p2.article_id "
                "JOIN authors auth2 ON p2.author_id=auth2.id "
                "WHERE auth2.name <> 'kein Autor' GROUP BY auth2.name HAVING COUNT(*) >= 10) q)"
            )).scalar_one())
        actual_bert = {
            "join_rows": join_rows,
            "without_kein_autor": non_placeholder_rows,
            "authors_after_no_author_filter": non_placeholder_authors,
            "authors_min_10": min10_authors,
            "rows_min_10": min10_rows,
        }
        expected_success = after == EXPECTED_COUNTS and actual_bert == EXPECTED_BERT and article_count == EXPECTED_COUNTS["articles"]
        status.update({
            "status": "completed" if expected_success else "completed_with_count_mismatch",
            "counts_before": before,
            "counts_after": after,
            "bert_sql_counts": actual_bert,
            "csv_rows_imported": article_count,
            "committed_batches": commit_count,
            "elapsed_seconds": round(time.time() - start, 2),
            "csv_bytes": audit["output_bytes"],
            "preflight_author_associations": import_check["author_article_associations_expected"],
            "preflight_bert_filters": bert_check,
        })
        write_status(status)
        print(json.dumps(status, ensure_ascii=False, indent=2), flush=True)
        return 0 if expected_success else 1
    except BaseException as error:
        # Record the last known committed boundary; this cannot undo prior batches.
        if session is not None:
            session.rollback()
            session.close()
        if status.get("status") == "running":
            status.update({
                "status": "failed_or_interrupted",
                "rows_started": article_count,
                "committed_articles_last_known": (article_count // 50) * 50,
                "commit_batches_last_known": commit_count,
                "elapsed_seconds": round(time.time() - start, 2),
                "error_type": type(error).__name__,
                "error": str(error),
                "note": "Vorige 50er-Batches können bestehen bleiben; nicht automatisch wiederholen oder Tabellen löschen.",
            })
            write_status(status)
        raise
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())

