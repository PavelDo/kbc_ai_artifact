"""Tests for src.builder: HTML/Markdown/git artifact building.

``build_from_git`` needs a public https clone to run end-to-end, which is not
available offline. Its input validation (https-only) is exercised directly,
and the private helpers it delegates to for entry-file selection and image
inlining (``_default_entry``, ``_resolve_entry``, ``_inline_images``) are
tested directly against a plain directory tree / a local git fixture repo,
since those helpers operate on any filesystem path and do not themselves
require a network clone.
"""

import base64
import subprocess
from pathlib import Path

import pytest

import src.builder as builder_module
from src.builder import (
    BuildError,
    DEFAULT_GIT_USERNAME,
    DEFAULT_TITLE,
    REDACTED,
    _authed_clone_url,
    _default_entry,
    _inline_images,
    _last_line,
    _resolve_entry,
    _scrub,
    _validate_git_url,
    build_from_git,
    build_from_html,
    build_from_markdown,
)

# A well-known minimal valid 1x1 PNG (base64), decoded at test time so the
# fixture always contains real, valid image bytes.
_PNG_1X1_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _png_bytes() -> bytes:
    return base64.b64decode(_PNG_1X1_B64)


def _run_git(args: list[str], cwd: Path) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


def _make_fixture_repo(tmp_path: Path) -> Path:
    """A small local git repo: README.md with a relative image reference."""
    repo = tmp_path / "fixture-repo"
    repo.mkdir()
    images_dir = repo / "images"
    images_dir.mkdir()
    (images_dir / "pixel.png").write_bytes(_png_bytes())
    (repo / "README.md").write_text(
        "# Fixture Repo\n\n![pixel](images/pixel.png)\n", encoding="utf-8"
    )

    _run_git(["git", "init"], repo)
    _run_git(["git", "config", "user.email", "test@example.com"], repo)
    _run_git(["git", "config", "user.name", "Test User"], repo)
    _run_git(["git", "add", "-A"], repo)
    _run_git(["git", "commit", "-m", "initial commit"], repo)
    return repo


# --------------------------------------------------------------------------
# build_from_html
# --------------------------------------------------------------------------


class TestBuildFromHtml:
    def test_title_tag_wins_over_h1(self):
        html = (
            "<html><head><title>From Title</title></head>"
            "<body><h1>From H1</h1></body></html>"
        )
        result = build_from_html(html)
        assert result.title == "From Title"
        assert result.source_type == "html"
        assert result.html == html

    def test_h1_used_when_no_title_tag(self):
        html = "<body><h1>From H1</h1></body>"
        result = build_from_html(html)
        assert result.title == "From H1"

    def test_explicit_title_wins_over_everything(self):
        html = "<html><head><title>From Title</title></head><body><h1>From H1</h1></body></html>"
        result = build_from_html(html, title="Explicit Title")
        assert result.title == "Explicit Title"

    def test_fallback_default_title(self):
        html = "<body><p>no headings here</p></body>"
        result = build_from_html(html)
        assert result.title == DEFAULT_TITLE


# --------------------------------------------------------------------------
# build_from_markdown
# --------------------------------------------------------------------------


class TestBuildFromMarkdown:
    def test_table_renders(self):
        md = "| a | b |\n| --- | --- |\n| 1 | 2 |\n"
        result = build_from_markdown(md)
        assert "<table>" in result.html
        assert "<th>a</th>" in result.html
        assert "<td>1</td>" in result.html

    def test_mermaid_fence_becomes_pre_mermaid(self):
        md = "```mermaid\ngraph TD;\nA-->B;\n```\n"
        result = build_from_markdown(md)
        assert '<pre class="mermaid">' in result.html
        assert "graph TD" in result.html

    def test_python_fence_gets_language_class(self):
        md = "```python\nprint('hi')\n```\n"
        result = build_from_markdown(md)
        assert "language-python" in result.html

    def test_front_matter_title(self):
        md = '---\ntitle: "From Front Matter"\n---\n\n# A Heading\n'
        result = build_from_markdown(md)
        assert result.title == "From Front Matter"

    def test_h1_title_fallback_when_no_front_matter(self):
        md = "# My Heading\n\nSome body text.\n"
        result = build_from_markdown(md)
        assert result.title == "My Heading"

    def test_fallback_default_title(self):
        md = "Just a paragraph, no heading at all.\n"
        result = build_from_markdown(md)
        assert result.title == DEFAULT_TITLE

    def test_full_page_with_dark_mode_media_query(self):
        md = "# Title\n\nBody text.\n"
        result = build_from_markdown(md)
        lowered = result.html.lower()
        assert "<!doctype html" in lowered
        assert "<html" in lowered
        assert "prefers-color-scheme: dark" in result.html
        assert result.source_type == "markdown"


