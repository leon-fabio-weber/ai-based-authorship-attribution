"""Prepare author labels and article-disjoint train/test splits from MySQL.

This standalone reconstruction preserves the historical filters and per-author
80/20 split, then assigns every article to one global split. It writes local CSV
and JSON artifacts; all database access is read-only.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
from collections import defaultdict
from pathlib import Path

import pandas as pd
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import LabelEncoder
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import URL


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "training_data"
MIN_ARTICLES_PER_AUTHOR = 10
TEST_SIZE = 0.2
SPLIT_RANDOM_STATE = 42
ARTICLE_QUERY = """
SELECT a.id AS article_id, a.content, auth.name AS author
FROM articles AS a
JOIN placeholder AS p ON a.id = p.article_id
JOIN authors AS auth ON p.author_id = auth.id
"""
PREPARED_COLUMNS = ["article_id", "author", "author_id", "content"]


def build_database_engine() -> tuple[object, str]:
    """Create a read-only MySQL engine from local environment settings.

    ``DATABASE_NAME`` is mandatory so this utility cannot silently select the
    historical reconstruction schema. Credentials can be supplied through
    ``DIPLOMARBEIT_DB_USER`` and ``DIPLOMARBEIT_DB_PASSWORD`` or entered
    interactively. The returned database name is included in the manifest.
    """
    database_name = os.environ.get("DATABASE_NAME", "").strip()
    if not database_name:
        raise RuntimeError("Set DATABASE_NAME explicitly before preparing data.")

    username = os.environ.get("DIPLOMARBEIT_DB_USER") or input("MySQL username: ")
    password = os.environ.get("DIPLOMARBEIT_DB_PASSWORD") or getpass.getpass(
        "MySQL password (hidden): "
    )
    if not username or not password:
        raise RuntimeError("A MySQL username and password are required.")

    url = URL.create(
        "mysql+mysqlconnector",
        username=username,
        password=password,
        host=os.environ.get("DIPLOMARBEIT_DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("DIPLOMARBEIT_DB_PORT", "3306")),
        database=database_name,
        query={"charset": "utf8mb4"},
    )
    engine = create_engine(url, pool_pre_ping=True)

    # Apply the read-only mode to every physical DB connection before SELECTs run.
    @event.listens_for(engine, "connect")
    def _set_read_only(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("SET SESSION TRANSACTION READ ONLY")
        finally:
            cursor.close()

    return engine, database_name


def prepare_splits(
    joined_articles: pd.DataFrame,
    *,
    minimum_articles_per_author: int = MIN_ARTICLES_PER_AUTHOR,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, int], pd.DataFrame, dict[str, int]]:
    """Filter joined rows, encode authors, and create article-disjoint splits.

    The procedure excludes ``kein Autor``, applies the minimum per-author row
    count, performs an 80/20 split of article IDs independently for each author,
    then unions those test IDs so a multi-author article cannot occur in both
    global partitions. Sorting IDs before splitting is a reproducibility measure;
    it retains the same split strategy but may differ from historical set order.

    Args:
        joined_articles: Rows with ``article_id``, ``content`` and ``author``.
        minimum_articles_per_author: Minimum joined rows required for a class.

    Returns:
        Train rows, test rows, author-to-label mapping, split manifest, and counts
        keyed by author. Both data frames include the encoded ``author_id``.

    Raises:
        ValueError: If required columns or usable author rows are absent.
    """
    required = {"article_id", "content", "author"}
    missing = required.difference(joined_articles.columns)
    if missing:
        raise ValueError(f"Input data is missing required columns: {sorted(missing)}")

    filtered = joined_articles.loc[
        joined_articles["author"].notna()
        & (joined_articles["author"] != "kein Autor")
    ].copy()
    if filtered["content"].isna().any():
        raise ValueError("Joined input contains articles without text content.")
    author_counts = filtered["author"].value_counts()
    retained_authors = author_counts[author_counts >= minimum_articles_per_author].index
    filtered = filtered.loc[filtered["author"].isin(retained_authors)].copy()
    if filtered.empty:
        raise ValueError("No author classes remain after applying the configured filter.")

    # Match the original author-wise split while making article-ID order stable.
    author_to_articles: dict[str, set[int]] = defaultdict(set)
    for row in filtered[["author", "article_id"]].itertuples(index=False):
        author_to_articles[str(row.author)].add(int(row.article_id))

    test_article_ids: set[int] = set()
    for article_ids in author_to_articles.values():
        ordered_ids = sorted(article_ids)
        if len(ordered_ids) < 2:
            raise ValueError("Each retained author needs at least two distinct article IDs to split.")
        _, author_test_ids = train_test_split(
            ordered_ids,
            test_size=TEST_SIZE,
            random_state=SPLIT_RANDOM_STATE,
        )
        test_article_ids.update(int(article_id) for article_id in author_test_ids)

    all_article_ids = set(int(value) for value in filtered["article_id"].unique())
    train_article_ids = all_article_ids - test_article_ids
    train_df = filtered.loc[filtered["article_id"].isin(train_article_ids)].copy()
    test_df = filtered.loc[filtered["article_id"].isin(test_article_ids)].copy()

    # Fit on training rows, as in the source notebook; all retained classes should
    # remain represented because each has at least ten distinct article IDs.
    encoder = LabelEncoder()
    train_df["author_id"] = encoder.fit_transform(train_df["author"])
    test_df["author_id"] = encoder.transform(test_df["author"])
    label_mapping = {
        str(author): int(encoder.transform([author])[0]) for author in encoder.classes_
    }

    train_df = train_df[PREPARED_COLUMNS].reset_index(drop=True)
    test_df = test_df[PREPARED_COLUMNS].reset_index(drop=True)
    manifest = pd.concat(
        [
            train_df[["article_id", "author", "author_id"]].assign(split="train"),
            test_df[["article_id", "author", "author_id"]].assign(split="test"),
        ],
        ignore_index=True,
    )
    retained_counts = {
        str(author): int(count)
        for author, count in filtered["author"].value_counts().sort_index().items()
    }
    return train_df, test_df, label_mapping, manifest, retained_counts


def write_preparation_artifacts(
    output_dir: Path,
    *,
    train_df: pd.DataFrame,
    test_df: pd.DataFrame,
    label_mapping: dict[str, int],
    split_manifest: pd.DataFrame,
    author_counts: dict[str, int],
    database_name: str,
    query_sha256: str,
) -> None:
    """Write prepared text splits and matching label/split metadata locally."""
    output_dir.mkdir(parents=True)
    train_df.to_csv(output_dir / "train.csv", index=False, encoding="utf-8")
    test_df.to_csv(output_dir / "test.csv", index=False, encoding="utf-8")
    split_manifest.to_csv(output_dir / "split_manifest.csv", index=False, encoding="utf-8")
    (output_dir / "label_mapping.json").write_text(
        json.dumps(label_mapping, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    summary = {
        "database": database_name,
        "query_sha256": query_sha256,
        "filters": {
            "excluded_author": "kein Autor",
            "minimum_joined_rows_per_author": MIN_ARTICLES_PER_AUTHOR,
        },
        "split": {
            "test_size_per_author": TEST_SIZE,
            "random_state": SPLIT_RANDOM_STATE,
            "train_rows": len(train_df),
            "test_rows": len(test_df),
            "train_unique_article_ids": int(train_df["article_id"].nunique()),
            "test_unique_article_ids": int(test_df["article_id"].nunique()),
            "article_id_overlap": len(
                set(train_df["article_id"]) & set(test_df["article_id"])
            ),
            "class_count": len(label_mapping),
            "article_counts_by_author": author_counts,
        },
        "artifacts": ["train.csv", "test.csv", "split_manifest.csv", "label_mapping.json"],
        "note": (
            "Article IDs are sorted before each per-author split for stable ordering. "
            "The test-ID union keeps each multi-author article in one global partition."
        ),
    }
    (output_dir / "preparation_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def parse_args() -> argparse.Namespace:
    """Parse the required output directory for a new preparation run."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        required=True,
        help="new output directory below training_data/; existing directories are rejected",
    )
    return parser.parse_args()


