"""Tests for :mod:`src.export` — Markdown source export and Obsidian vault ZIP.

The comment dataclasses live in ``src.comments`` (built in parallel with this
module). When that module is not importable yet, a stub with the *frozen*
contract shape is registered under ``sys.modules["src.comments"]`` — inside the
test process only. ``src.export`` never imports it at runtime (TYPE_CHECKING +
attribute access), so production code is unaffected either way.
"""

import io
import re
import sys
import zipfile

import pytest

from src.export import build_vault, head_source, markdown_of_html, slugify
from src.store import STATUS_LIVE, STATUS_PROPOSED, ArtifactMeta, Envelope

try:  # pragma: no cover - exercised implicitly by whichever branch applies
    from src.comments import CommentThread, Reply, Selector
except ImportError:  # pragma: no cover
    import types
    from dataclasses import dataclass, field

    @dataclass
    class Selector:  # type: ignore[no-redef]
        exact: str = ""
        prefix: str = ""
        suffix: str = ""

    @dataclass
    class Reply:  # type: ignore[no-redef]
        author: dict = field(default_factory=dict)
        body: str = ""
        created_at: str = ""

    @dataclass
    class CommentThread:  # type: ignore[no-redef]
        id: str = ""
        artifact_id: str = ""
        version: int = 1
        selector: Selector = field(default_factory=Selector)
        body: str = ""
        author: dict = field(default_factory=dict)
        created_at: str = ""
        resolved: bool = False
        resolved_by: dict | None = None
        replies: list = field(default_factory=list)

    _stub = types.ModuleType("src.comments")
    _stub.CommentThread = CommentThread
    _stub.Reply = Reply
    _stub.Selector = Selector
    sys.modules.setdefault("src.comments", _stub)


ARTIFACT_ID = "AbC123xyz"
TITLE = "Příliš: žluťoučký kůň"
ROOT = "prilis-zlutoucky-kun"

OWNER = {
    "stack_url": "https://connection.keboola.com",
    "project_id": 111,
    "project_name": "Acme Analytics",
    "key": "111@connection.keboola.com",
}
CONTRIBUTOR = {
    "stack_url": "https://connection.north-europe.azure.keboola.com",
    "project_id": 222,
    "project_name": "Beta Corp",
    "key": "222@connection.north-europe.azure.keboola.com",
}

V1_MD = "# Intro\n\nFirst draft of the plan.\n"
V2_MD = "# Intro\n\nSecond draft of the plan, tightened.\n\n- one\n- two\n"
V3_HTML = "<!doctype html><html><body><h1>Proposal</h1></body></html>"


def _envelope(
    version: int,
    *,
    title: str = TITLE,
    markdown: str | None = None,
    html: str = "",
    source_type: str = "markdown",
    author: dict | None = None,
    status: str = STATUS_LIVE,
    note: str | None = None,
    created_at: str = "",
) -> Envelope:
    source = {"markdown": markdown} if markdown is not None else {}
    return Envelope(
        id=ARTIFACT_ID,
        version=version,
        title=title,
        html=html or f"<html><body>v{version}</body></html>",
        source_type=source_type,
        source=source,
        author=author if author is not None else OWNER,
        status=status,
        note=note,
        created_at=created_at,
    )


@pytest.fixture
def meta() -> ArtifactMeta:
    return ArtifactMeta(
        id=ARTIFACT_ID,
        owner=dict(OWNER),
        password=None,
        created_at="2026-01-01T08:00:00+00:00",
        updated_at="2026-01-05T10:00:00+00:00",
    )


@pytest.fixture
def envelopes() -> list[Envelope]:
    return [
        _envelope(1, markdown=V1_MD, created_at="2026-01-01T10:00:00+00:00"),
        _envelope(
            2,
            markdown=V2_MD,
            note="Tightened the intro",
            created_at="2026-01-03T10:00:00+00:00",
        ),
        _envelope(
            3,
            title="Proposal from Beta",
            html=V3_HTML,
            source_type="html",
            author=CONTRIBUTOR,
            status=STATUS_PROPOSED,
            note="Suggested rewrite",
            created_at="2026-01-05T10:00:00+00:00",
        ),
    ]


