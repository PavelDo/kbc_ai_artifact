"""Exports of an artifact: single-file source and a ready-to-open Obsidian vault.

Two renderings live here, both **pure functions of their inputs**:

- :func:`head_source` — the served version's original Markdown (or the built
  HTML when the artifact was published as HTML), used by
  ``GET /a/{id}/export/markdown``.
- :func:`build_vault` — an in-memory ZIP holding an Obsidian vault
  (``INDEX.md`` hub, ``document.md``, ``versions/v{n}.md``,
  ``comments/{tid}.md``, ``reasoning.md``), used by
  ``GET /a/{id}/export/vault``.

Design constraints, in order of importance:

1. **Determinism.** The same ``(meta, envelopes, threads)`` must produce
   byte-identical ZIP bytes: entry order is fixed, entry timestamps are pinned
   to :data:`_ZIP_DATE`, and nothing here reads the clock, the environment or a
   random source. Everything written into the vault is reconstructible from the
   Storage records that were passed in — per CLAUDE.md, a vault must never
   carry state that cannot be rebuilt from Storage.
2. **No new dependencies.** ``zipfile`` + ``difflib`` + a tiny hand-rolled YAML
   string escaper (:func:`_yaml_value`) instead of PyYAML.
3. **Loose coupling to comments.** :class:`~src.comments.CommentThread` is
   imported for typing only; at runtime threads are read by attribute access so
   any object with the documented shape works.
"""

from __future__ import annotations

import difflib
import io
import unicodedata
import zipfile
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from src.diff import _comparable as _comparable_text
from src.store import HEAD_PINNED, STATUS_LIVE, ArtifactMeta, Envelope

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps runtime import-free
    from src.comments import CommentThread

__all__ = ["head_source", "build_vault"]

#: Fixed ZIP entry timestamp so repeated builds are byte-identical.
_ZIP_DATE = (2020, 1, 1, 0, 0, 0)
#: ``-rw-r--r--`` for a regular file, in the ZIP external-attribute encoding.
_ZIP_FILE_MODE = 0o100644 << 16
#: ZIP "create system" 3 == Unix, so the mode above is honoured on extraction.
_ZIP_UNIX = 3

#: Upper bound on a slug, in characters.
_SLUG_MAX_CHARS = 60
#: Slug used when neither the title nor the artifact ID yields anything usable.
_DEFAULT_SLUG = "artifact"

#: Longest quoted snippet shown in INDEX.md / reasoning.md, in characters.
_SNIPPET_MAX_CHARS = 60
#: Context lines of the per-version unified diff.
_DIFF_CONTEXT = 3
#: Sides larger than this are not diffed (the vault says so instead).
_DIFF_MAX_BYTES = 200_000

#: Artifact statuses (phase 3). ``final`` freezes versions and comments.
_STATUS_DRAFT = "draft"
_STATUS_FINAL = "final"
_ARTIFACT_STATUSES = (_STATUS_DRAFT, _STATUS_FINAL)

_MARKDOWN_CONTENT_TYPE = "text/markdown; charset=utf-8"
_HTML_CONTENT_TYPE = "text/html; charset=utf-8"

_YAML_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


# ---------------------------------------------------------------------------
# Small text helpers
# ---------------------------------------------------------------------------


def _slug_core(text: str) -> str:
    """Lowercase ASCII dash-slug of ``text``; "" when nothing survives."""
    normalized = unicodedata.normalize("NFKD", text or "")
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii").lower()
    chars = [ch if ch.isalnum() else "-" for ch in ascii_text]
    slug = "".join(chars)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")[:_SLUG_MAX_CHARS].strip("-")


def slugify(text: str, fallback: str = "") -> str:
    """Vault-safe folder/file name: lowercase, ASCII, dash-separated, capped.

    Falls back to ``fallback`` (usually the artifact ID) and then to
    :data:`_DEFAULT_SLUG`, so the result is never empty.
    """
    return _slug_core(text) or _slug_core(fallback) or _DEFAULT_SLUG