# --------------------------------------------------------------------------
# build_from_git: URL validation
# --------------------------------------------------------------------------


class TestValidateGitUrl:
    def test_https_url_accepted(self):
        url = "https://github.com/owner/repo.git"
        assert _validate_git_url(url) == url

    def test_file_url_rejected(self):
        with pytest.raises(BuildError):
            _validate_git_url("file:///etc/passwd")

    def test_ftp_scheme_rejected(self):
        with pytest.raises(BuildError):
            _validate_git_url("ftp://example.com/repo.git")

    def test_plain_http_rejected(self):
        with pytest.raises(BuildError):
            _validate_git_url("http://example.com/repo.git")

    def test_scheme_only_no_hostname_rejected(self):
        with pytest.raises(BuildError):
            _validate_git_url("https://")


class TestBuildFromGitRejectsNonHttps(object):
    """build_from_git must reject non-https URLs before attempting a clone."""

    def test_file_url_rejected(self, settings):
        with pytest.raises(BuildError):
            build_from_git("file:///tmp/some-repo", None, None, None, settings)

    def test_ftp_url_rejected(self, settings):
        with pytest.raises(BuildError):
            build_from_git("ftp://example.com/repo.git", None, None, None, settings)

    def test_ssh_style_url_rejected(self, settings):
        with pytest.raises(BuildError):
            build_from_git(
                "git@github.com:owner/repo.git", None, None, None, settings
            )


# --------------------------------------------------------------------------
# Private-repository clone credentials
# --------------------------------------------------------------------------


class TestAuthedCloneUrl:
    """URL construction for an authenticated clone.

    This is the only place the git token is ever materialized, so its exact
    shape (and its quoting) is worth pinning down.
    """

    def test_default_username_is_used_when_none_given(self):
        assert (
            _authed_clone_url("https://github.com/owner/repo.git", None, "ghp_abc")
            == f"https://{DEFAULT_GIT_USERNAME}:ghp_abc@github.com/owner/repo.git"
        )

    def test_explicit_username_is_used(self):
        assert (
            _authed_clone_url("https://gitlab.com/g/p.git", "deploy-user", "s3cr3t")
            == "https://deploy-user:s3cr3t@gitlab.com/g/p.git"
        )

    def test_special_characters_are_percent_encoded(self):
        # A token containing '@', ':' and '/' must not be able to break out of
        # the userinfo section and rewrite the host or the path.
        url = _authed_clone_url(
            "https://github.com/owner/repo.git", "user@example.com", "p@ss:w/rd +x"
        )
        assert url == (
            "https://user%40example.com:p%40ss%3Aw%2Frd%20%2Bx"
            "@github.com/owner/repo.git"
        )

    def test_host_and_port_are_preserved(self):
        url = _authed_clone_url("https://git.example.com:8443/g/p.git", None, "tok")
        assert url.endswith("@git.example.com:8443/g/p.git")

    def test_existing_userinfo_is_replaced_not_appended(self):
        url = _authed_clone_url("https://someone:old@github.com/o/r.git", None, "new")
        assert url == f"https://{DEFAULT_GIT_USERNAME}:new@github.com/o/r.git"
        assert "old" not in url

    def test_no_credentials_leak_into_the_unauthenticated_url(self):
        original = "https://github.com/owner/repo.git"
        _authed_clone_url(original, None, "ghp_abc")
        assert original == "https://github.com/owner/repo.git"


class _FailedGit:
    """Stand-in for a ``subprocess.CompletedProcess`` from a failed git call."""

    returncode = 128
    stdout = ""

    def __init__(self, stderr: str) -> None:
        self.stderr = stderr