@pytest.fixture
def threads() -> list:
    resolved = CommentThread(
        id="th-one",
        artifact_id=ARTIFACT_ID,
        version=1,
        selector=Selector(exact="First draft", prefix="# Intro\n\n", suffix=" of the"),
        body="Is this the final wording?",
        author=dict(CONTRIBUTOR),
        created_at="2026-01-02T09:00:00+00:00",
        resolved=True,
        resolved_by=dict(OWNER),
        replies=[
            Reply(
                author=dict(OWNER),
                body="No, v2 rewrites it.",
                created_at="2026-01-02T11:00:00+00:00",
            ),
            Reply(
                author=dict(CONTRIBUTOR),
                body="Great, thanks.",
                created_at="2026-01-02T12:00:00+00:00",
            ),
        ],
    )
    open_thread = CommentThread(
        id="th-two",
        artifact_id=ARTIFACT_ID,
        version=2,
        selector=Selector(exact="tightened", prefix="plan, ", suffix=".\n"),
        body="Can we add a third bullet?",
        author=dict(CONTRIBUTOR),
        created_at="2026-01-04T09:00:00+00:00",
        resolved=False,
        resolved_by=None,
        replies=[],
    )
    return [resolved, open_thread]


def _open(meta: ArtifactMeta, envelopes: list[Envelope], threads: list):
    name, blob = build_vault(meta, envelopes, threads)
    return name, blob, zipfile.ZipFile(io.BytesIO(blob))


def _read(archive: zipfile.ZipFile, path: str) -> str:
    return archive.read(path).decode("utf-8")


def _frontmatter_lines(text: str) -> list[str]:
    assert text.startswith("---\n")
    end = text.index("\n---\n", 3)
    return text[4:end].split("\n")


# --------------------------------------------------------------- head_source


def test_head_source_markdown_branch_is_byte_identical_to_the_original():
    """The author's own Markdown is never touched — not even re-wrapped."""
    env = _envelope(2, markdown=V2_MD)
    filename, content_type, body, kind = head_source(env)
    assert filename == f"{ROOT}.md"
    assert content_type == "text/markdown; charset=utf-8"
    assert body == V2_MD
    assert kind == "original"


def test_head_source_of_supplied_markdown_source_is_original_too():
    """An HTML artifact published with markdown_source exports that verbatim."""
    env = _envelope(
        4, title="Raw HTML doc", markdown=V2_MD, html=V3_HTML, source_type="html"
    )
    filename, content_type, body, kind = head_source(env)
    assert filename == "raw-html-doc.md"
    assert content_type == "text/markdown; charset=utf-8"
    assert body == V2_MD
    assert kind == "original"


def test_head_source_html_branch_converts_to_markdown():
    env = _envelope(3, title="Raw HTML doc", html=V3_HTML, source_type="html")
    filename, content_type, body, kind = head_source(env)
    assert filename == "raw-html-doc.md"
    assert content_type == "text/markdown; charset=utf-8"
    assert body == "# Proposal\n"
    assert kind == "converted"


def test_head_source_falls_back_to_html_when_conversion_fails(monkeypatch):
    """A converter failure degrades to the old behaviour, it never raises."""
    monkeypatch.setattr(
        "src.export._converter", _boom, raising=True
    )
    env = _envelope(3, title="Raw HTML doc", html=V3_HTML, source_type="html")
    filename, content_type, body, kind = head_source(env)
    assert filename == "raw-html-doc.html"
    assert content_type == "text/html; charset=utf-8"
    assert body == V3_HTML
    assert kind == "none"


def test_head_source_falls_back_to_artifact_id_for_empty_title():
    env = _envelope(1, title="", markdown=V1_MD)
    filename, _, _, _ = head_source(env)
    assert filename == f"{slugify('', ARTIFACT_ID)}.md"


# ------------------------------------------------------- markdown_of_html


def _boom(*args, **kwargs):
    raise RuntimeError("converter exploded")