def _safe_name(text: str) -> str:
    """File-name-safe rendering of an identifier, preserving its case."""
    chars = [ch if (ch.isalnum() or ch in "-_") else "-" for ch in text or ""]
    name = "".join(chars).strip("-")
    while "--" in name:
        name = name.replace("--", "-")
    return name[:_SLUG_MAX_CHARS].strip("-")


def _yaml_value(value: Any) -> str:
    """One YAML scalar: booleans/ints/None bare, everything else quoted."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return _yaml_quote(str(value))


def _yaml_quote(text: str) -> str:
    """Double-quoted YAML string; every character that could break out escaped.

    Hand-rolled on purpose: the project takes no YAML dependency, and the only
    thing needed here is a totally safe double-quoted scalar.
    """
    out: list[str] = []
    for ch in text:
        escaped = _YAML_ESCAPES.get(ch)
        if escaped is not None:
            out.append(escaped)
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append("\\x%02x" % ord(ch))
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _frontmatter(pairs: list[tuple[str, Any]]) -> str:
    """A YAML frontmatter block (leading and trailing ``---``)."""
    lines = ["---"]
    for key, value in pairs:
        lines.append(f"{key}: {_yaml_value(value)}")
    lines.append("---")
    body = "\n".join(lines)
    return body + "\n"


def _fence(text: str, lang: str) -> str:
    """Fenced code block whose fence is always longer than any run inside."""
    longest = 0
    run = 0
    for ch in text:
        if ch == "`":
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    ticks = "`" * max(3, longest + 1)
    return ticks + lang + "\n" + text + "\n" + ticks


def _snippet(text: str, limit: int = _SNIPPET_MAX_CHARS) -> str:
    """Single-line, length-capped rendering of quoted user text."""
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def _blockquote(text: str) -> str:
    """Markdown blockquote of arbitrary (possibly multi-line) user text."""
    lines = (text or "").splitlines() or [""]
    quoted = [("> " + line).rstrip() for line in lines]
    return "\n".join(quoted)


def _iso_date(value: str) -> str:
    """``YYYY-MM-DD`` part of an ISO timestamp; the raw value when unparsable."""
    text = str(value or "")
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    return text


def _stack_host(stack_url: str) -> str:
    """Hostname of a stack URL — the only part of it a vault may contain."""
    if not stack_url:
        return ""
    return urlsplit(str(stack_url)).hostname or ""


def _identity(who: Any) -> dict:
    """Normalize a verified project identity dict (owner/author/replier)."""
    if not isinstance(who, dict):
        return {}
    return who


def _display_name(who: Any) -> str:
    """Human label for a project identity, never empty."""
    data = _identity(who)
    name = data.get("project_name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    project_id = data.get("project_id")
    if project_id is not None:
        return f"project {project_id}"
    key = data.get("key")
    if isinstance(key, str) and key.strip():
        return key.strip()
    return "unknown project"


# ---------------------------------------------------------------------------
# Head selection and single-file source export
# ---------------------------------------------------------------------------


def _markdown_of(envelope: Envelope) -> str | None:
    """The envelope's original Markdown, or None when it has none."""
    source = envelope.source if isinstance(envelope.source, dict) else {}
    markdown = source.get("markdown")
    return markdown if isinstance(markdown, str) else None


def _git_of(envelope: Envelope) -> dict:
    """Git provenance recorded on the envelope, or ``{}``."""
    if not str(envelope.source_type or "").startswith("git"):
        return {}
    source = envelope.source if isinstance(envelope.source, dict) else {}
    git = source.get("git")
    return git if isinstance(git, dict) else {}


