# MySQL Database

This directory provides an example MySQL environment for the **AI-Based Authorship Attribution** project.

MySQL is used to store German-language news articles, author information, and the relationships between articles and their authors.

The database supports article ingestion and data management for the project's authorship attribution workflow.

## Database Architecture

The relational schema is defined through SQLAlchemy models in [`models.py`](../models.py).

It includes:

- **Articles:** Article content, source URLs, and associated metadata.
- **Authors:** Information identifying article authors.
- **Article–author relationships:** A many-to-many association that allows articles to be linked to multiple authors.

The association table is named `placeholder` in the existing ORM schema.

```text
       Authors
          |
          | Many-to-Many
          |
       Articles
```

The schema is not automatically created by the article importer.

## Technology

| Component | Description |
|---|---|
| Database | MySQL 8.0.36 |
| ORM | SQLAlchemy |
| Containerization | Docker Compose |
| Database name | `diplomarbeit` |
| Internal hostname | `mysql` |
| Internal port | `3306` |

## Docker Configuration

The repository provides:

`compose.example.yaml`

This file defines an isolated MySQL example service with its own Docker volume and network.

The example:

- Uses MySQL 8.0.36.
- Creates a separate persistent data volume.
- Connects to the Docker network `ai-authorship-attribution-daily`.
- Makes the database available to Airflow under the hostname `mysql`.
- Does not publish a database port to the host.
- Requires a locally configured root password.

It does not connect to the historical or reconstructed project database.

## Getting Started

### Prerequisites

- Docker and Docker Compose
- A locally generated MySQL root password
- The SQLAlchemy schema defined in `models.py`

### 1. Configure Environment Variables

Create a `.env` file inside the `mysql/` directory:

```dotenv
MYSQL_ROOT_PASSWORD=<your-strong-local-password>
```

Replace the placeholder with a strong, unique password.

The `.env` file is excluded from the public repository and must remain private.

### 2. Start MySQL

Open a terminal in the `mysql/` directory and run:

```powershell
docker compose --env-file .env -f compose.example.yaml up -d
```

On the first startup with an empty data volume, the MySQL image initializes the database:

`diplomarbeit`

The container maintains its database files in a dedicated named volume.

### 3. Initialize the Database Schema

The example creates the database itself, but it does **not** automatically create the required application tables.

Before importing articles, initialize the tables according to the SQLAlchemy models in [`models.py`](../models.py), including their foreign keys and the `placeholder` association table.

This repository does not currently provide an automated database migration or initialization command.

### 4. Create a Database User

Create a dedicated application account rather than using the MySQL root account for article ingestion.

The importer requires the following permissions on `diplomarbeit.*`:

- `SELECT`
- `INSERT`

Store the application account's credentials securely.

The Airflow importer should not use the MySQL root account.

### 5. Connect Apache Airflow

Configure an Airflow connection using these settings:

| Field | Value |
|---|---|
| Connection ID | `diplomarbeit_daily_mysql` |
| Connection type | `mysql` |
| Host | `mysql` |
| Port | `3306` |
| Schema | `diplomarbeit` |
| Username | Dedicated application user |
| Password | Application user's password |

The Airflow container must be attached to the Docker network `ai-authorship-attribution-daily`.

See [Airflow Setup](../airflow/README.md) for the remaining configuration.

## Data Persistence and Security

### Persistent Storage

The example uses a dedicated Docker volume named:

`ai-authorship-attribution-mysql-example-data`

Database records remain in this volume when the container is recreated, provided the volume is preserved.

Deleting the volume permanently removes the data stored in it unless a backup exists.

### Network Access

MySQL does not expose a host port in the provided example.

Airflow communicates with the database through the shared Docker network using the hostname `mysql`.

This limits direct host-network exposure but does not replace database authentication or proper network access controls.

### Credentials

- Keep passwords in local environment files or suitable secret-management systems.
- Do not commit `.env` files.
- Use separate root and application credentials.
- Do not reuse passwords from existing database installations.

## Limitations

This example is intended for local development and technical evaluation.

It does not provide:

- Preloaded article or author records
- Automated SQLAlchemy table initialization
- Database migrations
- Automatic creation of the restricted application account
- Production database backup or monitoring infrastructure

These components must be configured separately if required.

## Related Documentation

- [Project Overview](../README.md)
- [Airflow Pipeline](../airflow/README.md)
- [Training Documentation](../docs/TRAINING.md)