RICH_HTML = """<!doctype html>
<html lang="en">
<head>
<title>Q3 review</title>
<style>body { color: #ff0000; font-family: "Secret Sans"; }</style>
<script>window.TRACKING_TOKEN = "leaky-value";</script>
</head>
<body>
<h1 id="q3-review">Q3 review</h1>
<p>See the <a href="https://example.com/runbook">runbook</a>.</p>
<h2>Numbers</h2>
<table>
<thead><tr><th>Metric</th><th>Q2</th><th>Q3</th></tr></thead>
<tbody>
<tr><td>Runs</td><td>120</td><td>184</td></tr>
<tr><td>Failures</td><td>9</td><td>3</td></tr>
</tbody>
</table>
<h2>Pipeline</h2>
<pre class="mermaid">flowchart LR
  A[Extract] --&gt; B[Load]</pre>
<pre><code class="language-python">settings = Settings(max_retries=3)
</code></pre>
<ul><li>first<ul><li>nested</li></ul></li><li>second</li></ul>
<canvas id="chart" aria-label="Revenue by quarter"></canvas>
<svg viewBox="0 0 4 4"><title>Architecture sketch</title><circle r="1"/></svg>
<img src="data:image/png;base64,AAAA" alt="Inlined logo">
</body>
</html>
"""


@pytest.fixture(scope="module")
def rich() -> str:
    converted = markdown_of_html(RICH_HTML)
    assert converted is not None
    return converted


def test_conversion_keeps_headings_links_and_nested_lists(rich):
    assert "# Q3 review" in rich
    assert "## Numbers" in rich
    assert "[runbook](https://example.com/runbook)" in rich
    assert "- first\n  - nested\n- second" in rich


def test_conversion_produces_a_gfm_pipe_table(rich):
    assert "| Metric | Q2 | Q3 |" in rich
    assert "| --- | --- | --- |" in rich
    assert "| Runs | 120 | 184 |" in rich


def test_conversion_restores_mermaid_and_language_fences(rich):
    assert "```mermaid\nflowchart LR\n  A[Extract] --> B[Load]\n```" in rich
    assert "```python\nsettings = Settings(max_retries=3)\n```" in rich


def test_conversion_never_leaks_script_or_style_content(rich):
    assert "leaky-value" not in rich
    assert "TRACKING_TOKEN" not in rich
    assert "Secret Sans" not in rich
    assert "#ff0000" not in rich


def test_conversion_degrades_canvas_svg_and_inlined_images_to_placeholders(rich):
    assert "_[chart: Revenue by quarter — not exportable to Markdown]_" in rich
    assert "_[image: Architecture sketch — not exportable to Markdown]_" in rich
    assert "_[image: Inlined logo — not exportable to Markdown]_" in rich
    # The base64 blob itself must never land in the Markdown.
    assert "base64" not in rich
    assert "<svg" not in rich and "<canvas" not in rich


def test_conversion_collapses_blank_line_runs(rich):
    assert "\n\n\n" not in rich
    assert rich.endswith("\n") and not rich.endswith("\n\n")
    assert not any(line != line.rstrip() for line in rich.splitlines())


def test_conversion_is_deterministic():
    assert markdown_of_html(RICH_HTML) == markdown_of_html(RICH_HTML)


def test_conversion_uses_the_document_title_when_the_body_has_no_h1():
    converted = markdown_of_html(
        "<html><head><title>Notes</title></head><body><p>Hi</p></body></html>"
    )
    assert converted == "# Notes\n\nHi\n"


def test_conversion_of_nothing_is_none():
    assert markdown_of_html("") is None
    assert markdown_of_html("   ") is None
    assert markdown_of_html("<html><body></body></html>") is None


def test_conversion_failure_is_swallowed_and_logged(monkeypatch, caplog):
    monkeypatch.setattr("src.export._converter", _boom, raising=True)
    with caplog.at_level("ERROR"):
        assert markdown_of_html(RICH_HTML) is None
    assert "conversion failed" in caplog.text


# ------------------------------------------------------------------- slugify


@pytest.mark.parametrize(
    "text,fallback,expected",
    [
        ("Příliš: žluťoučký kůň", "", "prilis-zlutoucky-kun"),
        ("  Hello,   World!  ", "", "hello-world"),
        ("", "AbC123xyz", "abc123xyz"),
        ("", "", "artifact"),
        ("---", "", "artifact"),
        ("日本語だけ", "Fall_Back", "fall-back"),
        ("a" * 100, "", "a" * 60),
        ("Ünïcödé — dash", "", "unicode-dash"),
    ],
)
def test_slugify_edge_cases(text, fallback, expected):
    assert slugify(text, fallback) == expected


def test_slugify_never_ends_with_a_dash_after_capping():
    slug = slugify("x" * 59 + " tail")
    assert not slug.endswith("-")
    assert len(slug) <= 60