def head_source(envelope: Envelope) -> tuple[str, str, str]:
    """Downloadable source of one version: ``(filename, content_type, body)``.

    Markdown-authored artifacts export their original Markdown; everything else
    exports the built HTML document. The filename is derived from the version's
    title so a browser "Save as" lands on something readable.
    """
    stem = slugify(envelope.title, fallback=envelope.id)
    markdown = _markdown_of(envelope)
    if markdown is not None:
        return f"{stem}.md", _MARKDOWN_CONTENT_TYPE, markdown
    return f"{stem}.html", _HTML_CONTENT_TYPE, envelope.html or ""


def _pick_head(meta: ArtifactMeta, ordered: list[Envelope]) -> Envelope | None:
    """The version ``/a/{id}`` serves: the pinned live one, else the newest live.

    Mirrors :meth:`src.store.ArtifactStore.get_head` over an already-loaded list
    so the vault labels exactly the version a reader gets.
    """
    live = [env for env in ordered if env.status == STATUS_LIVE]
    if getattr(meta, "head_mode", None) == HEAD_PINNED:
        pinned = getattr(meta, "head_version", None)
        if isinstance(pinned, int) and not isinstance(pinned, bool):
            for env in live:
                if env.version == pinned:
                    return env
    return live[-1] if live else None


def _artifact_status(meta: ArtifactMeta) -> str:
    """``draft`` / ``final``. Tolerates a meta record that predates the field."""
    status = getattr(meta, "status", None)
    return status if status in _ARTIFACT_STATUSES else _STATUS_DRAFT


# ---------------------------------------------------------------------------
# Vault
# ---------------------------------------------------------------------------


def _thread_names(threads: list["CommentThread"]) -> list[tuple[Any, str]]:
    """Threads in chronological order, each paired with its vault file stem.

    Names are made file-safe and de-duplicated deterministically, because the
    wikilinks in INDEX.md and reasoning.md must resolve to real files.
    """
    ordered = sorted(
        threads or [],
        key=lambda thread: (str(getattr(thread, "created_at", "")), str(getattr(thread, "id", ""))),
    )
    used: set[str] = set()
    pairs: list[tuple[Any, str]] = []
    for position, thread in enumerate(ordered):
        name = _safe_name(str(getattr(thread, "id", ""))) or f"thread-{position + 1}"
        if name in used:
            name = f"{name}-{position + 1}"
        used.add(name)
        pairs.append((thread, name))
    return pairs


def _version_link(env: Envelope) -> str:
    """Wikilink to a version note, labelled ``v{n} — title (author, date)``."""
    label = (
        f"v{env.version} — {_snippet(env.title) or 'untitled'} "
        f"({_display_name(env.author)}, {_iso_date(env.created_at)})"
    )
    return f"[[versions/v{env.version}|{label}]]"


