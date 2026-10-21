# BERT Model Training

This document describes the data preparation and model training components of the **AI-Based Authorship Attribution** project.

The project explores the use of **BERT (Bidirectional Encoder Representations from Transformers)** for identifying the likely author of a German-language news article.

The training workflow is based on the original 2025 diploma thesis implementation and selected components reconstructed for technical evaluation.

## Overview

Authorship attribution is a text classification task in which a model predicts the author of a document based on linguistic and contextual patterns.

In this project, BERT is adapted for multi-class classification. Each author corresponds to a class label, and the model learns to associate article texts with their respective authors.

The workflow consists of:

1. Loading article and author information.
2. Filtering and preparing the dataset.
3. Encoding author labels.
4. Preparing training and evaluation splits.
5. Tokenizing article texts.
6. Fine-tuning a BERT-based classification model.
7. Evaluating the resulting model.

## Training Workflow

```text
Article Dataset
      |
      v
Author Filtering
      |
      v
Data Preparation
      |
      v
Label Encoding
      |
      v
Train / Evaluation Split
      |
      v
BERT Tokenization
      |
      v
Model Fine-Tuning
      |
      v
Evaluation
```

## Data Preparation

The file [`prepare_data.py`](../prepare_data.py) provides the reconstructed data preparation workflow.

Its operations include:

- Loading article and author information from a separately provisioned database.
- Filtering authors and article records.
- Preparing training and evaluation subsets.
- Encoding author identities as classification labels.

The implementation extracts SQL and preparation logic from the historical notebook `notebooks/bert_original.ipynb`.

**Important:** The notebook is not distributed in the public repository. Therefore, `prepare_data.py` cannot independently reproduce the full preparation workflow from a fresh clone.

The preparation stage also requires an authorized, read-only database connection and the expected database schema.

## Model Training

The file [`train.py`](../train.py) preserves the reconstructed BERT training workflow.

The training implementation is derived from the historical notebook and uses the Hugging Face Transformers and PyTorch ecosystems.

A complete training run requires:

- Prepared article data and author labels.
- Matching training and evaluation split files.
- A consistent author-to-class mapping.
- The historical notebook containing the required training cells.
- A compatible local pretrained model and tokenizer.
- A suitable CPU or GPU environment.

Training generates model artifacts and checkpoints in a designated output directory.

The script includes explicit execution safeguards and output-path checks to reduce the risk of accidentally overwriting existing training artifacts.

These safeguards do not replace the missing data, notebook, or model resources.

## Dependencies

The root-level [`requirements.txt`](../requirements.txt) lists the general Python dependencies used by the reconstructed workflow.

Key technologies include:

| Technology | Purpose |
|---|---|
| Python | Data preparation and training orchestration |
| PyTorch | Model training and tensor operations |
| Hugging Face Transformers | BERT model architecture, tokenization, and training utilities |
| Pandas | Dataset preparation |
| scikit-learn | Dataset splitting and evaluation utilities |
| SQLAlchemy | Database access |

Install the general dependencies using:

```bash
pip install -r requirements.txt
```

Install PyTorch separately using a build compatible with the target CPU or CUDA platform.

This command alone does not provide the historical data, notebook implementation, or model artifacts needed for a complete training run.

## Evaluation and Interpretation

A reconstructed local training run recorded the following evaluation accuracies:

| Epoch | Recorded Evaluation Accuracy |
|---|---:|
| 1 | 52.33% |
| 2 | 61.21% |
| 3 | 65.13% |

These results reflect the recorded evaluation of the reconstructed workflow.

The exact identity of the historical training and evaluation split could not be independently established. Therefore, the figures should not be treated as independently verified generalization performance.

A separate evaluation over the complete filtered dataset included training examples and is **not an independent held-out test**.

Likewise, successfully loading a trained checkpoint and generating a prediction confirms that the inference path works, but does not independently establish predictive accuracy.

## Reproducibility

This repository provides the public source files for understanding the reconstructed training workflow. It does not contain everything required to execute it.

The following resources are intentionally excluded:

- Historical Jupyter notebooks
- Original article datasets and article texts
- Author-label mappings
- Training and evaluation split artifacts
- Pretrained model snapshots and tokenizers
- Fine-tuned model checkpoints
- Database credentials and local environment files

Consequently, the published `prepare_data.py` and `train.py` scripts are **not standalone training tools**.

A complete reproduction requires separately authorized access to the relevant data and artifacts, together with the expected software environment.

## Related Documentation

- [Project Overview](../README.md)
- [Airflow Pipeline](../airflow/README.md)
- [MySQL Database](../mysql/README.md)