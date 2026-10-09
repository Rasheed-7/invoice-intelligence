"""Tests for the local storage backend."""

from pathlib import Path

import pytest

from app.services.storage import LocalStorage


def test_local_storage_roundtrip(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    key = "ab/cd/" + "f" * 64 + ".bin"

    assert storage.save(key, b"payload") == key
    assert storage.read(key) == b"payload"

    # The parent directories were created beneath the root.
    assert (tmp_path / "ab" / "cd" / f"{'f' * 64}.bin").read_bytes() == b"payload"


@pytest.mark.parametrize("key", ["../evil", "/abs/path", "a\\b"])
def test_local_storage_rejects_traversal(tmp_path: Path, key: str) -> None:
    storage = LocalStorage(tmp_path)

    with pytest.raises(ValueError):
        storage.save(key, b"x")

    with pytest.raises(ValueError):
        storage.read(key)