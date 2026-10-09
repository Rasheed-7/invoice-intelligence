"""Shared fixtures for the API tests.

Three fixtures are provided to every test that needs them:

* ``db_session`` - a session whose work is rolled back on teardown, so no rows
  are left behind in Postgres between runs.
* ``storage_root`` - storage pointed at a per-test temporary directory, so no
  upload artifacts are left behind on disk.
* ``client`` - a ``TestClient`` wired to both of the above, so HTTP tests get
  the same rollback and temp-directory guarantees without repeating them.
"""

from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import SessionLocal, get_db
from app.main import app
from app.services.storage import get_storage

_DB = "db_session"
_STORAGE = "storage_root"


@pytest.fixture
def storage_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[Path, None, None]:
    """Redirect the storage backend at a temporary directory for one test."""

    monkeypatch.setattr(get_settings(), "storage_root", str(tmp_path))
    get_storage.cache_clear()
    try:
        yield tmp_path
    finally:
        get_storage.cache_clear()


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    """Yield a session bound to an outer transaction that is later rolled back.

    ``create_savepoint`` lets the route's ``commit()`` release a savepoint
    inside the outer transaction instead of committing for real, so the
    rollback in teardown discards everything the test wrote.
    """

    engine = SessionLocal.kw["bind"]
    connection = engine.connect()
    outer = connection.begin()
    session = SessionLocal(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        outer.rollback()
        connection.close()


@pytest.fixture
def client(
    db_session: Session, storage_root: Path
) -> Generator[TestClient, None, None]:
    """A TestClient wired to the rolled-back session and temp storage root."""

    app.dependency_overrides[get_db] = lambda: db_session
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()