def _render_index(
    meta: ArtifactMeta,
    ordered: list[Envelope],
    head: Envelope | None,
    thread_pairs: list[tuple[Any, str]],
    html_document: bool,
) -> str:
    live = [env for env in ordered if env.status == STATUS_LIVE]
    open_threads = [
        pair for pair in thread_pairs if not bool(getattr(pair[0], "resolved", False))
    ]
    title = head.title if head is not None else ""
    owner = _identity(getattr(meta, "owner", None))

    parts: list[str] = [
        _frontmatter(
            [
                ("artifact_id", meta.id),
                ("title", title),
                ("status", _artifact_status(meta)),
                ("owner_project", _display_name(owner)),
                ("owner_stack", _stack_host(str(owner.get("stack_url") or ""))),
                ("created_at", getattr(meta, "created_at", "") or ""),
                ("updated_at", getattr(meta, "updated_at", "") or ""),
                ("versions", len(ordered)),
                ("live_versions", len(live)),
                ("proposed_versions", len(ordered) - len(live)),
                ("comments", len(thread_pairs)),
                ("open_comments", len(open_threads)),
            ]
        )
    ]

    heading = _snippet(title, _SLUG_MAX_CHARS) or meta.id
    parts.append(f"\n# {heading}\n")

    if head is not None:
        served = (
            f"The served document is [[document]] "
            f"(v{head.version}, by {_display_name(head.author)}, "
            f"{_iso_date(head.created_at)})."
        )
    else:
        served = "This artifact currently has no live version; [[document]] says so."
    parts.append(served + "\n")
    if html_document:
        parts.append(
            "\nThe document was published as raw HTML; the HTML itself is in "
            "`document.html`.\n"
        )

    parts.append("\n## Versions\n\n")
    if ordered:
        rows = []
        for env in reversed(ordered):
            marks = [env.status]
            if head is not None and env.version == head.version:
                marks.append("served")
            marks_text = " · ".join(marks)
            rows.append(f"- {_version_link(env)} — {marks_text}")
        parts.append("\n".join(rows) + "\n")
    else:
        parts.append("_No versions._\n")

    parts.append("\n## Discussion\n\n")
    if thread_pairs:
        rows = []
        for thread, name in thread_pairs:
            state = "resolved" if getattr(thread, "resolved", False) else "open"
            quote = _snippet(getattr(getattr(thread, "selector", None), "exact", ""))
            version = getattr(thread, "version", None)
            anchor = f"[[versions/v{version}]]" if version is not None else "an unknown version"
            rows.append(f'- [[comments/{name}]] — {state} · on {anchor} — "{quote}"')
        parts.append("\n".join(rows) + "\n")
    else:
        parts.append("_No comments._\n")

    parts.append("\n## Reasoning\n\n[[reasoning]] — how this document got here.\n")
    return "".join(parts)


def _render_document(head: Envelope | None) -> tuple[str, str | None]:
    """``(document.md body, document.html body or None)``."""
    if head is None:
        return (
            "# Document\n\n"
            "This artifact has no live version, so there is nothing to serve.\n"
        ), None

    markdown = _markdown_of(head)
    if markdown is not None:
        return markdown, None

    body = (
        f"# {_snippet(head.title, _SLUG_MAX_CHARS) or 'Document'}\n\n"
        f"This artifact was published as raw HTML (source type "
        f"`{head.source_type}`), so there is no Markdown source to show here. "
        f"The served document (version {head.version}) is stored next to this "
        f"note as `document.html`.\n"
    )
    return body, (head.html or "")


def _render_diff(previous: Envelope, current: Envelope) -> str:
    """A ```diff``` fenced unified diff of ``previous`` → ``current``."""
    kind, a_text, b_text = _comparable_text(previous, current)
    a_bytes = len(a_text.encode("utf-8"))
    b_bytes = len(b_text.encode("utf-8"))
    if max(a_bytes, b_bytes) > _DIFF_MAX_BYTES:
        return (
            f"Diff omitted: the compared {kind} is larger than "
            f"{_DIFF_MAX_BYTES} bytes "
            f"(v{previous.version}: {a_bytes} B, v{current.version}: {b_bytes} B).\n"
        )

    lines = list(
        difflib.unified_diff(
            a_text.splitlines(),
            b_text.splitlines(),
            fromfile=f"v{previous.version}",
            tofile=f"v{current.version}",
            n=_DIFF_CONTEXT,
            lineterm="",
        )
    )
    if not lines:
        return f"No changes in the compared {kind}.\n"
    joined = "\n".join(lines)
    return f"Comparing {kind}.\n\n" + _fence(joined, "diff") + "\n"


