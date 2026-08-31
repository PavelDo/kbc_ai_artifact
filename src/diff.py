"""Version diffing for artifacts.

Two versions of an artifact are compared over their most meaningful shared
representation: the original Markdown when both versions carry it, otherwise
the built HTML. Three renderings are produced, all with the standard library
only (``difflib``) — no new dependencies, no external assets:

- ``"unified"`` — a classic unified diff (``text/plain``) for machines and CLIs,
- ``"json"`` — the unified diff plus add/remove counts (``application/json``),
- ``"html"`` — a self-contained side-by-side page (``text/html``) for humans.

Both sides are size-guarded: comparing multi-megabyte documents line by line is
quadratic in the worst case, so anything above ``max_bytes`` is refused with
:class:`DiffError` (the API maps that to 413).
"""

from __future__ import annotations

import difflib
import html
import json

from src.store import Envelope

__all__ = ["DiffError", "compute_diff"]

#: Rendering formats accepted by :func:`compute_diff`.
FORMATS = ("html", "unified", "json")

#: Unified-diff context lines.
_UNIFIED_CONTEXT = 3
#: Unchanged runs longer than this are collapsed in the side-by-side page.
_COLLAPSE_MIN_LINES = 8
#: Unchanged lines kept visible at each end of a collapsed run.
_COLLAPSE_CONTEXT = 3

_CONTENT_TYPES = {
    "html": "text/html; charset=utf-8",
    "unified": "text/plain; charset=utf-8",
    "json": "application/json",
}


class DiffError(Exception):
    """Raised when two versions cannot be diffed (unknown format, too large)."""


def _comparable(a: Envelope, b: Envelope) -> tuple[str, str, str]:
    """Pick the text to compare: Markdown when both sides have it, else HTML.

    Returns ``(kind, a_text, b_text)`` where ``kind`` is ``"markdown"`` or
    ``"html"``.
    """
    a_md = a.source.get("markdown") if isinstance(a.source, dict) else None
    b_md = b.source.get("markdown") if isinstance(b.source, dict) else None
    if isinstance(a_md, str) and isinstance(b_md, str):
        return "markdown", a_md, b_md
    return "html", a.html or "", b.html or ""