class TestScrubbing:
    """Nothing derived from git output may carry the token back to the caller."""

    def test_authed_clone_url_in_git_stderr_is_scrubbed(self):
        token = "ghp_SuperSecretToken123"
        authed = _authed_clone_url("https://github.com/o/private.git", None, token)
        stderr = (
            "Cloning into '/tmp/artifact-git-x/repo'...\n"
            f"fatal: unable to access '{authed}/': "
            "The requested URL returned error: 403\n"
        )
        # Exactly how _clone builds the user-facing detail of a BuildError.
        reason = _last_line(stderr, token)
        message = str(BuildError(f"Could not clone the repository. git said: {reason}"))

        assert token not in message
        assert DEFAULT_GIT_USERNAME not in message
        assert "github.com" in message  # the useful part survives

    def test_token_with_special_chars_is_scrubbed_in_encoded_form(self):
        token = "p@ss/w:rd"
        authed = _authed_clone_url("https://github.com/o/p.git", None, token)
        assert _scrub(f"fatal: could not read from {authed}", token) == (
            "fatal: could not read from https://github.com/o/p.git"
        )

    def test_bare_token_without_url_is_still_redacted(self):
        token = "glpat-Abc123"
        scrubbed = _scrub(f"remote: token {token} is expired", token)
        assert token not in scrubbed
        assert REDACTED in scrubbed

    def test_scrub_without_secret_still_strips_userinfo(self):
        assert _scrub("https://user:pw@example.com/r.git") == (
            "https://example.com/r.git"
        )

    def test_private_repo_error_points_at_git_token(self, monkeypatch, tmp_path):
        """An unauthenticated clone of a private repo must suggest git_token."""
        monkeypatch.setattr(
            builder_module,
            "_run_git",
            lambda args, timeout_s: _FailedGit(
                "fatal: could not read Username for 'https://github.com': "
                "terminal prompts disabled\n"
            ),
        )
        with pytest.raises(BuildError) as exc:
            builder_module._clone(
                "https://github.com/o/private.git", None, tmp_path / "repo", 5
            )
        assert "git_token" in str(exc.value)

    def test_failed_authed_clone_error_carries_no_token(self, monkeypatch, tmp_path):
        """The end-to-end path: a rejected authenticated clone leaks nothing."""
        token = "ghp_LeakMeIfYouCan"
        authed = _authed_clone_url("https://github.com/o/private.git", None, token)
        monkeypatch.setattr(
            builder_module,
            "_run_git",
            lambda args, timeout_s: _FailedGit(
                f"fatal: unable to access '{authed}/': "
                "The requested URL returned error: 403\n"
            ),
        )
        with pytest.raises(BuildError) as exc:
            builder_module._clone(
                "https://github.com/o/private.git",
                "main",
                tmp_path / "repo",
                5,
                token=token,
            )
        message = str(exc.value)
        assert token not in message
        assert DEFAULT_GIT_USERNAME not in message


# --------------------------------------------------------------------------
# Entry-file selection helpers (private, but importable and independently
# testable: they operate on a plain directory tree, no git/network involved)
# --------------------------------------------------------------------------


