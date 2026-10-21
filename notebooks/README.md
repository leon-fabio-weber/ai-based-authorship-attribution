# BERT Evaluation Notebook

[`evaluate_bert.ipynb`](evaluate_bert.ipynb) contains the reconstructed post-training evaluation workflow. It loads a local fine-tuned checkpoint and tokenizer, reads article and author records from a configured MySQL database, runs inference over the loaded records, and computes per-author accuracy and a normalized confusion matrix.

## What the notebook evaluates

The notebook removes records whose author is `kein Autor`, retains authors with at least 10 records, encodes author labels, and runs the checkpoint over the resulting dataset. The per-author bar chart and confusion matrix then restrict their classes to authors with at least 700 loaded records. The accuracy-versus-count plot includes all authors remaining after the initial 10-record filter.

This notebook does not create a held-out split before its inference pass. It evaluates all filtered rows returned by its database query. The resulting figures are descriptive summaries of that loaded dataset and are not independent estimates of performance on unseen articles. This is distinct from the epoch-level evaluation values recorded during model training, described in [Training and Evaluation](../docs/TRAINING.md).

## Figures

### Accuracy per author

![Accuracy per author](../docs/figures/accuracy_per_author.png)

The horizontal axis lists author names; the vertical axis gives the fraction of loaded records for each author that the model classified correctly. Only authors with at least 700 records are included. The plot shows differences among these author classes, but does not establish performance on unseen data. Author names are visible in this figure.

### Accuracy vs. article count

![Accuracy vs. article count](../docs/figures/accuracy_vs_article_count.png)

Each blue point represents an author class. The horizontal axis shows its number of records in the loaded dataset, and the vertical axis shows its accuracy. The red curve is a 15-point moving average after sorting by article count. The saved plot title says “Testdaten”, but the notebook evaluates all rows returned by its database query; the plot must not be read as a held-out test result.

### Normalized confusion matrix

![Normalized confusion matrix](../docs/figures/confusion_matrix_normalized.png)

Rows are actual author classes and columns are predicted classes. The color scale shows the row-normalized fraction, from 0 to 1. Diagonal cells are correct classifications; off-diagonal cells show where examples from one actual class were assigned to another. The displayed classes are restricted to authors with at least 700 loaded records and use local labels `A1`–`A89`.

## Requirements and data

Running the notebook requires:

- A compatible Python environment with the project dependencies, including PyTorch, Transformers, SQLAlchemy, and the MySQL driver.
- Access to the expected MySQL schema and its `articles`, `authors`, and `placeholder` tables. The notebook prompts for a database username and password and opens a read-only database session.
- The local fine-tuned checkpoint and matching tokenizer, by default at `training/author_classification2/checkpoint-61590`. `BERT_CHECKPOINT_PATH` can override this location.

The database contents and model artifacts are not included in the public repository. A fresh clone therefore cannot run this notebook without separately provisioned, authorized data and model files. The notebook writes figures to `docs/figures/` by default; `BERT_FIGURE_DIR` can override the output directory.

The repository's [training documentation](../docs/TRAINING.md) describes the training workflow, recorded epoch-level metrics, and reproducibility limitations.