def _render_version(env: Envelope, previous: Envelope | None) -> tuple[str, str | None]:
    """``(versions/v{n}.md body, versions/v{n}.html body or None)``."""
    author = _identity(env.author)
    pairs: list[tuple[str, Any]] = [
        ("version", env.version),
        ("status", env.status),
        ("author_project", _display_name(author)),
        ("author_project_id", author.get("project_id")),
        ("author_stack", _stack_host(str(author.get("stack_url") or ""))),
        ("created_at", env.created_at or ""),
        ("note", env.note),
        ("source_type", env.source_type),
    ]
    git = _git_of(env)
    for key in sorted(git):
        value = git[key]
        if value is None or isinstance(value, (str, int, bool)):
            pairs.append((f"git_{_safe_name(str(key)).replace('-', '_') or 'field'}", value))

    parts: list[str] = [_frontmatter(pairs)]
    title = _snippet(env.title, _SLUG_MAX_CHARS) or "untitled"
    parts.append(f"\n# v{env.version} — {title}\n\n")

    markdown = _markdown_of(env)
    html_body: str | None = None
    if markdown is not None:
        parts.append(markdown.rstrip("\n") + "\n")
    else:
        html_body = env.html or ""
        parts.append(
            f"This version carries raw HTML (source type `{env.source_type}`) "
            f"rather than Markdown; the document itself is stored alongside as "
            f"`v{env.version}.html`.\n"
        )

    parts.append("\n## Changes vs previous version\n\n")
    if previous is None:
        parts.append("First version — nothing to compare against.\n")
    else:
        parts.append(_render_diff(previous, env))

    return "".join(parts), html_body


def _render_comment(thread: Any) -> str:
    author = _identity(getattr(thread, "author", None))
    resolved = bool(getattr(thread, "resolved", False))
    resolved_by = getattr(thread, "resolved_by", None)
    version = getattr(thread, "version", None)
    selector = getattr(thread, "selector", None)

    pairs: list[tuple[str, Any]] = [
        ("thread_id", str(getattr(thread, "id", ""))),
        ("artifact_id", str(getattr(thread, "artifact_id", ""))),
        ("version", version if isinstance(version, int) else None),
        ("author_project", _display_name(author)),
        ("author_stack", _stack_host(str(author.get("stack_url") or ""))),
        ("created_at", str(getattr(thread, "created_at", "") or "")),
        ("resolved", resolved),
    ]
    if resolved_by:
        pairs.append(("resolved_by", _display_name(resolved_by)))

    parts: list[str] = [_frontmatter(pairs)]
    parts.append("\n# Comment\n\n")
    if version is not None:
        parts.append(f"On [[versions/v{version}]].\n\n")
    parts.append("## Quoted text\n\n")
    parts.append(_blockquote(str(getattr(selector, "exact", "") or "")) + "\n")

    parts.append("\n## Thread\n\n")
    created = _iso_date(str(getattr(thread, "created_at", "") or ""))
    body = str(getattr(thread, "body", "") or "")
    parts.append(f"**{_display_name(author)} ({created}):** {body}\n")

    replies = list(getattr(thread, "replies", None) or [])
    for reply in replies:
        reply_author = _display_name(getattr(reply, "author", None))
        reply_date = _iso_date(str(getattr(reply, "created_at", "") or ""))
        reply_body = str(getattr(reply, "body", "") or "")
        parts.append(f"\n**{reply_author} ({reply_date}):** {reply_body}\n")

    parts.append("\n## Resolution\n\n")
    if resolved:
        who = _display_name(resolved_by) if resolved_by else "unknown"
        parts.append(f"Resolved by {who}.\n")
    else:
        parts.append("Open.\n")
    return "".join(parts)


