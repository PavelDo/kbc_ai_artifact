"""Application settings.

All configuration comes from environment variables. Required variables have no
defaults — the app fails fast at startup with a clear error instead of
inventing values. Optional limits have documented defaults overridable via env.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

REQUIRED_ENV = ["HUB_STORAGE_TOKEN", "HUB_STACK_URL", "HUB_SECRET_KEY"]


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    # Host project access (where serving envelopes live)
    hub_storage_token: str
    hub_stack_url: str
    # Signs unlock cookies
    secret_key: str
    # Optional absolute base URL used in returned artifact URLs
    public_base_url: str | None = None
    cache_dir: Path = field(default_factory=lambda: Path("/tmp/artifact-cache"))

    # Limits (bytes / seconds / counts)
    max_html_bytes: int = 15 * 1024 * 1024
    max_inline_image_bytes: int = 5 * 1024 * 1024
    max_inline_total_bytes: int = 15 * 1024 * 1024
    git_clone_timeout_s: int = 90
    git_max_repo_bytes: int = 200 * 1024 * 1024
    cache_max_entries: int = 200
    unlock_cookie_max_age_s: int = 12 * 3600
    token_verify_timeout_s: int = 15
    # Community versioning (phase 2)
    # Live versions kept per artifact; older non-head, non-pinned ones are pruned.
    max_versions: int = 50
    # Per-contributor cap on submitted versions per rolling day.
    max_versions_per_day: int = 20
    # Largest per-side payload the diff renderer will process.
    diff_max_bytes: int = 2 * 1024 * 1024
    # Extra stack URLs (comma-separated) beyond the *.keboola.com rule
    extra_stacks: tuple[str, ...] = ()


def load_settings() -> Settings:
    missing = [v for v in REQUIRED_ENV if not os.environ.get(v)]
    if missing:
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}"
        )
    extra = tuple(
        s.strip().rstrip("/")
        for s in os.environ.get("HUB_EXTRA_STACKS", "").split(",")
        if s.strip()
    )
    return Settings(
        hub_storage_token=os.environ["HUB_STORAGE_TOKEN"],
        hub_stack_url=os.environ["HUB_STACK_URL"].rstrip("/"),
        secret_key=os.environ["HUB_SECRET_KEY"],
        public_base_url=os.environ.get("HUB_PUBLIC_BASE_URL", "").rstrip("/") or None,
        cache_dir=Path(os.environ.get("HUB_CACHE_DIR", "/tmp/artifact-cache")),
        max_html_bytes=_int_env("HUB_MAX_HTML_BYTES", 15 * 1024 * 1024),
        max_inline_image_bytes=_int_env("HUB_MAX_INLINE_IMAGE_BYTES", 5 * 1024 * 1024),
        max_inline_total_bytes=_int_env("HUB_MAX_INLINE_TOTAL_BYTES", 15 * 1024 * 1024),
        git_clone_timeout_s=_int_env("HUB_GIT_CLONE_TIMEOUT_S", 90),
        git_max_repo_bytes=_int_env("HUB_GIT_MAX_REPO_BYTES", 200 * 1024 * 1024),
        cache_max_entries=_int_env("HUB_CACHE_MAX_ENTRIES", 200),
        unlock_cookie_max_age_s=_int_env("HUB_UNLOCK_COOKIE_MAX_AGE_S", 12 * 3600),
        token_verify_timeout_s=_int_env("HUB_TOKEN_VERIFY_TIMEOUT_S", 15),
        max_versions=_int_env("HUB_MAX_VERSIONS", 50),
        max_versions_per_day=_int_env("HUB_MAX_VERSIONS_PER_DAY", 20),
        diff_max_bytes=_int_env("HUB_DIFF_MAX_BYTES", 2 * 1024 * 1024),
        extra_stacks=extra,
    )