# ------------------------------------------------------------------- vault


def test_vault_filename_and_exact_file_list(meta, envelopes, threads):
    name, _, archive = _open(meta, envelopes, threads)
    assert name == f"{ROOT}-vault.zip"
    assert archive.namelist() == [
        f"{ROOT}/INDEX.md",
        f"{ROOT}/document.md",
        f"{ROOT}/versions/v1.md",
        f"{ROOT}/versions/v2.md",
        f"{ROOT}/versions/v3.md",
        f"{ROOT}/versions/v3.html",
        f"{ROOT}/comments/th-one.md",
        f"{ROOT}/comments/th-two.md",
        f"{ROOT}/reasoning.md",
    ]


def test_index_wikilinks_resolve_to_real_files(meta, envelopes, threads):
    _, _, archive = _open(meta, envelopes, threads)
    index = _read(archive, f"{ROOT}/INDEX.md")
    names = set(archive.namelist())

    targets = {
        match.split("|", 1)[0]
        for match in re.findall(r"\[\[([^\]]+)\]\]", index)
    }
    assert targets == {
        "document",
        "reasoning",
        "versions/v1",
        "versions/v2",
        "versions/v3",
        "comments/th-one",
        "comments/th-two",
    }
    for target in targets:
        assert f"{ROOT}/{target}.md" in names


def test_vault_publishes_the_share_id_not_the_internal_id(envelopes, threads):
    """A vault is a download, so the identity it carries is the public one.

    The internal artifact id is the owner's handle for every authenticated
    /api/* call. After a link rotation the two differ, and only the share id is
    the reader's to keep.
    """
    rotated = ArtifactMeta(
        id=ARTIFACT_ID,
        owner=dict(OWNER),
        share_id="RotatedShareId9",
        created_at="2026-01-01T08:00:00+00:00",
        updated_at="2026-01-05T10:00:00+00:00",
    )
    _, _, archive = _open(rotated, envelopes, threads)

    for note in (f"{ROOT}/INDEX.md", f"{ROOT}/reasoning.md"):
        lines = _frontmatter_lines(_read(archive, note))
        assert 'artifact_id: "RotatedShareId9"' in lines
        assert f'artifact_id: "{ARTIFACT_ID}"' not in lines


def test_vault_falls_back_to_the_id_when_there_is_no_share_id(envelopes, threads):
    """A meta record written before share ids existed still exports cleanly."""
    legacy = ArtifactMeta(id=ARTIFACT_ID, owner=dict(OWNER))
    _, _, archive = _open(legacy, envelopes, threads)
    lines = _frontmatter_lines(_read(archive, f"{ROOT}/INDEX.md"))
    assert f'artifact_id: "{ARTIFACT_ID}"' in lines


def test_index_frontmatter_and_sections(meta, envelopes, threads):
    _, _, archive = _open(meta, envelopes, threads)
    index = _read(archive, f"{ROOT}/INDEX.md")
    lines = _frontmatter_lines(index)

    assert f'artifact_id: "{ARTIFACT_ID}"' in lines
    assert 'status: "draft"' in lines
    assert 'owner_project: "Acme Analytics"' in lines
    assert 'owner_stack: "connection.keboola.com"' in lines
    assert "versions: 3" in lines
    assert "live_versions: 2" in lines
    assert "proposed_versions: 1" in lines
    assert "comments: 2" in lines
    assert "open_comments: 1" in lines

    assert "## Versions" in index
    assert "## Discussion" in index
    # Head (v2) is flagged as the served version, v3 as a proposal.
    assert "— live · served" in index
    assert "— proposed" in index
    assert "resolved · on [[versions/v1]]" in index


def test_document_carries_head_markdown(meta, envelopes, threads):
    _, _, archive = _open(meta, envelopes, threads)
    assert _read(archive, f"{ROOT}/document.md") == V2_MD
    assert f"{ROOT}/document.html" not in archive.namelist()


def test_html_head_produces_converted_markdown_plus_an_html_sidecar(meta):
    """document.md carries real Markdown; the HTML stays alongside it."""
    html_head = _envelope(
        1,
        title="Raw doc",
        html=V3_HTML,
        source_type="html",
        created_at="2026-01-01T10:00:00+00:00",
    )
    _, _, archive = _open(meta, [html_head], [])
    assert "raw-doc/document.html" in archive.namelist()
    assert _read(archive, "raw-doc/document.html") == V3_HTML

    document = _read(archive, "raw-doc/document.md")
    assert document.startswith("# Proposal\n")
    assert "converted from it" in document
    assert "`document.html`" in document
    assert "`document.html`" in _read(archive, "raw-doc/INDEX.md")


