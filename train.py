"""Fine-tune a BERT author classifier from prepared train/test CSV files.

The runner consumes the exact split and label mapping emitted by
``prepare_data.py``. It writes checkpoints, the final model, tokenizer and run
metadata beneath a new training directory and refuses to reuse an existing path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path

import numpy as np
import pandas as pd
import torch
from sklearn.metrics import accuracy_score
from torch.utils.data import Dataset
from transformers import (
    BertForSequenceClassification,
    BertTokenizer,
    DataCollatorWithPadding,
    EarlyStoppingCallback,
    Trainer,
    TrainingArguments,
)


PROJECT_ROOT = Path(__file__).resolve().parent
TRAINING_ROOT = PROJECT_ROOT / "training"
HISTORICAL_MODEL_ID = "bert-base-german-cased"
HISTORICAL_MODEL_REVISION = "0b061b4f7ce140d10dce6b6bd8a77da8ec80931b"
DATA_COLUMNS = ["article_id", "author", "author_id", "content"]


class TextDataset(Dataset):
    """Tokenize one article per item and return tensors expected by Trainer."""

    def __init__(self, frame: pd.DataFrame, tokenizer: BertTokenizer, max_length: int = 512):
        self.texts = frame["content"].astype(str).reset_index(drop=True)
        self.labels = frame["author_id"].astype(int).reset_index(drop=True)
        self.tokenizer = tokenizer
        self.max_length = max_length

    def __len__(self) -> int:
        return len(self.texts)

    def __getitem__(self, index: int) -> dict[str, torch.Tensor]:
        """Tokenize a text with the historical truncation and fixed-length padding."""
        encoding = self.tokenizer(
            self.texts.iloc[index],
            truncation=True,
            padding="max_length",
            max_length=self.max_length,
            return_tensors="pt",
        )
        return {
            "input_ids": encoding["input_ids"].squeeze(0),
            "attention_mask": encoding["attention_mask"].squeeze(0),
            "labels": torch.tensor(self.labels.iloc[index], dtype=torch.long),
        }


def parse_args() -> argparse.Namespace:
    """Parse model, prepared-data and fresh output-directory settings."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prepared-dir",
        required=True,
        help="directory created by prepare_data.py containing train/test CSVs and manifests",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="new direct child of training/; an existing path is never overwritten",
    )
    parser.add_argument(
        "--base-model",
        default=HISTORICAL_MODEL_ID,
        help="local BERT snapshot or Hugging Face model identifier",
    )
    parser.add_argument(
        "--revision",
        help="optional model revision; the historical revision is used for the default model ID",
    )
    parser.add_argument(
        "--local-files-only",
        action="store_true",
        help="refuse model downloads and load only files already available locally",
    )
    return parser.parse_args()


