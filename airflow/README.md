# Automated Article Pipeline with Apache Airflow

This directory contains an automated data pipeline for collecting and importing German-language news articles using **Apache Airflow**, **Python**, and **MySQL**.

The pipeline was developed in **2026 as an extension** to the original 2025 diploma thesis project on BERT-based authorship attribution. It is not part of the original thesis implementation.

## Overview

The pipeline automates two main steps:

1. **Article collection:** Discover and retrieve articles from DerStandard for a specific calendar day.
2. **Database ingestion:** Import the collected articles and their author relationships into a MySQL database.

Apache Airflow coordinates both steps through the `daily_article_pipeline` DAG.

### Pipeline Architecture

```text
Apache Airflow
      |
      v
daily_article_pipeline
      |
      +--> crawl_daily_articles
      |          |
      |          v
      |     DerStandard Sitemap
      |          |
      |          v
      |     Article Extraction
      |          |
      |          v
      |     Run-Specific CSV
      |          |
      +--> import_daily_articles
                 |
                 v
           SQLAlchemy ORM
                 |
                 v
            MySQL Database
```

## Workflow

### Article Collection

The `crawl_daily_articles` task executes `daily_pipeline/crawler.py`.

The crawler:

- Discovers articles through the DerStandard monthly sitemap.
- Selects articles associated with the requested Austrian calendar day.
- Uses HTTPS and validates the target domain, including redirects.
- Applies HTTP request timeouts and waits at least one second between article requests.
- Avoids previously processed URLs using locally available CSV records.
- Writes the collected data to a UTF-8 CSV file.

The CSV contains the following columns:

| Column | Description |
|---|---|
| `url` | Article URL |
| `lastmod` | Last-modified timestamp from the source |
| `author` | Extracted author information |
| `content` | Article text |

Output files are stored in run-specific directories:

```text
/opt/airflow/daily_data/
└── YYYY-MM-DD/
    └── <run-token>/
        └── articles.csv
```

There is no configured maximum number of articles per run.

### Database Import

The `import_daily_articles` task executes `daily_pipeline/import_articles.py`.

It reads the CSV created by the crawler and uses the SQLAlchemy models defined in the root-level `models.py` to insert articles and their author relationships.

The importer:

- Validates the selected date and the run-specific CSV path.
- Checks for existing and duplicate article URLs.
- Inserts new records through a database transaction.
- Uses the Airflow connection `diplomarbeit_daily_mysql`.

The importer expects the required database schema and tables to exist beforehand. It does not initialize them automatically.

## Scheduling

The DAG uses the following settings:

| Setting | Value |
|---|---|
| DAG ID | `daily_article_pipeline` |
| Schedule | Daily at 01:00 |
| Timezone | `Europe/Vienna` |
| Catchup | Disabled |
| Maximum active runs | 1 |
| Automatic task retries | Disabled |

Scheduled runs process the Vienna calendar day preceding the end of the data interval.

Manually triggered runs select the calendar day preceding the run's logical date.

This distinction is important when triggering historical or manual runs.

## Project Structure

```text
airflow/
├── README.md
├── Dockerfile
├── compose.yaml
├── requirements.txt
├── .env.example
├── dags/
│   └── daily_pipeline_dag.py
└── daily_pipeline/
    ├── crawler.py
    └── import_articles.py
```

## Local Setup

The included Docker configuration runs **Apache Airflow 2.10.5** in standalone mode for local development and evaluation.

It is not intended as a production deployment.

### Prerequisites

- Docker and Docker Compose
- A configured MySQL database
- The required database tables from `models.py`
- A dedicated MySQL application user
- A valid Airflow Fernet key

### 1. Prepare MySQL

Follow the instructions in [MySQL Setup](../mysql/README.md).

The public MySQL example provides an isolated Docker environment and uses the example network:

`ai-authorship-attribution-daily`

The MySQL schema, required tables, and dedicated application user must be configured before running the article importer.

### 2. Configure Airflow

Create a local `.env` file based on `.env.example`.

Configure:

| Variable | Purpose |
|---|---|
| `AIRFLOW_FERNET_KEY` | Persistent Fernet key used by Airflow |
| `MYSQL_DOCKER_NETWORK` | External Docker network used to reach MySQL |
| `AIRFLOW_WEB_PORT` | Local Airflow web interface port; defaults to 8083 |

Replace placeholder values with your own local configuration. Keep the `.env` file private.

### 3. Start Airflow

Start the MySQL environment first so the external Docker network exists.

From the `airflow/` directory, run:

```bash
docker compose up -d --build
```

The Airflow web interface is available at:

`http://localhost:8083`

The Compose configuration binds the web interface to the local loopback interface by default.

**Important:** The supplied Compose file uses fixed project and volume names. Before running multiple installations on the same Docker host, check for naming conflicts. Do not change or remove existing volumes without verifying which data they contain.

### 4. Configure the Database Connection

Create an Airflow connection with the following settings:

| Field | Value |
|---|---|
| Connection ID | `diplomarbeit_daily_mysql` |
| Connection type | `mysql` |
| Host | `mysql` |
| Port | `3306` |
| Schema | `diplomarbeit` |
| Username / Password | Credentials of the dedicated MySQL application user |

Store the credentials in the Airflow connection store or an appropriate secret backend.

The application user requires `SELECT` and `INSERT` permissions on `diplomarbeit.*`.

The database must contain the tables mapped by `models.py`, including the `placeholder` association table.

### 5. Enable the DAG

On a fresh Airflow metadata database, the DAG is configured to start in a paused state.

Verify its state in the Airflow interface before enabling scheduled execution.

Once enabled, Airflow schedules the pipeline daily according to the configured timezone.

## Security and Operational Considerations

### Network Isolation

The Airflow container joins the Docker network specified by `MYSQL_DOCKER_NETWORK`.

The public MySQL example does not publish a database port to the host. Database access is intended to occur through the shared Docker network.

### Duplicate Handling

Duplicate article URLs are checked at the application level.

The database does not currently enforce a unique constraint on the article URL column. Concurrent or independently executed imports therefore require additional care.

### Memory Usage

The crawler temporarily stores fetched article records in memory before writing the CSV.

The importer also loads the CSV before processing database inserts.

Memory usage can therefore increase with the number of articles processed.

### Failure Recovery

Automatic task retries are not configured.

If an import fails, inspect the run state and retry the import using the CSV generated by that same run where appropriate.

Starting a new crawl is not equivalent to retrying the original import, because previously written CSV files influence URL deduplication.

### Logging

Some error messages intentionally report exception classes rather than full database driver messages to reduce accidental disclosure of connection details.

This can limit diagnostic detail during troubleshooting.

## Limitations

The public repository does not include:

- A preconfigured Airflow metadata database
- MySQL database contents
- Article datasets or previously collected CSV files
- Airflow connection credentials
- Local Fernet keys or other secrets
- Existing Docker volumes or runtime state

A fresh checkout therefore requires separate database initialization and configuration.

The provided infrastructure is intended to demonstrate and support the article collection architecture, not to serve as a production-ready deployment.

## Related Documentation

- [Project Overview](../README.md)
- [MySQL Setup](../mysql/README.md)
- [Training Documentation](../docs/TRAINING.md)