def test_html_head_with_a_supplied_markdown_source_is_not_converted(meta):
    """markdown_source wins over conversion, verbatim, and needs no footnote."""
    html_head = _envelope(
        1,
        title="Raw doc",
        markdown=V2_MD,
        html=V3_HTML,
        source_type="html",
        created_at="2026-01-01T10:00:00+00:00",
    )
    _, _, archive = _open(meta, [html_head], [])
    assert _read(archive, "raw-doc/document.md") == V2_MD
    assert "raw-doc/document.html" not in archive.namelist()


def test_html_version_gets_converted_markdown_and_an_html_sidecar(meta, envelopes, threads):
    _, _, archive = _open(meta, envelopes, threads)
    assert _read(archive, f"{ROOT}/versions/v3.html") == V3_HTML
    body = _read(archive, f"{ROOT}/versions/v3.md")
    assert "# Proposal" in body
    assert "converted from it" in body
    assert "`v3.html`" in body
    assert f"{ROOT}/versions/v1.html" not in archive.namelist()
    assert f"{ROOT}/versions/v2.html" not in archive.namelist()


def test_version_frontmatter_spot_check(meta, envelopes, threads):
    _, _, archive = _open(meta, envelopes, threads)

    v2 = _frontmatter_lines(_read(archive, f"{ROOT}/versions/v2.md"))
    assert "version: 2" in v2
    assert 'status: "live"' in v2
    assert 'author_project: "Acme Analytics"' in v2
    assert "author_project_id: 111" in v2
    assert 'author_stack: "connection.keboola.com"' in v2
    assert 'note: "Tightened the intro"' in v2
    assert 'source_type: "markdown"' in v2

    v3 = _frontmatter_lines(_read(archive, f"{ROOT}/versions/v3.md"))
    assert 'status: "proposed"' in v3
    assert 'author_project: "Beta Corp"' in v3
    assert 'author_stack: "connection.north-europe.azure.keboola.com"' in v3

    v1 = _frontmatter_lines(_read(archive, f"{ROOT}/versions/v1.md"))
    assert "note: null" in v1


def test_version_body_and_diff_section(meta, envelopes, threads):
    _, _, archive = _open(meta, envelopes, threads)

    v1 = _read(archive, f"{ROOT}/versions/v1.md")
    assert "First draft of the plan." in v1
    assert "## Changes vs previous version" in v1
    assert "First version — nothing to compare against." in v1
    assert "```diff" not in v1

    v2 = _read(archive, f"{ROOT}/versions/v2.md")
    assert "Second draft of the plan, tightened." in v2
    assert "```diff" in v2
    assert "Comparing markdown." in v2
    assert "+# Intro" not in v2  # unchanged line stays context, not an addition
    assert "-First draft of the plan." in v2
    assert "+Second draft of the plan, tightened." in v2

    # v2 (markdown) -> v3 (html) has no shared markdown, so HTML is compared.
    v3 = _read(archive, f"{ROOT}/versions/v3.md")
    assert "Comparing html." in v3


def test_version_git_provenance_in_frontmatter(meta):
    env = Envelope(
        id=ARTIFACT_ID,
        version=1,
        title="From git",
        html="<html></html>",
        source_type="git-markdown",
        source={
            "markdown": "# hi\n",
            "git": {"url": "https://github.com/padak/x", "ref": "main", "path": "README.md"},
        },
        author=dict(OWNER),
        status=STATUS_LIVE,
        created_at="2026-01-01T10:00:00+00:00",
    )
    _, _, archive = _open(meta, [env], [])
    lines = _frontmatter_lines(_read(archive, "from-git/versions/v1.md"))
    assert 'git_url: "https://github.com/padak/x"' in lines
    assert 'git_ref: "main"' in lines
    assert 'git_path: "README.md"' in lines


