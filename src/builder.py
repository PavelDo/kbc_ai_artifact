"""Artifact builder.

Turns publish inputs (raw HTML, Markdown or a public git repository) into one
final, self-contained HTML document.

Three entry points mirror the three publish input shapes:

* :func:`build_from_html` — pass-through, only the title is derived.
* :func:`build_from_markdown` — markdown-it-py rendering wrapped in
  :data:`PAGE_TEMPLATE` (tables, task lists, anchors, mermaid, highlight.js).
* :func:`build_from_git` — shallow clone, entry-file selection, the same
  Markdown rendering, plus inlining of relative images as ``data:`` URIs.

Every failure that is the caller's fault raises :class:`BuildError`; its message
is user-facing and is mapped to HTTP 422 by the API layer.

The only outbound network access in this module is the ``git clone``
subprocess. Everything else is local.

Private repositories are supported by passing a transient ``git_token`` (and
optionally ``git_username``) to :func:`build_from_git`. The credentials are
injected into the clone URL for the duration of that one subprocess call and
nothing else: the *unauthenticated* URL is what gets logged, recorded and
echoed back, and every string that can reach a :class:`BuildError` message goes
through :func:`_scrub` with the token as an extra literal to redact.
"""

from __future__ import annotations

import base64
import html as html_lib
import logging
import mimetypes
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import quote, unquote, urlparse, urlunparse

from markdown_it import MarkdownIt
from mdit_py_plugins.anchors import anchors_plugin
from mdit_py_plugins.front_matter import front_matter_plugin
from mdit_py_plugins.tasklists import tasklists_plugin

if TYPE_CHECKING:  # pragma: no cover - typing only
    from src.config import Settings

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

DEFAULT_TITLE = "Untitled artifact"

#: Suffixes accepted as an explicit git entry file.
HTML_SUFFIXES: frozenset[str] = frozenset({".html", ".htm"})
MARKDOWN_SUFFIXES: frozenset[str] = frozenset({".md", ".markdown"})
ALLOWED_ENTRY_SUFFIXES: frozenset[str] = HTML_SUFFIXES | MARKDOWN_SUFFIXES

#: Default entry-file lookup order inside a repository directory (lowercased).
INDEX_CANDIDATES: tuple[str, ...] = ("index.html", "index.htm")
README_CANDIDATES: tuple[str, ...] = ("readme.md", "readme.markdown")

#: Anchors are generated for h1..h3 only — deeper headings rarely need links.
ANCHOR_MAX_LEVEL = 3

#: Directory skipped when measuring repository size.
GIT_METADATA_DIR = ".git"

DEFAULT_MIME_TYPE = "application/octet-stream"

#: Username used when a git token is supplied without one. Works for GitHub
#: personal access tokens and GitLab deploy tokens alike.
DEFAULT_GIT_USERNAME = "x-access-token"

#: What a redacted secret is replaced with in anything shown to a caller.
REDACTED = "***"

#: Schemes that are never inlined as data URIs.
EXTERNAL_URL_PREFIXES: tuple[str, ...] = (
    "http://",
    "https://",
    "data:",
    "//",
    "mailto:",
    "cid:",
)

HLJS_CSS_LIGHT = (
    "https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11"
    "/build/styles/github.min.css"
)
HLJS_CSS_DARK = (
    "https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11"
    "/build/styles/github-dark.min.css"
)
HLJS_JS = (
    "https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11/build/highlight.min.js"
)
MERMAID_ESM = "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs"

#: Placeholders substituted into PAGE_TEMPLATE (plain replace — the template
#: contains CSS/JS braces, so str.format() is not usable here).
_TITLE_SLOT = "{{ARTIFACT_TITLE}}"
_BODY_SLOT = "{{ARTIFACT_BODY}}"

# --------------------------------------------------------------------------
# Regular expressions
# --------------------------------------------------------------------------

