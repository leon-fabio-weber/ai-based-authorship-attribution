"""Run the source notebook's BERT training workflow in a new output directory.

The runner validates the notebook, database query, local base-model snapshot and
destination before executing the training cells. Training requires the explicit
``--start-training`` flag and a new direct child of ``training/``; existing runs
and checkpoints are not valid destinations.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NOTEBOOK_PATH = ROOT / "notebooks" / "bert_original.ipynb"
DATABASE_NAME = "diplomarbeit_rekonstruktion_full_2025_exact"
BASE_MODEL_SOURCE = ROOT / "base_model" / "model_source"
TRAINING_ROOT = ROOT / "training"
MODEL_ID = "bert-base-german-cased"
MODEL_REVISION = "0b061b4f7ce140d10dce6b6bd8a77da8ec80931b"


def source_of(cell: dict[str, object]) -> str:
    """Join a notebook cell's JSON source fragments into executable text."""
    return "".join(cell.get("source", []))


def arguments() -> argparse.Namespace:
    """Parse the explicit training authorization and output-directory options.

    Returns:
        Parsed command-line arguments. Parsing does not execute notebook cells.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--start-training",
        action="store_true",
        help="explicitly authorize execution of the original trainer.train() cell",
    )
    parser.add_argument(
        "--output-dir",
        help="new, unused directory inside training/ for this run",
    )
    return parser.parse_args()


def main() -> int:
    """Validate inputs, prepare run metadata and execute the original training cells.

    The command exits before notebook execution unless ``--start-training`` is
    supplied. It requires a new direct child of ``training/``, checks the local
    model snapshot and notebook, verifies that the extracted SQL is a SELECT with
    a read-only session guard, and then runs notebook cells 0–9. Cell 9 invokes the
    Trainer's training operation.

    Returns:
        ``0`` when the training cell completes. Validation or execution errors
        propagate to the caller and produce a non-zero process exit.

    Side effects:
        Creates the requested run directory, reads from the configured database,
        and writes manifests, logs, checkpoints and final model files beneath that
        new directory. Existing training directories are rejected before creation.
    """
    args = arguments()
    if not args.start_training:
        print("Training is guarded; no notebook cells or database queries were run.")
        return 2
    if not args.output_dir:
        raise SystemExit("--output-dir must name a new directory under training/.")
    if not NOTEBOOK_PATH.is_file():
        raise FileNotFoundError(f"Shared source notebook is missing: {NOTEBOOK_PATH}")
    if not BASE_MODEL_SOURCE.is_dir():
        raise FileNotFoundError(f"Local base model is missing: {BASE_MODEL_SOURCE}")

    requested_output = Path(args.output_dir).expanduser()
    if not requested_output.is_absolute():
        requested_output = ROOT / requested_output
    requested_output = requested_output.absolute()
    project_root = ROOT.resolve()
    training_root = TRAINING_ROOT.resolve()
    try:
        training_root.relative_to(project_root)
    except ValueError as exc:
        raise ValueError("Training root must remain inside the reconstruction project.") from exc

    # Restrict output to a direct child so a run cannot target an existing run's subdirectory.
    if requested_output.parent.resolve() != training_root:
        raise ValueError("Training output must be a new direct child of training/.")

    # Treat junctions and symbolic links as occupied paths, even if their targets differ.
    is_junction = getattr(requested_output, "is_junction", lambda: False)()
    if os.path.lexists(requested_output) or requested_output.is_symlink() or is_junction:
        raise FileExistsError(f"Training output must be a new directory: {requested_output}")
    output_root = requested_output.resolve()
    if output_root.parent != training_root or os.path.lexists(output_root):
        raise FileExistsError(f"Training output must be a new direct child of training/: {output_root}")

    with NOTEBOOK_PATH.open("r", encoding="utf-8") as notebook_file:
        cells = json.load(notebook_file).get("cells", [])
    if len(cells) < 10 or any(cells[i].get("cell_type") != "code" for i in range(10)):
        raise RuntimeError("Unexpected structure in notebooks/bert_original.ipynb cells 0-9.")

    cell1_source = source_of(cells[1])
    match = re.search(r'query\s*=\s*"""(.*?)"""', cell1_source, re.DOTALL)
    if not match or not match.group(1).lstrip().lower().startswith("select"):
        raise RuntimeError("Safety stop: notebook cell 1 does not contain a SELECT query.")
    # Record a digest of the exact notebook query without exposing credentials or query results.
    query_hash = hashlib.sha256(match.group(1).encode("utf-8")).hexdigest()
    if "SET SESSION TRANSACTION READ ONLY" not in cell1_source:
        raise RuntimeError("Safety stop: cell 1 has no read-only session guard.")

    username = os.environ.get("DIPLOMARBEIT_DB_USER") or input("MySQL username: ")
    password = os.environ.get("DIPLOMARBEIT_DB_PASSWORD") or getpass.getpass("MySQL password (hidden): ")
    from sqlalchemy.engine import URL

    db_url = URL.create(
        "mysql+mysqlconnector",
        username=username,
        password=password,
        host=os.environ.get("DIPLOMARBEIT_DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("DIPLOMARBEIT_DB_PORT", "3306")),
        database=DATABASE_NAME,
    )

    # Create the fresh run directory only after path and notebook safety checks pass.
    output_root.mkdir(parents=True)
    namespace: dict[str, object] = {
        "db_url": db_url,
        "PROJECT_ROOT": ROOT,
        "MODEL_SOURCE": BASE_MODEL_SOURCE,
        "TRAINING_OUTPUT_DIR": output_root,
        "TRAINING_LOG_DIR": output_root / "logs",
    }
    engine = None
    try:
        # Execute notebook cells in order, supplying project-local paths and database settings.
        exec(compile(source_of(cells[0]), f"{NOTEBOOK_PATH}:cell0", "exec"), namespace)
        captured_join_rows: list[int] = []
        original_read_sql = namespace["pd"].read_sql

        def capture_join_count(*args: object, **kwargs: object):
            """Record the original joined-row count without changing query output."""
            result = original_read_sql(*args, **kwargs)
            if args and isinstance(args[0], str) and args[0].lstrip().lower().startswith("select a.id"):
                captured_join_rows.append(len(result))
            return result

        # Capture the original joined-row count while letting the notebook run its own query.
        namespace["pd"].read_sql = capture_join_count
        exec(compile(cell1_source, f"{NOTEBOOK_PATH}:cell1", "exec"), namespace)
        namespace["pd"].read_sql = original_read_sql
        engine = namespace["engine"]
        selected_database = namespace["pd"].read_sql(
            "SELECT DATABASE() AS selected_database", engine
        ).iloc[0, 0]
        if selected_database != DATABASE_NAME:
            raise RuntimeError("Connected database does not match the configured reconstruction schema.")

        for index in (2, 3):
            exec(compile(source_of(cells[index]), f"{NOTEBOOK_PATH}:cell{index}", "exec"), namespace)
        train_df = namespace["train_df"]
        test_df = namespace["test_df"]
        filtered_df = namespace["df_filtered"]
        encoder = namespace["label_encoder"]
        train_ids = set(train_df["article_id"].unique())
        test_ids = set(test_df["article_id"].unique())
        mapping = {
            str(author): int(encoder.transform([author])[0])
            for author in encoder.classes_
        }
        # Persist the actual row-level split and encoder mapping alongside the checkpoints.
        train_manifest = train_df[["article_id", "author", "author_id"]].copy()
        train_manifest["split"] = "train"
        test_manifest = test_df[["article_id", "author", "author_id"]].copy()
        test_manifest["split"] = "test"
        import pandas as pd

        pd.concat([train_manifest, test_manifest], ignore_index=True).to_csv(
            output_root / "split_manifest.csv", index=False, encoding="utf-8"
        )
        (output_root / "label_mapping.json").write_text(
            json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        manifest = {
            "database": DATABASE_NAME,
            "source_notebook": str(NOTEBOOK_PATH.relative_to(ROOT)),
            "executed_cells": list(range(10)),
            "sql_sha256": query_hash,
            "rows_after_join": captured_join_rows[0] if captured_join_rows else None,
            "rows_after_original_filters": int(len(filtered_df)),
            "train_rows": int(len(train_df)),
            "test_rows": int(len(test_df)),
            "classes": int(len(encoder.classes_)),
            "train_test_article_id_overlap": len(train_ids & test_ids),
            "model_id": MODEL_ID,
            "model_revision": MODEL_REVISION,
            "model_source": str(BASE_MODEL_SOURCE.relative_to(ROOT)),
            "label_mapping_file": "label_mapping.json",
            "split_manifest_file": "split_manifest.csv",
            "training_parameters": {
                "epochs": 3,
                "learning_rate": 5e-5,
                "train_batch_size": 8,
                "eval_batch_size": 8,
                "max_length": 512,
                "warmup_steps": 500,
                "lr_scheduler_type": "linear",
                "fp16": True,
                "evaluation_strategy": "epoch",
                "save_strategy": "epoch",
                "metric_for_best_model": "eval_loss",
                "early_stopping_patience": 2,
            },
            "note": "The original split uses list(set(article_ids)); set iteration may vary between Python processes.",
        }
        (output_root / "run_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # Cells 4–8 configure datasets, model, metrics and Trainer before cell 9 trains.
        for index in range(4, 9):
            exec(compile(source_of(cells[index]), f"{NOTEBOOK_PATH}:cell{index}", "exec"), namespace)
        print(
            f"Prepared original workflow: {len(train_df)} train rows, {len(test_df)} test rows, "
            f"{len(encoder.classes_)} classes. Executing original training cell 9.",
            flush=True,
        )
        exec(compile(source_of(cells[9]), f"{NOTEBOOK_PATH}:cell9", "exec"), namespace)
        log_history = namespace["trainer"].state.log_history
        (output_root / "training_log_history.json").write_text(
            json.dumps(log_history, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        print(f"Training finished; outputs: {output_root}", flush=True)
        return 0
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