def _resolve_project_path(value: str, *, default_parent: Path) -> Path:
    """Resolve a CLI path relative to the project unless it is already absolute."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else default_parent / path


def _require_fresh_training_dir(path: Path) -> Path:
    """Validate that the requested output is a new direct child of training/."""
    training_root = TRAINING_ROOT.resolve()
    project_root = PROJECT_ROOT.resolve()
    try:
        training_root.relative_to(project_root)
    except ValueError as exc:
        raise ValueError("Training root must remain inside the project.") from exc
    if TRAINING_ROOT.exists() and (
        not TRAINING_ROOT.is_dir()
        or TRAINING_ROOT.is_symlink()
        or getattr(TRAINING_ROOT, "is_junction", lambda: False)()
    ):
        raise ValueError("Training root must be a real project directory, not a link.")

    path = path.absolute()
    if path.parent.resolve() != training_root:
        raise ValueError("Training output must be a direct child of training/.")
    if (
        os.path.lexists(path)
        or path.is_symlink()
        or getattr(path, "is_junction", lambda: False)()
    ):
        raise FileExistsError("Training output must be a new directory.")
    resolved = path.resolve()
    if resolved.parent != training_root or os.path.lexists(resolved):
        raise FileExistsError("Training output must resolve to a new direct child of training/.")
    return resolved


def load_prepared_data(prepared_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, int]]:
    """Load and validate the prepared splits and their author-to-label mapping."""
    train_path = prepared_dir / "train.csv"
    test_path = prepared_dir / "test.csv"
    mapping_path = prepared_dir / "label_mapping.json"
    manifest_path = prepared_dir / "split_manifest.csv"
    for required in (train_path, test_path, mapping_path, manifest_path):
        if not required.is_file():
            raise FileNotFoundError(f"Required preparation artifact is missing: {required.name}")

    train_df = pd.read_csv(train_path)
    test_df = pd.read_csv(test_path)
    if list(train_df.columns) != DATA_COLUMNS or list(test_df.columns) != DATA_COLUMNS:
        raise ValueError("Prepared CSV files do not have the expected columns.")
    if train_df.empty or test_df.empty:
        raise ValueError("Both prepared train and test splits must contain rows.")
    if train_df[DATA_COLUMNS].isna().any().any() or test_df[DATA_COLUMNS].isna().any().any():
        raise ValueError("Prepared train/test files contain missing required values.")

    raw_mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    if not isinstance(raw_mapping, dict) or not raw_mapping:
        raise ValueError("Label mapping must be a non-empty JSON object.")
    mapping = {str(author): int(label) for author, label in raw_mapping.items()}
    if sorted(mapping.values()) != list(range(len(mapping))):
        raise ValueError("Label IDs must be unique and contiguous from zero.")

    for split_name, frame in (("train", train_df), ("test", test_df)):
        if not set(frame["author"].astype(str)).issubset(mapping):
            raise ValueError(f"The {split_name} split contains authors absent from label_mapping.json.")
        expected_labels = frame["author"].astype(str).map(mapping).astype(int)
        if not expected_labels.equals(frame["author_id"].astype(int).reset_index(drop=True)):
            raise ValueError(f"The {split_name} split labels disagree with label_mapping.json.")

    manifest = pd.read_csv(manifest_path)
    manifest_columns = ["article_id", "author", "author_id", "split"]
    if list(manifest.columns) != manifest_columns:
        raise ValueError("split_manifest.csv does not have the expected columns.")
    expected_manifest = pd.concat(
        [
            train_df[manifest_columns[:3]].assign(split="train"),
            test_df[manifest_columns[:3]].assign(split="test"),
        ],
        ignore_index=True,
    )
    for frame in (manifest, expected_manifest):
        frame["article_id"] = frame["article_id"].astype(int)
        frame["author"] = frame["author"].astype(str)
        frame["author_id"] = frame["author_id"].astype(int)
        frame["split"] = frame["split"].astype(str)
    sort_columns = ["article_id", "author", "author_id", "split"]
    if not manifest["split"].isin({"train", "test"}).all():
        raise ValueError("split_manifest.csv contains an unknown split name.")
    if not manifest[sort_columns].sort_values(sort_columns).reset_index(drop=True).equals(
        expected_manifest[sort_columns].sort_values(sort_columns).reset_index(drop=True)
    ):
        raise ValueError("split_manifest.csv does not match the prepared train/test rows.")

    train_ids = set(train_df["article_id"].astype(int))
    test_ids = set(test_df["article_id"].astype(int))
    if train_ids & test_ids:
        raise ValueError("An article ID appears in both train and test splits.")
    return train_df, test_df, mapping


def compute_metrics(evaluation) -> dict[str, float]:
    """Return classification accuracy for the Trainer evaluation predictions."""
    logits = evaluation.predictions
    if isinstance(logits, tuple):
        logits = logits[0]
    predictions = np.asarray(logits).argmax(axis=1)
    return {"accuracy": float(accuracy_score(evaluation.label_ids, predictions))}


def _sha256(path: Path) -> str:
    """Hash a file incrementally so large prepared splits need not be loaded whole."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_training(
    train_df: pd.DataFrame,
    test_df: pd.DataFrame,
    label_mapping: dict[str, int],
    *,
    base_model: str,
    revision: str | None,
    local_files_only: bool,
    output_dir: Path,
) -> Trainer:
    """Fine-tune the sequence classifier and save model/tokenizer artifacts.

    Historical optimizer, batch, epoch, evaluation, checkpoint and early-stopping
    settings are retained. Mixed precision is enabled only when CUDA is available,
    allowing the same workflow to run on CPU-only systems.
    """
    tokenizer = BertTokenizer.from_pretrained(
        base_model, revision=revision, local_files_only=local_files_only
    )
    id_to_label = {label: author for author, label in label_mapping.items()}
    model = BertForSequenceClassification.from_pretrained(
        base_model,
        revision=revision,
        local_files_only=local_files_only,
        num_labels=len(label_mapping),
        id2label=id_to_label,
        label2id=label_mapping,
    )
    train_dataset = TextDataset(train_df, tokenizer)
    test_dataset = TextDataset(test_df, tokenizer)
    checkpoint_dir = output_dir / "checkpoints"
    final_model_dir = output_dir / "final_model"
    checkpoint_dir.mkdir()

    training_args = TrainingArguments(
        output_dir=str(checkpoint_dir),
        num_train_epochs=3,
        learning_rate=5e-5,
        per_device_train_batch_size=8,
        per_device_eval_batch_size=8,
        evaluation_strategy="epoch",
        save_strategy="epoch",
        load_best_model_at_end=True,
        metric_for_best_model="eval_loss",
        warmup_steps=500,
        lr_scheduler_type="linear",
        logging_dir=str(output_dir / "logs"),
        report_to="none",
        fp16=torch.cuda.is_available(),
    )
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=test_dataset,
        tokenizer=tokenizer,
        data_collator=DataCollatorWithPadding(tokenizer=tokenizer),
        compute_metrics=compute_metrics,
        callbacks=[EarlyStoppingCallback(early_stopping_patience=2)],
    )
    trainer.train()
    trainer.save_model(str(final_model_dir))
    tokenizer.save_pretrained(final_model_dir)
    (final_model_dir / "label_mapping.json").write_text(
        json.dumps(label_mapping, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "training_log_history.json").write_text(
        json.dumps(trainer.state.log_history, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return trainer


def main() -> int:
    """Validate inputs, create a fresh run directory, and start model training."""
    args = parse_args()
    prepared_dir = _resolve_project_path(args.prepared_dir, default_parent=PROJECT_ROOT).resolve()
    if not prepared_dir.is_dir():
        raise NotADirectoryError("Prepared-data directory does not exist.")
    output_dir = _require_fresh_training_dir(
        _resolve_project_path(args.output_dir, default_parent=PROJECT_ROOT)
    )

    train_df, test_df, label_mapping = load_prepared_data(prepared_dir)
    revision = args.revision
    if revision is None and args.base_model == HISTORICAL_MODEL_ID:
        revision = HISTORICAL_MODEL_REVISION

    # Validate local model paths before creating output. Hub identifiers are
    # resolved by Transformers when the developer explicitly allows downloads.
    model_path = Path(args.base_model).expanduser()
    if model_path.exists() and not model_path.is_dir():
        raise NotADirectoryError("A local base-model path must be a directory.")
    if args.local_files_only and not model_path.is_dir():
        raise FileNotFoundError("--local-files-only requires a local base-model directory.")

    TRAINING_ROOT.mkdir(exist_ok=True)
    output_dir.mkdir()
    shutil.copy2(prepared_dir / "split_manifest.csv", output_dir / "split_manifest.csv")
    shutil.copy2(prepared_dir / "label_mapping.json", output_dir / "label_mapping.json")
    mapping_for_model = output_dir / "final_model" / "label_mapping.json"
    run_manifest = {
        "prepared_data_directory": str(prepared_dir.relative_to(PROJECT_ROOT))
        if prepared_dir.is_relative_to(PROJECT_ROOT)
        else "external",
        "train_rows": len(train_df),
        "test_rows": len(test_df),
        "class_count": len(label_mapping),
        "train_csv_sha256": _sha256(prepared_dir / "train.csv"),
        "test_csv_sha256": _sha256(prepared_dir / "test.csv"),
        "label_mapping_file": "label_mapping.json",
        "split_manifest_file": "split_manifest.csv",
        "base_model": args.base_model,
        "revision": revision,
        "device": "cuda" if torch.cuda.is_available() else "cpu",
        "training_parameters": {
            "epochs": 3,
            "learning_rate": 5e-5,
            "train_batch_size": 8,
            "eval_batch_size": 8,
            "max_length": 512,
            "warmup_steps": 500,
            "lr_scheduler_type": "linear",
            "fp16": torch.cuda.is_available(),
            "evaluation_strategy": "epoch",
            "save_strategy": "epoch",
            "metric_for_best_model": "eval_loss",
            "early_stopping_patience": 2,
        },
        "final_model_mapping_path": str(mapping_for_model.relative_to(output_dir)),
    }
    (output_dir / "run_manifest.json").write_text(
        json.dumps(run_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    run_training(
        train_df,
        test_df,
        label_mapping,
        base_model=args.base_model,
        revision=revision,
        local_files_only=args.local_files_only,
        output_dir=output_dir,
    )
    print(f"Training finished; model and run metadata saved under {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