def test_comment_note_contents(meta, envelopes, threads):
    _, _, archive = _open(meta, envelopes, threads)

    resolved = _read(archive, f"{ROOT}/comments/th-one.md")
    lines = _frontmatter_lines(resolved)
    assert 'thread_id: "th-one"' in lines
    assert "version: 1" in lines
    assert "resolved: true" in lines
    assert 'resolved_by: "Acme Analytics"' in lines
    assert 'author_project: "Beta Corp"' in lines
    assert "[[versions/v1]]" in resolved
    assert "> First draft" in resolved
    assert "**Beta Corp (2026-01-02):** Is this the final wording?" in resolved
    assert "**Acme Analytics (2026-01-02):** No, v2 rewrites it." in resolved
    assert "**Beta Corp (2026-01-02):** Great, thanks." in resolved
    assert "Resolved by Acme Analytics." in resolved

    open_thread = _read(archive, f"{ROOT}/comments/th-two.md")
    open_lines = _frontmatter_lines(open_thread)
    assert "resolved: false" in open_lines
    assert "resolved_by" not in open_thread
    assert "Open." in open_thread


def test_reasoning_is_chronological(meta, envelopes, threads):
    _, _, archive = _open(meta, envelopes, threads)
    reasoning = _read(archive, f"{ROOT}/reasoning.md")

    bullets = [line for line in reasoning.splitlines() if line.startswith("- ")]
    dates = [line[2:12] for line in bullets]
    assert dates == sorted(dates)
    assert dates == [
        "2026-01-01",
        "2026-01-02",
        "2026-01-03",
        "2026-01-04",
        "2026-01-05",
    ]

    assert "v1|v1]] published by Acme Analytics" in bullets[0]
    assert "comment]] opened by Beta Corp on v1" in bullets[1]
    assert "resolved, 2 replies" in bullets[1]
    assert "note: Tightened the intro" in bullets[2]
    assert bullets[3].endswith("— open")
    assert "v3|v3]] proposed by Beta Corp" in bullets[4]

    assert "## Current status" in reasoning
    assert "Draft — open for new versions and comments." in reasoning
    assert "The served version is [[versions/v2|v2]]" in reasoning


def test_vault_is_byte_identical_across_builds(meta, envelopes, threads):
    first_name, first = build_vault(meta, envelopes, threads)
    second_name, second = build_vault(meta, envelopes, threads)
    assert first_name == second_name
    assert first == second

    # Fixed entry timestamps are what makes that true.
    with zipfile.ZipFile(io.BytesIO(first)) as archive:
        assert {info.date_time for info in archive.infolist()} == {
            (2020, 1, 1, 0, 0, 0)
        }


def test_pinned_head_is_honoured(meta, envelopes, threads):
    meta.head_mode = "pinned"
    meta.head_version = 1
    _, _, archive = _open(meta, envelopes, threads)
    # Root slug + document both follow the pinned version (same title here),
    # and INDEX marks v1 as served.
    assert _read(archive, f"{ROOT}/document.md") == V1_MD
    index = _read(archive, f"{ROOT}/INDEX.md")
    served = [line for line in index.splitlines() if line.endswith("· served")]
    assert len(served) == 1
    assert "versions/v1|" in served[0]


def test_pinned_proposal_falls_back_to_newest_live(meta, envelopes, threads):
    meta.head_mode = "pinned"
    meta.head_version = 3  # proposed, never served
    _, _, archive = _open(meta, envelopes, threads)
    assert _read(archive, f"{ROOT}/document.md") == V2_MD


def test_artifact_without_live_versions(meta):
    proposal = _envelope(
        1,
        title="",
        markdown="# nope\n",
        status=STATUS_PROPOSED,
        created_at="2026-01-01T10:00:00+00:00",
    )
    name, _, archive = _open(meta, [proposal], [])
    root = slugify("", ARTIFACT_ID)
    assert name == f"{root}-vault.zip"
    assert f"{root}/document.md" in archive.namelist()
    assert "no live version" in _read(archive, f"{root}/document.md")
    assert "No version is currently served." in _read(archive, f"{root}/reasoning.md")


def test_artifact_without_versions_or_comments(meta):
    _, _, archive = _open(meta, [], [])
    root = slugify("", ARTIFACT_ID)
    assert archive.namelist() == [
        f"{root}/INDEX.md",
        f"{root}/document.md",
        f"{root}/reasoning.md",
    ]
    assert "_No versions._" in _read(archive, f"{root}/INDEX.md")
    assert "_No comments._" in _read(archive, f"{root}/INDEX.md")
    assert "_Nothing has happened yet._" in _read(archive, f"{root}/reasoning.md")


