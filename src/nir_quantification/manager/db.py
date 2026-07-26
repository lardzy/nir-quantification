from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .config import ManagerSettings
from .models import Base


CURRENT_SCHEMA_VERSION = 1


def create_sqlite_engine(settings: ManagerSettings):
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{settings.db_path}",
        future=True,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _connection_record) -> None:  # pragma: no cover
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.execute("PRAGMA busy_timeout=5000;")
        cursor.close()

    return engine


def create_session_factory(engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def init_database(engine) -> None:
    Base.metadata.create_all(engine)


def ensure_runtime_schema(engine) -> None:
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS app_schema_version (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                version INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        stored_version = connection.exec_driver_sql(
            "SELECT version FROM app_schema_version WHERE id = 1"
        ).scalar_one_or_none()
        if stored_version is not None and int(stored_version) > CURRENT_SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema version {stored_version} is newer than supported version {CURRENT_SCHEMA_VERSION}"
            )
        columns = {
            str(row[1])
            for row in connection.exec_driver_sql("PRAGMA table_info('spectra')").all()
        }
        if "content_sha256" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE spectra ADD COLUMN content_sha256 VARCHAR(64)"
            )
        connection.exec_driver_sql(
            """
            INSERT INTO app_schema_version (id, version, updated_at)
            VALUES (1, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                version = excluded.version,
                updated_at = excluded.updated_at
            """,
            (CURRENT_SCHEMA_VERSION,),
        )


def ensure_runtime_indexes(engine) -> None:
    statements = [
        "CREATE INDEX IF NOT EXISTS ix_spectra_class_excluded_axis_file ON spectra (class_key, is_excluded, axis_kind, file_name)",
        "CREATE INDEX IF NOT EXISTS ix_spectra_class_excluded_file ON spectra (class_key, is_excluded, file_name)",
        "CREATE INDEX IF NOT EXISTS ix_spectra_class_component_excluded_file ON spectra (class_key, component_count, is_excluded, file_name)",
        "CREATE INDEX IF NOT EXISTS ix_spectra_excluded_recent ON spectra (is_excluded, excluded_at)",
        "CREATE INDEX IF NOT EXISTS ix_spectra_content_sha256 ON spectra (content_sha256)",
    ]
    with engine.begin() as connection:
        for statement in statements:
            connection.exec_driver_sql(statement)


@contextmanager
def session_scope(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def encode_json(data) -> str:
    return json.dumps(data, ensure_ascii=False)


def decode_json(value: str | None, default):
    if not value:
        return default
    return json.loads(value)
