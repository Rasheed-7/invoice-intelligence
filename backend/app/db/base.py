from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


# Importing models here has the side effect of registering them with
# Base.metadata, which Alembic autogenerate relies on. Keep this import
# at the bottom to avoid circular imports.
from app import models  # noqa: E402,F401