def _render_reasoning(
    meta: ArtifactMeta,
    ordered: list[Envelope],
    head: Envelope | None,
    thread_pairs: list[tuple[Any, str]],
) -> str:
    """Chronological "how this document got here" trail.

    Events are merged on ``created_at`` alone — every timestamp comes from the
    Storage records, so the timeline is reproducible from Storage forever.
    """
    # (timestamp, kind order, tie-breaker, bullet)
    events: list[tuple[str, int, str, str]] = []

    for env in ordered:
        verb = "published" if env.status == STATUS_LIVE else "proposed"
        bullet = (
            f"{_iso_date(env.created_at)} — [[versions/v{env.version}|v{env.version}]] "
            f"{verb} by {_display_name(env.author)}"
        )
        if env.note:
            bullet += f" — note: {_snippet(env.note, _SNIPPET_MAX_CHARS * 2)}"
        events.append((str(env.created_at or ""), 0, f"{env.version:09d}", bullet))

    for thread, name in thread_pairs:
        quote = _snippet(getattr(getattr(thread, "selector", None), "exact", ""))
        version = getattr(thread, "version", None)
        anchor = f"v{version}" if version is not None else "an unknown version"
        state = "resolved" if getattr(thread, "resolved", False) else "open"
        replies = list(getattr(thread, "replies", None) or [])
        created = str(getattr(thread, "created_at", "") or "")
        bullet = (
            f"{_iso_date(created)} — [[comments/{name}|comment]] opened by "
            f"{_display_name(getattr(thread, 'author', None))} on {anchor}: "
            f'"{quote}" — {state}'
        )
        if replies:
            bullet += f", {len(replies)} repl{'y' if len(replies) == 1 else 'ies'}"
        events.append((created, 1, name, bullet))

    events.sort(key=lambda event: (event[0], event[1], event[2]))

    parts: list[str] = [
        _frontmatter([("artifact_id", meta.id), ("note_kind", "reasoning")]),
        "\n# How this document got here\n\n",
    ]
    if events:
        parts.append("\n".join(f"- {event[3]}" for event in events) + "\n")
    else:
        parts.append("_Nothing has happened yet._\n")

    parts.append("\n## Current status\n\n")
    status = _artifact_status(meta)
    state_text = (
        "Final — new versions and comments are frozen."
        if status == _STATUS_FINAL
        else "Draft — open for new versions and comments."
    )
    parts.append(state_text + "\n\n")
    if head is not None:
        parts.append(
            f"The served version is [[versions/v{head.version}|v{head.version}]] "
            f"by {_display_name(head.author)} ({_iso_date(head.created_at)}).\n"
        )
    else:
        parts.append("No version is currently served.\n")
    return "".join(parts)


def _add(archive: zipfile.ZipFile, path: str, text: str) -> None:
    """Write one UTF-8 text entry with pinned metadata (reproducible ZIPs)."""
    info = zipfile.ZipInfo(path, date_time=_ZIP_DATE)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = _ZIP_UNIX
    info.external_attr = _ZIP_FILE_MODE
    archive.writestr(info, text.encode("utf-8"))


def build_vault(
    meta: ArtifactMeta,
    envelopes: list[Envelope],
    threads: list["CommentThread"],
) -> tuple[str, bytes]:
    """Build an Obsidian vault ZIP for one artifact: ``(filename, zip_bytes)``.

    Everything is derived from the three arguments, so two builds from the same
    inputs produce byte-identical archives.
    """
    ordered = sorted(envelopes or [], key=lambda env: env.version)
    head = _pick_head(meta, ordered)
    root = slugify(head.title if head is not None else "", fallback=meta.id)
    thread_pairs = _thread_names(list(threads or []))

    document_md, document_html = _render_document(head)
    index_md = _render_index(
        meta, ordered, head, thread_pairs, html_document=document_html is not None
    )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        _add(archive, f"{root}/INDEX.md", index_md)
        _add(archive, f"{root}/document.md", document_md)
        if document_html is not None:
            _add(archive, f"{root}/document.html", document_html)

        previous: Envelope | None = None
        for env in ordered:
            version_md, version_html = _render_version(env, previous)
            _add(archive, f"{root}/versions/v{env.version}.md", version_md)
            if version_html is not None:
                _add(archive, f"{root}/versions/v{env.version}.html", version_html)
            previous = env

        for thread, name in thread_pairs:
            _add(archive, f"{root}/comments/{name}.md", _render_comment(thread))

        _add(archive, f"{root}/reasoning.md", _render_reasoning(meta, ordered, head, thread_pairs))

    return f"{root}-vault.zip", buffer.getvalue()
