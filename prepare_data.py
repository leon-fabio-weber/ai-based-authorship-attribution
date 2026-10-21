"""Reproduce the BERT notebook's author filtering and train/test preparation.

The script reads the SQL and preparation cells from ``notebooks/bert_original.ipynb``,
uses the configured reconstruction database, and writes label and split manifests
to a new project-local output directory. It does not train a model.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd
import sqlalchemy
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import LabelEncoder
from sqlalchemy import text
from sqlalchemy.engine import URL


ROOT = Path(__file__).resolve().parent
NOTEBOOK_PATH = ROOT / "notebooks" / "bert_original.ipynb"
DATABASE_NAME = "diplomarbeit_rekonstruktion_full_2025_exact"
OUTPUT_DIR = Path(
    os.environ.get("BERT_PREPARATION_OUTPUT_DIR", str(ROOT / "training_data"))
).expanduser().resolve()


def source_of(cell: dict[str, object]) -> str:
    """Join a notebook cell's JSON source fragments into executable text."""
    return "".join(cell.get("source", []))


def multi_author_article_count(frame: pd.DataFrame) -> int:
    """Count article IDs that have multiple distinct author labels in a frame.

    Args:
        frame: Joined article-author rows containing ``article_id`` and ``author``.

    Returns:
        The number of article groups with more than one distinct author; empty
        input produces zero.
    """
    if frame.empty:
        return 0
    return int((frame.groupby("article_id")["author"].nunique() > 1).sum())


