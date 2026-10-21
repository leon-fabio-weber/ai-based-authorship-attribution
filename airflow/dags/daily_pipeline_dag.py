"""Schedule the daily DerStandard crawl and MySQL import workflow.

The DAG launches the project crawler and importer as separate subprocesses. Each
run gets a deterministic, run-scoped CSV path; the importer consumes that exact
path through XCom and uses the configured Airflow connection.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pendulum
from airflow import DAG
from airflow.operators.python import PythonOperator


PROJECT_DIR = Path("/opt/airflow/project")
SCRIPT_DIR = PROJECT_DIR / "airflow" / "daily_pipeline"
DAILY_DATA_DIR = Path("/opt/airflow/daily_data")
CRAWLER_SCRIPT = SCRIPT_DIR / "crawler.py"
IMPORT_SCRIPT = SCRIPT_DIR / "import_articles.py"
AIRFLOW_DB_CONNECTION_ID = "diplomarbeit_daily_mysql"
VIENNA = ZoneInfo("Europe/Vienna")


def _target_date(context) -> date:
    """Resolve the Austrian calendar day whose articles this run should process.

    Args:
        context: Airflow task context containing the DAG run and its logical or
            data-interval timestamp.

    Returns:
        The calendar date one day before the selected timestamp after conversion
        to ``Europe/Vienna``. Scheduled runs use ``data_interval_end``; externally
        triggered runs use their logical date.

    Raises:
        ValueError: If neither timestamp is present.

    Converting the instant before subtracting a calendar day preserves the local
    date around daylight-saving transitions.
    """
    dag_run = context.get("dag_run")
    if getattr(dag_run, "external_trigger", False):
        scheduled_at = context.get("logical_date") or getattr(dag_run, "logical_date", None)
    else:
        scheduled_at = context.get("data_interval_end")
    if scheduled_at is None:
        raise ValueError("Airflow logical_date or data_interval_end is required")
    # Convert the run instant first; subtracting a calendar day then follows Vienna's DST rules.
    return scheduled_at.astimezone(VIENNA).date() - timedelta(days=1)


def _run_token(context) -> str:
    """Create a stable 12-character filesystem token from the Airflow run ID.

    Args:
        context: Airflow task context containing ``run_id``.

    Returns:
        The first 12 lowercase hexadecimal characters of the run ID's SHA-256.
    """
    run_id = str(context["run_id"])
    return hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:12]


def _run_process(command: list[str]) -> None:
    """Run one pipeline script from the mounted project directory.

    Args:
        command: Argument vector passed directly to a child process; no shell is
            used.

    Side effects:
        Starts the requested subprocess, forwards its captured standard output
        and error streams to Airflow logs, and sets the daily-data root in the
        child environment.

    Raises:
        RuntimeError: If the child exits with a non-zero status.
    """
    child_environment = os.environ.copy()
    child_environment["AIRFLOW_DAILY_DATA_DIR"] = str(DAILY_DATA_DIR)
    result = subprocess.run(
        command,
        cwd=str(PROJECT_DIR),
        check=False,
        capture_output=True,
        text=True,
        env=child_environment,
    )
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    if result.returncode != 0:
        raise RuntimeError(f"Daily pipeline subprocess failed with exit code {result.returncode}")


def crawl_daily_articles(**context) -> str:
    """Run the date-scoped crawler and return its CSV path for XCom.

    Airflow uses the resolved target date and run ID to isolate each output file.
    The crawler performs the HTTP and CSV-writing work; this task only builds its
    command and checks the subprocess result.

    Returns:
        Absolute path to this run's ``articles.csv``.

    Raises:
        RuntimeError: If the crawler subprocess fails.
    """
    target_date = _target_date(context)
    output_path = DAILY_DATA_DIR / target_date.isoformat() / _run_token(context) / "articles.csv"
    command = [
        sys.executable,
        str(CRAWLER_SCRIPT),
        "--target-date",
        target_date.isoformat(),
        "--output",
        str(output_path),
    ]
    _run_process(command)
    return str(output_path)


def import_daily_articles(**context) -> None:
    """Import the CSV produced by this same DAG run.

    The task verifies that the XCom path exactly matches the expected date and
    run token before invoking the importer with the dedicated Airflow connection.
    The importer owns CSV validation and the MySQL transaction.

    Raises:
        ValueError: If the crawler did not return a path or the path belongs to
            another date/run.
        RuntimeError: If the importer subprocess fails.
    """
    target_date = _target_date(context)
    csv_path = context["ti"].xcom_pull(task_ids="crawl_daily_articles")
    if not csv_path:
        raise ValueError("crawler task did not return a CSV path")
    expected_path = DAILY_DATA_DIR / target_date.isoformat() / _run_token(context) / "articles.csv"
    if Path(csv_path) != expected_path:
        raise ValueError("crawler CSV path does not match this DAG run")
    _run_process(
        [
            sys.executable,
            str(IMPORT_SCRIPT),
            "--target-date",
            target_date.isoformat(),
            "--csv",
            str(expected_path),
            "--airflow-connection-id",
            AIRFLOW_DB_CONNECTION_ID,
        ]
    )


with DAG(
    dag_id="daily_article_pipeline",
    description="Daily DerStandard crawl and idempotent MySQL import",
    # Airflow derives the DAG timezone from this aware start date; cron runs at local 01:00.
    start_date=pendulum.datetime(2025, 1, 1, tz="Europe/Vienna"),
    schedule="0 1 * * *",
    catchup=False,
    # Serialize daily imports so two dates cannot write concurrently through this DAG.
    max_active_runs=1,
    default_args={"owner": "airflow", "retries": 0},
    tags=["reconstruction", "daily"],
) as dag:
    crawl_task = PythonOperator(
        task_id="crawl_daily_articles",
        python_callable=crawl_daily_articles,
    )
    import_task = PythonOperator(
        task_id="import_daily_articles",
        python_callable=import_daily_articles,
    )

    crawl_task >> import_task
