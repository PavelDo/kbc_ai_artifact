"""Shared pytest fixtures for the KBC Artifact Hub test suite.

Required environment variables are set (via ``setdefault``, so a developer's
own shell env still wins) before anything in ``src`` is imported, since
``src.config.load_settings`` fails fast when they are missing.
"""

import os

os.environ.setdefault("HUB_STORAGE_TOKEN", "test-token")
os.environ.setdefault("HUB_STACK_URL", "https://connection.keboola.com")
os.environ.setdefault(
    # At least MIN_SECRET_KEY_CHARS (32) characters — load_settings rejects a
    # shorter signing key, since it is the HMAC key for every unlock cookie.
    "HUB_SECRET_KEY",
    "test-secret-key-for-tests-only-0123456789",
)

import pytest

from src.config import Settings, load_settings
from src.kbc import InMemoryFilesBackend
from src.store import ArtifactStore


@pytest.fixture
def settings() -> Settings:
    """Application settings loaded from the environment set up above."""
    return load_settings()


@pytest.fixture
def backend() -> InMemoryFilesBackend:
    """A fresh in-memory Storage Files backend."""
    return InMemoryFilesBackend()


@pytest.fixture
def tmp_store(backend: InMemoryFilesBackend, tmp_path, settings: Settings) -> ArtifactStore:
    """An ArtifactStore over ``backend`` with an on-disk cache under tmp_path."""
    cache_dir = tmp_path / "cache"
    return ArtifactStore(
        backend=backend,
        cache_dir=cache_dir,
        cache_max_entries=settings.cache_max_entries,
        max_versions=settings.max_versions,
    )
