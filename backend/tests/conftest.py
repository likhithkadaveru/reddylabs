"""Test configuration.

Integration tests run against a real Postgres database because the engine relies
on real SQL semantics (JSONB, arrays, CHECK constraints, UNION in subqueries).
Testing that against SQLite would prove nothing about production behaviour.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

TEST_DB_URL = os.environ.get(
    "MR_TEST_DATABASE_URL",
    "postgresql+psycopg://postgres:postgres@localhost:5432/moleculeradar_test",
)

# Settings must be configured before application modules read them.
os.environ.setdefault("MR_DATABASE_URL", TEST_DB_URL)
os.environ.setdefault("MR_ENVIRONMENT", "development")
os.environ.setdefault("MR_HTTP_CONTACT_EMAIL", "test@example.invalid")


@pytest.fixture(scope="session")
def engine():
    from moleculeradar.core.models import Base

    eng = create_engine(TEST_DB_URL, future=True)
    try:
        with eng.connect() as conn:
            conn.execute(text("select 1"))
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"Test Postgres unavailable at {TEST_DB_URL}: {exc}")

    Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine) -> Session:
    """A session wrapped in a transaction that is always rolled back.

    Each test therefore sees a clean database without paying for schema recreation.
    """
    connection = engine.connect()
    transaction = connection.begin()
    factory = sessionmaker(bind=connection, expire_on_commit=False, future=True)
    sess = factory()
    try:
        yield sess
    finally:
        sess.close()
        transaction.rollback()
        connection.close()