def main() -> int:
    """Run the notebook's preparation cells and save reproducibility manifests.

    The function validates the notebook cell layout and its SQL shape, requests
    database credentials, executes the query and original preparation/split cells,
    then writes the label map, per-author counts, split manifest and summary JSON.
    The destination must be a new directory under this project. No model training
    or database writes are performed by this preparation workflow.

    Returns:
        ``0`` after the manifests have been written.

    Raises:
        RuntimeError: For an unexpected notebook layout, SQL shape, schema or
            pre-existing/out-of-project output directory.
        OSError, ValueError or SQLAlchemy errors: If source files, credentials,
            database access or output writes fail.

    Side effects:
        Executes the selected notebook cells in memory, queries the configured
        database, and creates CSV/JSON artifacts in ``OUTPUT_DIR``.
    """
    try:
        OUTPUT_DIR.relative_to(ROOT)
    except ValueError as exc:
        raise RuntimeError("Preparation output must remain inside the reconstruction project.") from exc
    if OUTPUT_DIR.exists():
        raise RuntimeError(f"Ausgabeordner existiert bereits; Sicherheitsstopp: {OUTPUT_DIR}")
    with NOTEBOOK_PATH.open("r", encoding="utf-8") as notebook_file:
        notebook = json.load(notebook_file)
    cells = notebook.get("cells", [])
    if len(cells) < 4:
        raise RuntimeError("Das Notebook enthält nicht die erwarteten Zellen 1 bis 3.")
    if any(cells[index].get("cell_type") != "code" for index in (1, 2, 3)):
        raise RuntimeError("Notebook-Zellen 1 bis 3 haben unerwartete Typen.")

    cell1_source = source_of(cells[1])
    engine_lines = [
        line for line in cell1_source.splitlines()
        if line.strip().startswith("engine = sqlalchemy.create_engine(")
    ]
    if len(engine_lines) != 1:
        raise RuntimeError("Unerwartete Verbindungsdefinition in Notebook-Zelle 1.")
    query_match = re.search(r'query\s*=\s*"""(.*?)"""', cell1_source, re.DOTALL)
    if not query_match or not query_match.group(1).lstrip().lower().startswith("select"):
        raise RuntimeError("Sicherheitsstopp: Zelle 1 enthält keine erkennbare SELECT-Abfrage.")
    # Bind the output summary to the exact SQL text extracted from the notebook.
    query_hash = hashlib.sha256(query_match.group(1).encode("utf-8")).hexdigest()

    username = os.environ.get("DIPLOMARBEIT_DB_USER") or input("Lokaler MySQL-Benutzername: ")
    password = os.environ.get("DIPLOMARBEIT_DB_PASSWORD") or getpass.getpass("Passwort (verdeckt): ")
    db_url = URL.create(
        "mysql+mysqlconnector",
        username=username,
        password=password,
        host=os.environ.get("DIPLOMARBEIT_DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("DIPLOMARBEIT_DB_PORT", "3306")),
        database=DATABASE_NAME,
    )
    namespace = {
        "pd": pd,
        "sqlalchemy": sqlalchemy,
        "db_url": db_url,
        "PROJECT_ROOT": ROOT,
        "defaultdict": defaultdict,
        "train_test_split": train_test_split,
        "LabelEncoder": LabelEncoder,
    }
    captured: dict[str, pd.DataFrame] = {}
    original_read_sql = pd.read_sql

    def capture_join_result(*args: object, **kwargs: object) -> pd.DataFrame:
        """Capture a copy of the SQL join result while returning it unchanged."""
        result = original_read_sql(*args, **kwargs)
        captured["join_df"] = result.copy(deep=True)
        return result

    # Capture the unfiltered join for diagnostics while preserving the notebook cell's result.
    pd.read_sql = capture_join_result
    try:
        exec(compile(cell1_source, f"{NOTEBOOK_PATH}:cell1", "exec"), namespace)
    finally:
        pd.read_sql = original_read_sql

    engine = namespace["engine"]
    try:
        with engine.connect() as connection:
            selected_database = connection.execute(text("SELECT DATABASE()")).scalar_one()
        if selected_database != DATABASE_NAME:
            raise RuntimeError(f"Unerwartete Zieldatenbank: {selected_database!r}")

        join_df = captured["join_df"]
        non_placeholder_df = namespace["df"].copy()
        filtered_df = namespace["df_filtered"].copy()

        # Reuse the notebook's preparation cells to preserve its data logic.
        exec(compile(source_of(cells[2]), f"{NOTEBOOK_PATH}:cell2", "exec"), namespace)
        exec(compile(source_of(cells[3]), f"{NOTEBOOK_PATH}:cell3", "exec"), namespace)

        train_df = namespace["train_df"]
        test_df = namespace["test_df"]
        encoder = namespace["label_encoder"]
        author_to_articles = namespace["author_to_articles"]
        train_by_author = namespace["train_article_ids_by_author"]
        test_by_author = namespace["test_article_ids_by_author"]
        # Compare article IDs as well as rows: multi-author articles can appear more than once.
        train_ids = set(train_df["article_id"].unique())
        test_ids = set(test_df["article_id"].unique())
        id_overlap = train_ids & test_ids

        per_author_rows: list[dict[str, object]] = []
        conflicts: list[dict[str, object]] = []
        conflict_count = 0
        reassignments = 0
        for author, article_ids in author_to_articles.items():
            train_rows = int((train_df["author"] == author).sum())
            test_rows = int((test_df["author"] == author).sum())
            per_author_rows.append({
                "author": author,
                "filtered_article_ids": len(article_ids),
                "planned_train_ids": len(train_by_author[author]),
                "planned_test_ids": len(test_by_author[author]),
                "final_train_rows": train_rows,
                "final_test_rows": test_rows,
                "label_id": int(encoder.transform([author])[0]),
            })

        # Record when author-wise split plans disagree for a shared multi-author article.
        for article_id, group in filtered_df.groupby("article_id"):
            authors = group["author"].unique().tolist()
            if len(authors) < 2:
                continue
            plans = {author: article_id in test_by_author[author] for author in authors}
            if len(set(plans.values())) > 1:
                conflict_count += 1
                if len(conflicts) < 100:
                    conflicts.append({
                        "article_id": int(article_id),
                        "author_planned_test": plans,
                        "global_split": "test",
                    })
            for author, planned_test in plans.items():
                if not planned_test and article_id in namespace["test_article_ids"]:
                    reassignments += 1

        # Keep the encoder's class order as the authoritative label-ID mapping.
        class_names = [str(name) for name in encoder.classes_]
        label_mapping = {
            name: int(encoder.transform([name])[0]) for name in class_names
        }
        OUTPUT_DIR.mkdir(parents=True)
        # Store row-level labels and article IDs so the produced split can be inspected later.
        train_manifest = train_df[["article_id", "author", "author_id"]].copy()
        train_manifest["split"] = "train"
        test_manifest = test_df[["article_id", "author", "author_id"]].copy()
        test_manifest["split"] = "test"
        pd.concat([train_manifest, test_manifest], ignore_index=True).to_csv(
            OUTPUT_DIR / "split_manifest.csv", index=False, encoding="utf-8"
        )
        pd.DataFrame(per_author_rows).sort_values("author").to_csv(
            OUTPUT_DIR / "per_author_split_counts.csv", index=False, encoding="utf-8"
        )
        (OUTPUT_DIR / "label_mapping.json").write_text(
            json.dumps(label_mapping, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        summary = {
            "database": DATABASE_NAME,
            "source_notebook": str(NOTEBOOK_PATH.relative_to(ROOT)),
            "executed_cells": [1, 2, 3],
            "cell1_sql_sha256": query_hash,
            "filters": {
                "rows_after_join": len(join_df),
                "rows_after_author_not_equal_kein_Autor": len(non_placeholder_df),
                "rows_after_minimum_10_authors": len(filtered_df),
                "authors_after_minimum_filter": len(author_to_articles),
            },
            "classes": class_names,
            "class_count": len(class_names),
            "label_mapping_file": "label_mapping.json",
            "split": {
                "train_rows": len(train_df),
                "test_rows": len(test_df),
                "train_unique_article_ids": len(train_ids),
                "test_unique_article_ids": len(test_ids),
                "article_id_overlap_count": len(id_overlap),
                "article_id_overlap_examples": sorted(int(value) for value in id_overlap)[:100],
                "train_test_rows_sum": len(train_df) + len(test_df),
            },
            "multi_author_articles": {
                "after_join": multi_author_article_count(join_df),
                "after_kein_Autor_filter": multi_author_article_count(non_placeholder_df),
                "after_minimum_filter": multi_author_article_count(filtered_df),
                "authorwise_plan_conflict_count": conflict_count,
                "authorwise_plan_conflict_examples": conflicts,
                "author_rows_moved_to_global_test_by_union": reassignments,
            },
            "per_author_counts_file": "per_author_split_counts.csv",
            "split_manifest_file": "split_manifest.csv",
            "notes": [
                "Notebook-Zellen 1-3 wurden aus dem Original geladen; nur die Engine-Zeile in Zelle 1 wurde ersetzt.",
                "Zelle 2 verwendet list(set(article_ids)) und random_state=42. Die Set-Iteration kann sich zwischen Python-Prozessen unterscheiden; daher kann ein späterer Original-Notebook-Lauf einen anderen Split erzeugen.",
                "Zelle 3 bildet test_article_ids als Vereinigung der autorweisen Test-IDs. Artikel-IDs sind deshalb global disjunkt zwischen train_df und test_df; Mehrfachautoren-Zeilen können gemeinsam ins globale Testset verschoben werden.",
            ],
        }
        (OUTPUT_DIR / "preparation_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps({
            "output_dir": str(OUTPUT_DIR),
            "database": DATABASE_NAME,
            "join_rows": len(join_df),
            "after_no_author_filter": len(non_placeholder_df),
            "after_minimum_filter": len(filtered_df),
            "authors": len(class_names),
            "train_rows": len(train_df),
            "test_rows": len(test_df),
            "train_unique_article_ids": len(train_ids),
            "test_unique_article_ids": len(test_ids),
            "global_article_id_overlap": len(id_overlap),
            "multi_author_filtered": multi_author_article_count(filtered_df),
            "authorwise_plan_conflict_count": conflict_count,
            "authorwise_plan_conflict_examples_saved": len(conflicts),
            "author_rows_moved_to_global_test": reassignments,
            "label_mapping_file": "label_mapping.json",
        }, ensure_ascii=False, indent=2), flush=True)
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