class TestDefaultEntrySelection:
    def test_prefers_index_html(self, tmp_path):
        (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
        (tmp_path / "README.md").write_text("# Readme", encoding="utf-8")
        entry = _default_entry(tmp_path, "the repository root")
        assert entry.name == "index.html"

    def test_falls_back_to_readme(self, tmp_path):
        (tmp_path / "README.md").write_text("# Readme", encoding="utf-8")
        entry = _default_entry(tmp_path, "the repository root")
        assert entry.name == "README.md"

    def test_readme_lookup_is_case_insensitive(self, tmp_path):
        (tmp_path / "readme.markdown").write_text("# Readme", encoding="utf-8")
        entry = _default_entry(tmp_path, "the repository root")
        assert entry.name == "readme.markdown"

    def test_falls_back_to_single_html_file(self, tmp_path):
        (tmp_path / "page.html").write_text("<html></html>", encoding="utf-8")
        entry = _default_entry(tmp_path, "the repository root")
        assert entry.name == "page.html"

    def test_raises_when_nothing_matches(self, tmp_path):
        (tmp_path / "notes.txt").write_text("hi", encoding="utf-8")
        with pytest.raises(BuildError):
            _default_entry(tmp_path, "the repository root")

    def test_raises_when_multiple_html_files_ambiguous(self, tmp_path):
        (tmp_path / "a.html").write_text("<html></html>", encoding="utf-8")
        (tmp_path / "b.html").write_text("<html></html>", encoding="utf-8")
        with pytest.raises(BuildError):
            _default_entry(tmp_path, "the repository root")


class TestResolveEntry:
    def test_explicit_path_to_file(self, tmp_path):
        (tmp_path / "docs.md").write_text("# Docs", encoding="utf-8")
        entry = _resolve_entry(tmp_path, "docs.md")
        assert entry.name == "docs.md"

    def test_explicit_path_to_directory_uses_default_entry(self, tmp_path):
        sub = tmp_path / "docs"
        sub.mkdir()
        (sub / "index.html").write_text("<html></html>", encoding="utf-8")
        entry = _resolve_entry(tmp_path, "docs")
        assert entry.name == "index.html"

    def test_no_path_uses_default_entry(self, tmp_path):
        (tmp_path / "index.htm").write_text("<html></html>", encoding="utf-8")
        entry = _resolve_entry(tmp_path, None)
        assert entry.name == "index.htm"

    def test_rejects_path_escaping_repo_root(self, tmp_path):
        with pytest.raises(BuildError):
            _resolve_entry(tmp_path, "../outside.md")

    def test_rejects_disallowed_suffix(self, tmp_path):
        (tmp_path / "data.json").write_text("{}", encoding="utf-8")
        with pytest.raises(BuildError):
            _resolve_entry(tmp_path, "data.json")

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(BuildError):
            _resolve_entry(tmp_path, "missing.md")


# --------------------------------------------------------------------------
# Image inlining helper (private, testable against a local fixture repo)
# --------------------------------------------------------------------------


class TestInlineImages:
    def test_relative_image_is_inlined_as_data_uri(self, tmp_path, settings):
        repo = _make_fixture_repo(tmp_path)
        html = '<img src="images/pixel.png" alt="pixel">'
        result = _inline_images(
            html,
            base_dir=repo,
            repo_root=repo,
            max_image_bytes=settings.max_inline_image_bytes,
            max_total_bytes=settings.max_inline_total_bytes,
        )
        assert "data:image/png;base64," in result
        assert "images/pixel.png" not in result

    def test_external_url_left_untouched(self, tmp_path, settings):
        repo = _make_fixture_repo(tmp_path)
        html = '<img src="https://example.com/pic.png">'
        result = _inline_images(
            html,
            base_dir=repo,
            repo_root=repo,
            max_image_bytes=settings.max_inline_image_bytes,
            max_total_bytes=settings.max_inline_total_bytes,
        )
        assert result == html

    def test_oversized_image_left_as_link(self, tmp_path, settings):
        repo = _make_fixture_repo(tmp_path)
        html = '<img src="images/pixel.png">'
        result = _inline_images(
            html,
            base_dir=repo,
            repo_root=repo,
            max_image_bytes=1,  # smaller than the fixture PNG
            max_total_bytes=settings.max_inline_total_bytes,
        )
        assert result == html

    def test_total_budget_exhausted_leaves_image_as_link(self, tmp_path, settings):
        repo = _make_fixture_repo(tmp_path)
        html = '<img src="images/pixel.png">'
        result = _inline_images(
            html,
            base_dir=repo,
            repo_root=repo,
            max_image_bytes=settings.max_inline_image_bytes,
            max_total_bytes=0,
        )
        assert result == html

    def test_image_outside_repo_root_left_untouched(self, tmp_path, settings):
        repo = _make_fixture_repo(tmp_path)
        (tmp_path / "outside.png").write_bytes(_png_bytes())
        html = '<img src="../outside.png">'
        result = _inline_images(
            html,
            base_dir=repo,
            repo_root=repo,
            max_image_bytes=settings.max_inline_image_bytes,
            max_total_bytes=settings.max_inline_total_bytes,
        )
        assert result == html

    def test_missing_image_left_untouched(self, tmp_path, settings):
        repo = _make_fixture_repo(tmp_path)
        html = '<img src="images/does-not-exist.png">'
        result = _inline_images(
            html,
            base_dir=repo,
            repo_root=repo,
            max_image_bytes=settings.max_inline_image_bytes,
            max_total_bytes=settings.max_inline_total_bytes,
        )
        assert result == html