# -------------------------------------------------------------- YAML safety


NASTY = 'He said "hi": line1\nline2\ttab \\ backslash \x07bell'
NASTY_YAML = (
    '"He said \\"hi\\": line1\\nline2\\ttab \\\\ backslash \\x07bell"'
)


def test_yaml_escaping_of_nasty_text(meta):
    meta.owner = dict(OWNER, project_name=NASTY)
    env = _envelope(
        1,
        title=NASTY,
        markdown="# body\n",
        note=NASTY,
        created_at="2026-01-01T10:00:00+00:00",
    )
    thread = CommentThread(
        id="nasty/../id",
        artifact_id=ARTIFACT_ID,
        version=1,
        selector=Selector(exact=NASTY, prefix="", suffix=""),
        body=NASTY,
        author=dict(CONTRIBUTOR, project_name=NASTY),
        created_at="2026-01-02T09:00:00+00:00",
        resolved=False,
        resolved_by=None,
        replies=[],
    )
    _, _, archive = _open(meta, [env], [thread])
    root = slugify(NASTY, ARTIFACT_ID)

    index_lines = _frontmatter_lines(_read(archive, f"{root}/INDEX.md"))
    assert f"title: {NASTY_YAML}" in index_lines
    assert f"owner_project: {NASTY_YAML}" in index_lines
    # Every key stays on exactly one line — no newline escaped into the block.
    assert len(index_lines) == 12
    assert all(re.match(r"^[a-z_]+: ", line) for line in index_lines)

    version_lines = _frontmatter_lines(_read(archive, f"{root}/versions/v1.md"))
    assert f"note: {NASTY_YAML}" in version_lines

    # A thread id with path separators cannot escape the comments folder.
    comment_files = [n for n in archive.namelist() if n.startswith(f"{root}/comments/")]
    assert comment_files == [f"{root}/comments/nasty-id.md"]
    comment = _read(archive, comment_files[0])
    comment_lines = _frontmatter_lines(comment)
    # The unsanitized id is still recorded faithfully — but safely quoted.
    assert 'thread_id: "nasty/../id"' in comment_lines
    assert f"author_project: {NASTY_YAML}" in comment_lines
    # Multi-line quoted text becomes a proper multi-line blockquote.
    assert '> He said "hi": line1' in comment
    assert "> line2\ttab \\ backslash \x07bell" in comment


def test_multiline_note_stays_single_frontmatter_line(meta):
    env = _envelope(
        1,
        markdown="# body\n",
        note="line one\nline two: with colon",
        created_at="2026-01-01T10:00:00+00:00",
    )
    _, _, archive = _open(meta, [env], [])
    lines = _frontmatter_lines(_read(archive, f"{ROOT}/versions/v1.md"))
    assert 'note: "line one\\nline two: with colon"' in lines
    assert len([line for line in lines if line.startswith("note:")]) == 1


def test_markdown_body_with_fences_does_not_break_the_diff_fence(meta):
    first = _envelope(
        1, markdown="```python\nprint(1)\n```\n", created_at="2026-01-01T10:00:00+00:00"
    )
    second = _envelope(
        2, markdown="```python\nprint(2)\n```\n", created_at="2026-01-02T10:00:00+00:00"
    )
    _, _, archive = _open(meta, [first, second], [])
    body = _read(archive, f"{ROOT}/versions/v2.md")
    # The diff contains ``` runs, so the surrounding fence must be longer.
    assert "````diff" in body
    assert body.rstrip().endswith("````")


class TestGuestAttribution:
    """Guest commenters carry a name, not a project identity."""

    def test_display_name_labels_a_guest(self) -> None:
        from src.export import _display_name

        guest = {"kind": "guest", "name": "Board Observer", "invitation_id": "abc"}
        assert _display_name(guest) == "Board Observer (guest)"

    def test_display_name_falls_back_for_a_nameless_guest(self) -> None:
        from src.export import _display_name

        assert _display_name({"kind": "guest"}) == "a guest"

    def test_display_name_still_prefers_a_project_name(self) -> None:
        from src.export import _display_name

        who = {"project_id": 10539, "project_name": "Padak 2.0"}
        assert _display_name(who) == "Padak 2.0"
