"""Binary object storage abstraction.

This step only ships a local-disk backend. The :class:`StorageBackend`
protocol keeps the persistence contract small (``save`` / ``read``) so a
remote backend could be added later without touching callers.

Keys are slash-separated, relative, forward-slash paths (for example
``ab/cd/<sha256>.bin``) and are always resolved *under* the storage root.
Every key is validated to reject path traversal and absolute paths.
"""

from functools import lru_cache
from pathlib import Path
from typing import Protocol

from app.core.config import get_settings

__all__ = ["LocalStorage", "StorageBackend", "get_storage"]


class StorageBackend(Protocol):
    """Minimal contract every storage backend must satisfy."""

    def save(self, key: str, data: bytes) -> str:
        """Persist ``data`` under ``key`` and return the stored key."""

    def read(self, key: str) -> bytes:
        """Return the bytes previously stored under ``key``."""


class LocalStorage:
    """Store objects as files beneath a single root directory."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _resolve(self, key: str) -> Path:
        """Map ``key`` to an absolute path beneath :attr:`root`.

        Rejects keys that could escape the storage root. This is a defence
        against path traversal: only simple, relative, forward-slash keys are
        accepted.
        """

        # Reject Windows-style separators outright rather than normalising
        # them - accepting them would silently allow keys we never intended.
        if "\\" in key:
            raise ValueError(f"unsafe storage key: {key!r}")

        candidate = Path(key)
        if candidate.is_absolute() or key.startswith("/"):
            raise ValueError(f"unsafe storage key: {key!r}")

        # Explicitly reject ".." segments (belt-and-braces: the containment
        # check below would also catch them).
        if ".." in candidate.parts:
            raise ValueError(f"unsafe storage key: {key!r}")

        root = self.root.resolve()
        resolved = (root / candidate).resolve()
        if not resolved.is_relative_to(root):
            raise ValueError(f"unsafe storage key: {key!r}")

        return resolved

    def save(self, key: str, data: bytes) -> str:
        """Write ``data`` to ``key``, creating parent directories as needed.

        Idempotent by content: because keys are content-addressed, an existing
        file is assumed to already hold the same bytes and is left untouched.
        """

        path = self._resolve(key)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return key

    def read(self, key: str) -> bytes:
        """Return the bytes stored under ``key``."""

        return self._resolve(key).read_bytes()


@lru_cache
def get_storage() -> StorageBackend:
    """Return the process-wide storage backend.

    Cached so the backend is not rebuilt for every request. Tests override the
    root by calling ``get_storage.cache_clear()``.
    """

    return LocalStorage(Path(get_settings().storage_root))
