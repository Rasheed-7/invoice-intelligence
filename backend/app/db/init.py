"""Database seeding helpers.

Nothing here is wired into application startup yet - callers invoke these
explicitly (for example from a management script or a test fixture).
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.user import User

SYSTEM_USER_EMAIL = "system@local"
SYSTEM_USER_DISPLAY_NAME = "System"


def seed_system_user(db: Session) -> None:
    """Ensure a single system user exists.

    Idempotent: if a user with :data:`SYSTEM_USER_EMAIL` is already present
    (committed or pending in this session) this is a no-op. The insert is
    flushed but NOT committed - the caller owns the transaction, which keeps
    the helper safe to use inside a rolled-back test transaction.
    """

    exists = db.execute(
        select(User.id).where(User.email == SYSTEM_USER_EMAIL)
    ).first()
    if exists is not None:
        return

    db.add(User(email=SYSTEM_USER_EMAIL, display_name=SYSTEM_USER_DISPLAY_NAME))
    db.flush()