def main() -> int:
    """Read the configured MySQL data and write a fresh prepared dataset."""
    args = parse_args()
    requested_output = Path(args.output_dir).expanduser()
    if not requested_output.is_absolute():
        requested_output = PROJECT_ROOT / requested_output
    requested_output = requested_output.absolute()
    if DEFAULT_OUTPUT_ROOT.exists() and (
        not DEFAULT_OUTPUT_ROOT.is_dir()
        or DEFAULT_OUTPUT_ROOT.is_symlink()
        or getattr(DEFAULT_OUTPUT_ROOT, "is_junction", lambda: False)()
    ):
        raise ValueError("training_data/ must be a real project directory, not a link.")
    output_root = DEFAULT_OUTPUT_ROOT.resolve()
    try:
        output_root.relative_to(PROJECT_ROOT.resolve())
    except ValueError as exc:
        raise ValueError("Preparation output root must remain inside the project.") from exc
    if requested_output.parent.resolve() != output_root:
        raise ValueError("Preparation output must be a direct child of training_data/.")
    if (
        os.path.lexists(requested_output)
        or requested_output.is_symlink()
        or getattr(requested_output, "is_junction", lambda: False)()
    ):
        raise FileExistsError("Preparation output must be a new directory.")

    engine, database_name = build_database_engine()
    try:
        # The connection event enforces read-only mode before this SELECT is issued.
        with engine.connect() as connection:
            joined_articles = pd.read_sql(text(ARTICLE_QUERY), connection)
        query_sha256 = hashlib.sha256(ARTICLE_QUERY.encode("utf-8")).hexdigest()
        prepared = prepare_splits(joined_articles)
        write_preparation_artifacts(
            requested_output,
            train_df=prepared[0],
            test_df=prepared[1],
            label_mapping=prepared[2],
            split_manifest=prepared[3],
            author_counts=prepared[4],
            database_name=database_name,
            query_sha256=query_sha256,
        )
        print(
            json.dumps(
                {
                    "output_dir": str(requested_output),
                    "classes": len(prepared[2]),
                    "train_rows": len(prepared[0]),
                    "test_rows": len(prepared[1]),
                    "query_sha256": query_sha256,
                },
                indent=2,
            )
        )
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
