from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

_settings = get_settings()

# Engine is created at import time but does not open a connection
# until the first query. The app can therefore boot (e.g. for /api/health)
# even when Postgres is not running.
_engine = create_engine(
    _settings.database_url,
    pool_pre_ping=True,
    future=True,
    # Fail fast when the DB is down so readiness probes don't hang.
    connect_args={"connect_timeout": 3},
)

SessionLocal = sessionmaker(
    bind=_engine,
    autoflush=False,
    autocommit=False,
    future=True,
)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a scoped DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()