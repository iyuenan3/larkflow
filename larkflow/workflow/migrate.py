"""Apply packaged PostgreSQL migrations exactly once."""
from __future__ import annotations

from collections.abc import Callable
from importlib.resources import files
from typing import Any

import psycopg
from psycopg.rows import dict_row


ConnectionFactory = Callable[[], Any]
MIGRATION_LOCK_KEY = "larkflow.workflow.migrations.v1"


def postgres_connection_factory(dsn: str) -> ConnectionFactory:
    return lambda: psycopg.connect(dsn, autocommit=True, row_factory=dict_row)


def available_migrations() -> tuple[tuple[str, str], ...]:
    root = files("larkflow.workflow.migrations")
    migrations = []
    for resource in root.iterdir():
        if resource.name.endswith(".sql"):
            migrations.append((resource.name[:-4], resource.read_text(encoding="utf-8")))
    return tuple(sorted(migrations))


def apply_migrations(connection_factory: ConnectionFactory) -> tuple[str, ...]:
    applied: list[str] = []
    with connection_factory() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS workflow_schema_migrations (
                version text PRIMARY KEY,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        for version, sql in available_migrations():
            with connection.transaction():
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (MIGRATION_LOCK_KEY,),
                )
                exists = connection.execute(
                    "SELECT 1 FROM workflow_schema_migrations WHERE version = %s",
                    (version,),
                ).fetchone()
                if exists:
                    continue
                connection.execute(sql, prepare=False)
                connection.execute(
                    "INSERT INTO workflow_schema_migrations (version) VALUES (%s)",
                    (version,),
                )
                applied.append(version)
    return tuple(applied)