_TITLE_TAG_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_H1_TAG_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_FRONT_MATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_FRONT_MATTER_TITLE_RE = re.compile(
    r"^[ \t]*title[ \t]*:[ \t]*(.+?)[ \t]*$", re.IGNORECASE | re.MULTILINE
)
_MD_H1_RE = re.compile(r"^[ \t]{0,3}#[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)
_IMG_SRC_RE = re.compile(
    r"(<img\b[^>]*?\bsrc\s*=\s*)(\"|')(.*?)\2", re.IGNORECASE | re.DOTALL
)
#: Strips ``user:password@`` from anything echoed back to the user.
_CREDENTIALS_RE = re.compile(r"//[^/@\s]*@")


class BuildError(Exception):
    """Raised when the supplied publish input cannot be built.

    The message is user-facing and is surfaced by the API as HTTP 422.
    """


@dataclass(frozen=True)
class BuiltArtifact:
    """Result of a build: the final HTML plus provenance metadata."""

    html: str
    title: str
    source_type: str  # "html" | "markdown" | "git-html" | "git-markdown"
    git_commit: str | None = None


# --------------------------------------------------------------------------
# Page template
# --------------------------------------------------------------------------

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ARTIFACT_TITLE}}</title>
<link rel="stylesheet" href="__HLJS_CSS_LIGHT__" media="(prefers-color-scheme: light)">
<link rel="stylesheet" href="__HLJS_CSS_DARK__" media="(prefers-color-scheme: dark)">
<style>
:root {
  --bg: #ffffff;
  --fg: #1f2328;
  --muted: #656d76;
  --border: #d0d7de;
  --subtle-bg: #f6f8fa;
  --accent: #0969da;
  --quote-border: #d0d7de;
  --table-header-bg: #f6f8fa;
  --table-hover-bg: #f0f3f6;
  --shadow: rgba(31, 35, 40, 0.08);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0d1117;
    --fg: #e6edf3;
    --muted: #9198a1;
    --border: #30363d;
    --subtle-bg: #161b22;
    --accent: #4493f8;
    --quote-border: #3d444d;
    --table-header-bg: #161b22;
    --table-hover-bg: #1c2128;
    --shadow: rgba(0, 0, 0, 0.4);
  }
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0;
  padding: 2.5rem 1.25rem 5rem;
  background: var(--bg);
  color: var(--fg);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue",
    Arial, "Noto Sans", sans-serif, "Apple Color Emoji", "Segoe UI Emoji";
  font-size: 17px;
  line-height: 1.7;
  -moz-osx-font-smoothing: grayscale;
  -webkit-font-smoothing: antialiased;
}
main {
  max-width: 52rem;
  margin: 0 auto;
}
h1, h2, h3, h4, h5, h6 {
  line-height: 1.25;
  margin: 2.2rem 0 1rem;
  font-weight: 600;
}
h1 { font-size: 2.1rem; letter-spacing: -0.02em; }
h2 {
  font-size: 1.55rem;
  padding-bottom: 0.3rem;
  border-bottom: 1px solid var(--border);
}
h3 { font-size: 1.25rem; }
h4 { font-size: 1.05rem; }
h5, h6 { font-size: 1rem; color: var(--muted); }
h1:first-child, h2:first-child, h3:first-child { margin-top: 0; }
p { margin: 0 0 1.1rem; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
h1 a.header-anchor, h2 a.header-anchor, h3 a.header-anchor {
  color: var(--muted);
  opacity: 0;
  font-weight: 400;
  text-decoration: none;
}
h1:hover a.header-anchor, h2:hover a.header-anchor, h3:hover a.header-anchor {
  opacity: 1;
}
ul, ol { padding-left: 1.6rem; margin: 0 0 1.1rem; }
li { margin: 0.25rem 0; }
li > p { margin-bottom: 0.4rem; }
hr {
  border: none;
  border-top: 1px solid var(--border);
  margin: 2.5rem 0;
}
blockquote {
  margin: 0 0 1.1rem;
  padding: 0.1rem 1.1rem;
  border-left: 4px solid var(--quote-border);
  color: var(--muted);
}
blockquote > :last-child { margin-bottom: 0; }
code, kbd, samp {
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas,
    "Liberation Mono", monospace;
  font-size: 0.88em;
}
:not(pre) > code {
  background: var(--subtle-bg);
  border: 1px solid var(--border);
  border-radius: 5px;
  padding: 0.12em 0.36em;
}
pre {
  background: var(--subtle-bg);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 0.9rem 1rem;
  margin: 0 0 1.3rem;
  overflow-x: auto;
  line-height: 1.55;
}
pre code {
  background: none;
  border: none;
  padding: 0;
  display: block;
}
pre.mermaid {
  background: none;
  border: none;
  padding: 0.5rem 0;
  text-align: center;
  overflow-x: auto;
}
table {
  border-collapse: collapse;
  width: 100%;
  margin: 0 0 1.4rem;
  display: block;
  overflow-x: auto;
  font-size: 0.95em;
}
th, td {
  border: 1px solid var(--border);
  padding: 0.5rem 0.75rem;
  text-align: left;
  vertical-align: top;
}
thead th { background: var(--table-header-bg); font-weight: 600; }
tbody tr:hover { background: var(--table-hover-bg); }
img { max-width: 100%; height: auto; }
figure { margin: 0 0 1.3rem; }
.contains-task-list, ul.contains-task-list { list-style: none; padding-left: 0.4rem; }
.task-list-item { list-style: none; }
.task-list-item input[type="checkbox"] {
  margin: 0 0.5rem 0 0;
  vertical-align: middle;
  width: 0.95em;
  height: 0.95em;
  accent-color: var(--accent);
}
.footnotes { font-size: 0.9em; color: var(--muted); }
</style>
</head>
<body>
<main>
{{ARTIFACT_BODY}}
</main>
<script src="__HLJS_JS__"></script>
<script>
  if (window.hljs) { hljs.highlightAll(); }
</script>
<script type="module">
import mermaid from "__MERMAID_ESM__";
mermaid.initialize({startOnLoad: true, theme: window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "default"});
</script>
</body>
</html>
"""

PAGE_TEMPLATE = (
    PAGE_TEMPLATE.replace("__HLJS_CSS_LIGHT__", HLJS_CSS_LIGHT)
    .replace("__HLJS_CSS_DARK__", HLJS_CSS_DARK)
    .replace("__HLJS_JS__", HLJS_JS)
    .replace("__MERMAID_ESM__", MERMAID_ESM)
)


# --------------------------------------------------------------------------
# Markdown rendering
# --------------------------------------------------------------------------


def _linkify_available() -> bool:
    """Whether markdown-it's optional ``linkify-it-py`` backend is installed.

    Enabling the ``linkify`` rule without it raises at render time, so the rule
    is only switched on when the package is importable. The absence is logged
    loudly rather than passing silently.
    """
    try:
        import linkify_it  # noqa: F401
    except ImportError:
        logger.warning(
            "linkify-it-py is not installed; bare URLs in Markdown will not be "
            "auto-linked. Install markdown-it-py[linkify] to enable it."
        )
        return False
    return True


def _make_markdown() -> MarkdownIt:
    """Build the configured MarkdownIt instance used for every render."""
    linkify = _linkify_available()
    md = MarkdownIt(
        "commonmark",
        {"html": True, "linkify": linkify, "typographer": True},
    ).enable(["table", "strikethrough"] + (["linkify"] if linkify else []))
    md.use(front_matter_plugin)
    md.use(tasklists_plugin, enabled=True)
    md.use(anchors_plugin, max_level=ANCHOR_MAX_LEVEL)
    _install_mermaid_fence(md)
    return md


def _install_mermaid_fence(md: MarkdownIt) -> None:
    """Render ```mermaid fences as ``<pre class="mermaid">`` blocks.

    Every other fence keeps markdown-it's default rendering so that
    highlight.js still sees ``<pre><code class="language-X">``.
    """
    default_fence: Callable[..., str] | None = md.renderer.rules.get("fence")

    def fence(tokens: Any, idx: int, options: Any, env: Any, *args: Any) -> str:
        token = tokens[idx]
        info = token.info.strip().split(maxsplit=1)[0].lower() if token.info else ""
        if info == "mermaid":
            return f'<pre class="mermaid">{html_lib.escape(token.content)}</pre>\n'
        if default_fence is not None:
            return default_fence(tokens, idx, options, env, *args)
        return md.renderer.renderToken(tokens, idx, options, env)

    md.renderer.rules["fence"] = fence


def _render_markdown_body(md_text: str) -> str:
    """Render Markdown source into an HTML fragment."""
    return _make_markdown().render(md_text)


def _render_page(body_html: str, title: str) -> str:
    """Wrap a rendered fragment in the full standalone HTML page."""
    return PAGE_TEMPLATE.replace(_TITLE_SLOT, html_lib.escape(title)).replace(
        _BODY_SLOT, body_html
    )


# --------------------------------------------------------------------------
# Title extraction
# --------------------------------------------------------------------------


def _clean(value: str | None) -> str | None:
    """Collapse whitespace; return None for empty results."""
    if value is None:
        return None
    collapsed = " ".join(value.split())
    return collapsed or None


def _strip_tags(value: str) -> str:
    return html_lib.unescape(_TAG_RE.sub("", value))


def _title_from_html(html: str) -> str | None:
    match = _TITLE_TAG_RE.search(html)
    if match:
        found = _clean(html_lib.unescape(match.group(1)))
        if found:
            return found
    match = _H1_TAG_RE.search(html)
    if match:
        return _clean(_strip_tags(match.group(1)))
    return None


def _front_matter_block(md_text: str) -> str | None:
    match = _FRONT_MATTER_RE.match(md_text)
    return match.group(1) if match else None


def _title_from_front_matter(md_text: str) -> str | None:
    """Pull ``title:`` out of a leading YAML front-matter block.

    Deliberately a minimal regex parse — the project does not carry a YAML
    dependency just to read one field.
    """
    block = _front_matter_block(md_text)
    if block is None:
        return None
    match = _FRONT_MATTER_TITLE_RE.search(block)
    if not match:
        return None
    raw = match.group(1).strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        raw = raw[1:-1]
    return _clean(raw)


def _title_from_markdown(md_text: str) -> str | None:
    front_matter = _title_from_front_matter(md_text)
    if front_matter:
        return front_matter
    body = md_text
    block = _front_matter_block(md_text)
    if block is not None:
        body = md_text[_FRONT_MATTER_RE.match(md_text).end() :]  # type: ignore[union-attr]
    match = _MD_H1_RE.search(body)
    if match:
        return _clean(_strip_tags(match.group(1)))
    return None


# --------------------------------------------------------------------------
# Public builders: html / markdown
# --------------------------------------------------------------------------


def build_from_html(html: str, title: str | None = None) -> BuiltArtifact:
    """Serve raw HTML as-is, deriving a title when none was supplied.

    Title precedence: explicit argument, ``<title>``, first ``<h1>``, then
    :data:`DEFAULT_TITLE`.
    """
    resolved = _clean(title) or _title_from_html(html) or DEFAULT_TITLE
    logger.debug("Built html artifact (title=%r, %d bytes)", resolved, len(html))
    return BuiltArtifact(html=html, title=resolved, source_type="html")


def build_from_markdown(md: str, title: str | None = None) -> BuiltArtifact:
    """Render Markdown into a full standalone HTML page.

    Title precedence: explicit argument, front-matter ``title:``, first
    ``# heading``, then :data:`DEFAULT_TITLE`.
    """
    resolved = _clean(title) or _title_from_markdown(md) or DEFAULT_TITLE
    body = _render_markdown_body(md)
    page = _render_page(body, resolved)
    logger.debug("Built markdown artifact (title=%r, %d bytes)", resolved, len(page))
    return BuiltArtifact(html=page, title=resolved, source_type="markdown")


# --------------------------------------------------------------------------
# Git helpers
# --------------------------------------------------------------------------


def _scrub(text: str, secret: str | None = None) -> str:
    """Remove any embedded credentials before echoing text back to a user.

    Two independent passes, deliberately overlapping:

    1. the ``//user:pass@host`` form is rewritten to ``//host`` — this catches
       credentials in any URL git echoes back, whatever the secret was;
    2. when ``secret`` is given, both its literal and its percent-encoded form
       are replaced with :data:`REDACTED`, so a token that leaked in some shape
       the regex does not model still never reaches the caller.
    """
    cleaned = _CREDENTIALS_RE.sub("//", text)
    if secret:
        for form in (secret, quote(secret, safe="")):
            cleaned = cleaned.replace(form, REDACTED)
    return cleaned


def _last_line(text: str, secret: str | None = None) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return _scrub(lines[-1], secret) if lines else ""


def _authed_clone_url(git_url: str, username: str | None, token: str) -> str:
    """Return ``git_url`` with transient clone credentials in its userinfo.

    The result is passed to ``git clone`` and to nothing else: it is never
    logged, stored, or included in an error message. Both parts are
    percent-encoded with ``safe=""`` so that a token containing ``@``, ``:``
    or ``/`` cannot break out of the userinfo section. Any userinfo already
    present in ``git_url`` is replaced.
    """
    parsed = urlparse(git_url)
    host = parsed.netloc.rsplit("@", 1)[-1]
    user = quote(username or DEFAULT_GIT_USERNAME, safe="")
    secret = quote(token, safe="")
    return urlunparse(parsed._replace(netloc=f"{user}:{secret}@{host}"))


def _validate_git_url(git_url: str) -> str:
    """Accept only https URLs with a hostname."""
    try:
        parsed = urlparse(git_url.strip())
    except ValueError as exc:  # pragma: no cover - urlparse rarely raises
        raise BuildError(f"Invalid git URL: {exc}") from exc
    if parsed.scheme != "https" or not parsed.hostname:
        raise BuildError(
            "git_url must be an https URL with a hostname, "
            "for example https://github.com/owner/repo.git"
        )
    return git_url.strip()


def _run_git(args: list[str], timeout_s: int) -> subprocess.CompletedProcess[str]:
    """Run a git command, capturing output; never raises on non-zero exit."""
    try:
        # GIT_TERMINAL_PROMPT=0 makes git fail immediately on private or
        # missing repositories instead of waiting for interactive credentials.
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        return subprocess.run(  # noqa: S603 - fixed argv, no shell
            args,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
            env=env,
        )
    except FileNotFoundError as exc:
        raise BuildError("git is not available on this server.") from exc
    except subprocess.TimeoutExpired as exc:
        raise BuildError(
            f"git operation timed out after {timeout_s}s. "
            "Try a smaller repository or a specific branch."
        ) from exc


def _clone(
    git_url: str,
    ref: str | None,
    dest: Path,
    timeout_s: int,
    *,
    username: str | None = None,
    token: str | None = None,
) -> None:
    """Shallow-clone ``git_url`` (optionally at ``ref``) into ``dest``.

    With ``token`` set, the clone runs against a credentialed variant of the
    URL (see :func:`_authed_clone_url`); ``git_url`` itself stays the
    unauthenticated URL used for every message raised from here.
    """
    clone_url = _authed_clone_url(git_url, username, token) if token else git_url
    args = ["git", "clone", "--depth", "1"]
    if ref:
        args += ["--branch", ref]
    args += [clone_url, str(dest)]
    result = _run_git(args, timeout_s)
    if result.returncode == 0:
        return
    reason = _last_line(result.stderr, token) or _last_line(result.stdout, token)
    lowered = (result.stderr or "").lower()
    if "could not read username" in lowered or "authentication failed" in lowered:
        hint = (
            "The supplied git_token was rejected, or git_username is wrong for "
            "this host."
            if token
            else "Pass git_token (and optionally git_username) to publish from "
            "a private repository."
        )
        raise BuildError(
            f"Could not clone the repository: it is private or does not exist. {hint}"
        )
    detail = f" git said: {reason}" if reason else ""
    if ref:
        raise BuildError(
            f"Could not clone the repository at ref {ref!r}. Only branch and tag "
            f"names are supported (not commit SHAs).{detail}"
        )
    raise BuildError(f"Could not clone the repository.{detail}")


def _repo_size_bytes(root: Path) -> int:
    """Sum file sizes under ``root``, excluding git metadata."""
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        if GIT_METADATA_DIR in dirnames:
            dirnames.remove(GIT_METADATA_DIR)
        for name in filenames:
            candidate = Path(dirpath) / name
            try:
                if candidate.is_symlink():
                    continue
                total += candidate.stat().st_size
            except OSError:  # pragma: no cover - broken entries are ignored
                continue
    return total


def _head_commit(root: Path, timeout_s: int, secret: str | None = None) -> str | None:
    result = _run_git(["git", "-C", str(root), "rev-parse", "HEAD"], timeout_s)
    if result.returncode != 0:
        # ``secret`` is passed through purely defensively: rev-parse never
        # touches the remote, but a cloned .git/config does hold the authed
        # remote URL, so nothing derived from git output is logged unscrubbed.
        logger.warning("rev-parse HEAD failed: %s", _last_line(result.stderr, secret))
        return None
    return result.stdout.strip() or None


def _is_inside(root: Path, candidate: Path) -> bool:
    """True when ``candidate`` really lives under ``root`` (symlinks resolved)."""
    root_real = os.path.realpath(root)
    candidate_real = os.path.realpath(candidate)
    return candidate_real == root_real or candidate_real.startswith(
        root_real + os.sep
    )


def _find_named(directory: Path, names: tuple[str, ...]) -> Path | None:
    """Case-insensitive lookup of the first matching file name."""
    try:
        entries = sorted(directory.iterdir(), key=lambda p: p.name)
    except OSError:
        return None
    lowered = {entry.name.lower(): entry for entry in entries if entry.is_file()}
    for name in names:
        found = lowered.get(name)
        if found is not None:
            return found
    return None


def _default_entry(directory: Path, where: str) -> Path:
    """Apply the default entry-file rules inside ``directory``."""
    found = _find_named(directory, INDEX_CANDIDATES)
    if found is not None:
        return found
    found = _find_named(directory, README_CANDIDATES)
    if found is not None:
        return found
    html_files = sorted(
        (
            entry
            for entry in directory.iterdir()
            if entry.is_file() and entry.suffix.lower() in HTML_SUFFIXES
        ),
        key=lambda p: p.name,
    )
    if len(html_files) == 1:
        return html_files[0]
    raise BuildError(
        f"No entry file found in {where}. Tried index.html, README.md, and a "
        "single *.html file"
        + (
            f" (found {len(html_files)} .html files — set git_path to pick one)."
            if html_files
            else ". Set git_path to point at the file to publish."
        )
    )


def _resolve_entry(repo_root: Path, path: str | None) -> Path:
    """Pick the file to build from, honouring an explicit ``git_path``."""
    if not path:
        return _default_entry(repo_root, "the repository root")

    cleaned = path.strip().lstrip("/")
    if not cleaned:
        return _default_entry(repo_root, "the repository root")

    candidate = repo_root / cleaned
    if not _is_inside(repo_root, candidate):
        raise BuildError(f"git_path {path!r} points outside the repository.")
    if not candidate.exists():
        raise BuildError(f"git_path {path!r} does not exist in the repository.")
    if candidate.is_dir():
        return _default_entry(candidate, f"directory {cleaned!r}")
    if candidate.suffix.lower() not in ALLOWED_ENTRY_SUFFIXES:
        raise BuildError(
            f"git_path {path!r} must point at an .html, .htm, .md or .markdown "
            "file."
        )
    return candidate


def _read_text(entry: Path) -> str:
    try:
        return entry.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise BuildError(f"Could not read {entry.name}: {exc.strerror}") from exc


# --------------------------------------------------------------------------
# Image inlining
# --------------------------------------------------------------------------


def _inline_images(
    html: str,
    base_dir: Path,
    repo_root: Path,
    max_image_bytes: int,
    max_total_bytes: int,
) -> str:
    """Replace relative ``<img src=...>`` references with ``data:`` URIs.

    References that are absolute URLs, missing, outside the repository, or that
    would breach a size cap are left untouched — inlining is best-effort and
    never fails the build.
    """
    budget = {"used": 0}

    def replace(match: re.Match[str]) -> str:
        prefix, quote, src = match.group(1), match.group(2), match.group(3)
        raw = src.strip()
        if not raw or raw.lower().startswith(EXTERNAL_URL_PREFIXES):
            return match.group(0)

        relative = unquote(raw.split("#", 1)[0].split("?", 1)[0])
        if not relative:
            return match.group(0)

        target = (
            (repo_root / relative.lstrip("/"))
            if relative.startswith("/")
            else (base_dir / relative)
        )
        if not _is_inside(repo_root, target):
            logger.debug("Skipping image outside repository: %r", relative)
            return match.group(0)
        if not target.is_file():
            return match.group(0)

        try:
            size = target.stat().st_size
        except OSError:
            return match.group(0)
        if size > max_image_bytes:
            logger.info("Image %r exceeds per-image cap, left as link", relative)
            return match.group(0)
        if budget["used"] + size > max_total_bytes:
            logger.info("Inline image budget exhausted, %r left as link", relative)
            return match.group(0)

        try:
            payload = target.read_bytes()
        except OSError:
            return match.group(0)

        mime = mimetypes.guess_type(target.name)[0] or DEFAULT_MIME_TYPE
        encoded = base64.b64encode(payload).decode("ascii")
        budget["used"] += size
        return f"{prefix}{quote}data:{mime};base64,{encoded}{quote}"

    return _IMG_SRC_RE.sub(replace, html)


# --------------------------------------------------------------------------
# Public builder: git
# --------------------------------------------------------------------------


def build_from_git(
    git_url: str,
    ref: str | None,
    path: str | None,
    title: str | None,
    settings: "Settings",
    *,
    git_username: str | None = None,
    git_token: str | None = None,
) -> BuiltArtifact:
    """Shallow-clone an https repository and build its entry document.

    Entry selection: explicit ``path`` (file or directory), otherwise
    ``index.html`` → ``README.md`` → a single root-level ``*.html``. Markdown
    entries go through the same rendering as :func:`build_from_markdown`; HTML
    entries are used verbatim. Relative images are inlined as ``data:`` URIs.

    ``git_token`` (with the optional ``git_username``, defaulting to
    :data:`DEFAULT_GIT_USERNAME`) makes private repositories reachable. It is
    used for the clone subprocess only: the unauthenticated ``git_url`` is what
    is logged and reported, and it is the caller's job never to persist the
    token.
    """
    url = _validate_git_url(git_url)

    with tempfile.TemporaryDirectory(prefix="artifact-git-") as tmp:
        dest = Path(tmp) / "repo"
        logger.info(
            "Cloning %s (ref=%r, authenticated=%s)", _scrub(url), ref, bool(git_token)
        )
        _clone(
            url,
            ref,
            dest,
            settings.git_clone_timeout_s,
            username=git_username,
            token=git_token,
        )

        size = _repo_size_bytes(dest)
        if size > settings.git_max_repo_bytes:
            raise BuildError(
                f"Repository is too large ({size // (1024 * 1024)} MB); the limit "
                f"is {settings.git_max_repo_bytes // (1024 * 1024)} MB."
            )

        commit = _head_commit(dest, settings.git_clone_timeout_s, git_token)
        entry = _resolve_entry(dest, path)
        suffix = entry.suffix.lower()
        source = _read_text(entry)

        if suffix in MARKDOWN_SUFFIXES:
            resolved = _clean(title) or _title_from_markdown(source) or DEFAULT_TITLE
            page = _render_page(_render_markdown_body(source), resolved)
            source_type = "git-markdown"
        else:
            resolved = _clean(title) or _title_from_html(source) or DEFAULT_TITLE
            page = source
            source_type = "git-html"

        page = _inline_images(
            page,
            base_dir=entry.parent,
            repo_root=dest,
            max_image_bytes=settings.max_inline_image_bytes,
            max_total_bytes=settings.max_inline_total_bytes,
        )

    encoded_size = len(page.encode("utf-8"))
    if encoded_size > settings.max_html_bytes:
        raise BuildError(
            f"Built HTML is too large ({encoded_size // (1024 * 1024)} MB); the "
            f"limit is {settings.max_html_bytes // (1024 * 1024)} MB."
        )

    logger.info(
        "Built %s artifact from %s (entry=%s, commit=%s, %d bytes)",
        source_type,
        _scrub(url),
        entry.name,
        commit,
        encoded_size,
    )
    return BuiltArtifact(
        html=page,
        title=resolved,
        source_type=source_type,
        git_commit=commit,
    )