def _stats(a_lines: list[str], b_lines: list[str]) -> tuple[int, int]:
    """(added, removed) line counts between two line lists."""
    added = removed = 0
    matcher = difflib.SequenceMatcher(None, a_lines, b_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "replace":
            removed += i2 - i1
            added += j2 - j1
        elif tag == "delete":
            removed += i2 - i1
        elif tag == "insert":
            added += j2 - j1
    return added, removed


def _unified(a: Envelope, b: Envelope, a_lines: list[str], b_lines: list[str]) -> str:
    return "\n".join(
        difflib.unified_diff(
            a_lines,
            b_lines,
            fromfile=f"v{a.version}",
            tofile=f"v{b.version}",
            n=_UNIFIED_CONTEXT,
            lineterm="",
        )
    )


def compute_diff(
    a: Envelope, b: Envelope, fmt: str, max_bytes: int
) -> tuple[str, str]:
    """Diff two version envelopes.

    Returns ``(content_type, body)``. ``a`` is the older ("from") side, ``b``
    the newer ("to") side. Raises :class:`DiffError` for an unknown format or
    when either side exceeds ``max_bytes``.
    """
    if fmt not in FORMATS:
        raise DiffError(f"Unknown diff format: {fmt!r}")

    kind, a_text, b_text = _comparable(a, b)
    for label, text in (("v%d" % a.version, a_text), ("v%d" % b.version, b_text)):
        if len(text.encode("utf-8")) > max_bytes:
            raise DiffError(
                f"{label} is too large to diff "
                f"({len(text.encode('utf-8'))} bytes > {max_bytes})"
            )

    a_lines = a_text.splitlines()
    b_lines = b_text.splitlines()

    if fmt == "unified":
        return _CONTENT_TYPES["unified"], _unified(a, b, a_lines, b_lines)

    added, removed = _stats(a_lines, b_lines)

    if fmt == "json":
        payload = {
            "from": a.version,
            "to": b.version,
            "kind": kind,
            "stats": {"added": added, "removed": removed},
            "unified": _unified(a, b, a_lines, b_lines),
        }
        return _CONTENT_TYPES["json"], json.dumps(payload, ensure_ascii=False)

    body = _render_html(a, b, kind, a_lines, b_lines, added, removed)
    return _CONTENT_TYPES["html"], body


# ---------------------------------------------------------------------------
# Side-by-side HTML
# ---------------------------------------------------------------------------


def _row(
    css_class: str,
    left_no: int | None,
    left: str | None,
    right_no: int | None,
    right: str | None,
) -> str:
    """One two-column table row; both texts are escaped here, never earlier."""
    return (
        f'<tr class="{css_class}">'
        f'<td class="num">{left_no if left_no is not None else ""}</td>'
        f'<td class="code">{html.escape(left) if left is not None else ""}</td>'
        f'<td class="num">{right_no if right_no is not None else ""}</td>'
        f'<td class="code">{html.escape(right) if right is not None else ""}</td>'
        "</tr>"
    )


def _collapse_row(count: int) -> str:
    return (
        '<tr class="skip"><td class="num"></td>'
        f'<td class="code" colspan="3">… {count} unchanged lines …</td></tr>'
    )


def _equal_rows(
    a_lines: list[str], b_lines: list[str], i1: int, i2: int, j1: int, j2: int
) -> list[str]:
    """Rows for an unchanged run, collapsing the middle of long runs."""
    length = i2 - i1
    if length <= _COLLAPSE_MIN_LINES:
        return [
            _row("equal", i1 + k + 1, a_lines[i1 + k], j1 + k + 1, b_lines[j1 + k])
            for k in range(length)
        ]
    rows: list[str] = []
    for k in range(_COLLAPSE_CONTEXT):
        rows.append(
            _row("equal", i1 + k + 1, a_lines[i1 + k], j1 + k + 1, b_lines[j1 + k])
        )
    rows.append(_collapse_row(length - 2 * _COLLAPSE_CONTEXT))
    for k in range(length - _COLLAPSE_CONTEXT, length):
        rows.append(
            _row("equal", i1 + k + 1, a_lines[i1 + k], j1 + k + 1, b_lines[j1 + k])
        )
    return rows


def _diff_rows(a_lines: list[str], b_lines: list[str]) -> list[str]:
    matcher = difflib.SequenceMatcher(None, a_lines, b_lines, autojunk=False)
    rows: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            rows.extend(_equal_rows(a_lines, b_lines, i1, i2, j1, j2))
        elif tag == "replace":
            paired = min(i2 - i1, j2 - j1)
            for k in range(paired):
                rows.append(
                    _row(
                        "replace",
                        i1 + k + 1,
                        a_lines[i1 + k],
                        j1 + k + 1,
                        b_lines[j1 + k],
                    )
                )
            for i in range(i1 + paired, i2):
                rows.append(_row("delete", i + 1, a_lines[i], None, None))
            for j in range(j1 + paired, j2):
                rows.append(_row("insert", None, None, j + 1, b_lines[j]))
        elif tag == "delete":
            for i in range(i1, i2):
                rows.append(_row("delete", i + 1, a_lines[i], None, None))
        elif tag == "insert":
            for j in range(j1, j2):
                rows.append(_row("insert", None, None, j + 1, b_lines[j]))
    if not rows:
        rows.append(_collapse_row(0))
    return rows


_CSS = """
:root {
  color-scheme: light dark;
  --bg: #ffffff; --fg: #1b1f23; --muted: #6a737d; --border: #e1e4e8;
  --head-bg: #f6f8fa; --num: #8b949e;
  --ins: #e6ffec; --del: #ffebe9; --chg: #fff8c5; --skip: #f6f8fa;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0d1117; --fg: #e6edf3; --muted: #9198a1; --border: #30363d;
    --head-bg: #161b22; --num: #6e7681;
    --ins: #12261e; --del: #2d1214; --chg: #2b2412; --skip: #161b22;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
header { position: sticky; top: 0; z-index: 2; background: var(--head-bg);
  border-bottom: 1px solid var(--border); padding: 12px 16px;
  display: flex; flex-wrap: wrap; gap: 12px; align-items: baseline; }
header h1 { font-size: 15px; margin: 0; font-weight: 600; }
header .meta { color: var(--muted); font-size: 13px; }
header .added { color: #1a7f37; font-weight: 600; }
header .removed { color: #cf222e; font-weight: 600; }
@media (prefers-color-scheme: dark) {
  header .added { color: #3fb950; } header .removed { color: #f85149; }
}
.wrap { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; table-layout: fixed;
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  font-size: 12.5px; }
col.num-col { width: 52px; }
td { vertical-align: top; padding: 1px 8px; border: 0; }
td.num { color: var(--num); text-align: right; user-select: none;
  border-right: 1px solid var(--border); white-space: nowrap; }
td.code { white-space: pre-wrap; word-break: break-word; }
tr.insert td:nth-child(3), tr.insert td:nth-child(4) { background: var(--ins); }
tr.delete td:nth-child(1), tr.delete td:nth-child(2) { background: var(--del); }
tr.replace td { background: var(--chg); }
tr.skip td { background: var(--skip); color: var(--muted);
  text-align: center; font-style: italic; }
"""


def _render_html(
    a: Envelope,
    b: Envelope,
    kind: str,
    a_lines: list[str],
    b_lines: list[str],
    added: int,
    removed: int,
) -> str:
    rows = "\n".join(_diff_rows(a_lines, b_lines))
    title = html.escape(f"artifact {a.id}: v{a.version} vs v{b.version}")
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="robots" content="noindex">'
        f"<title>{title}</title><style>{_CSS}</style></head><body>"
        f"<header><h1>{title}</h1>"
        f'<span class="meta">comparing {html.escape(kind)} · '
        f'<span class="added">+{added}</span> '
        f'<span class="removed">-{removed}</span></span></header>'
        '<div class="wrap"><table>'
        '<colgroup><col class="num-col"><col><col class="num-col"><col></colgroup>'
        f"<tbody>{rows}</tbody></table></div></body></html>"
    )
