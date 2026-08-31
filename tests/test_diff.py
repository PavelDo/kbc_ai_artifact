"""Tests for src.diff: comparable-text selection and the three renderings."""

import json

import pytest

from src.diff import DiffError, compute_diff
from src.store import Envelope

MAX_BYTES = 2 * 1024 * 1024


def _env(version: int, html: str = "", markdown: str | None = None) -> Envelope:
    source = {"markdown": markdown} if markdown is not None else {}
    return Envelope(
        id="art1",
        version=version,
        title=f"v{version}",
        html=html,
        source_type="markdown" if markdown is not None else "html",
        source=source,
        author={
            "stack_url": "https://connection.keboola.com",
            "project_id": 1,
            "project_name": "Proj",
            "key": "1@connection.keboola.com",
        },
        created_at="2026-01-01T00:00:00+00:00",
    )


# --------------------------------------------------------------------------
# Which text gets compared
# --------------------------------------------------------------------------


class TestKindSelection:
    def test_markdown_when_both_sides_have_it(self):
        a = _env(1, html="<p>one</p>", markdown="one")
        b = _env(2, html="<p>two</p>", markdown="two")
        _, body = compute_diff(a, b, "json", MAX_BYTES)
        payload = json.loads(body)
        assert payload["kind"] == "markdown"
        assert "+two" in payload["unified"]
        assert "<p>" not in payload["unified"]

    def test_html_when_only_one_side_has_markdown(self):
        a = _env(1, html="<p>one</p>", markdown="one")
        b = _env(2, html="<p>two</p>")
        payload = json.loads(compute_diff(a, b, "json", MAX_BYTES)[1])
        assert payload["kind"] == "html"
        assert "<p>two</p>" in payload["unified"]

    def test_html_when_neither_side_has_markdown(self):
        payload = json.loads(
            compute_diff(_env(1, "<p>a</p>"), _env(2, "<p>b</p>"), "json", MAX_BYTES)[1]
        )
        assert payload["kind"] == "html"


# --------------------------------------------------------------------------
# Unified
# --------------------------------------------------------------------------


class TestUnified:
    def test_headers_and_hunk(self):
        a = _env(3, markdown="line one\nline two\nline three")
        b = _env(4, markdown="line one\nline TWO\nline three")
        content_type, body = compute_diff(a, b, "unified", MAX_BYTES)
        assert content_type.startswith("text/plain")
        lines = body.splitlines()
        assert lines[0] == "--- v3"
        assert lines[1] == "+++ v4"
        assert "-line two" in lines
        assert "+line TWO" in lines
        # Unchanged context lines are prefixed with a space, not dropped.
        assert " line one" in lines

    def test_identical_versions_produce_an_empty_diff(self):
        a = _env(1, markdown="same\ntext")
        b = _env(2, markdown="same\ntext")
        assert compute_diff(a, b, "unified", MAX_BYTES)[1] == ""

    def test_pure_insertion(self):
        a = _env(1, markdown="a\nb")
        b = _env(2, markdown="a\nb\nc")
        body = compute_diff(a, b, "unified", MAX_BYTES)[1]
        assert "+c" in body
        assert "-" not in body.splitlines()[3:]


# --------------------------------------------------------------------------
# JSON
# --------------------------------------------------------------------------


class TestJson:
    def test_shape_and_stats(self):
        a = _env(1, markdown="keep\nold one\nold two\ndrop")
        b = _env(2, markdown="keep\nnew one\nnew two\nnew three")
        content_type, body = compute_diff(a, b, "json", MAX_BYTES)
        assert content_type == "application/json"
        payload = json.loads(body)
        assert payload["from"] == 1
        assert payload["to"] == 2
        assert payload["kind"] == "markdown"
        assert payload["stats"] == {"added": 3, "removed": 3}
        assert payload["unified"].startswith("--- v1")

    def test_zero_stats_for_identical_versions(self):
        payload = json.loads(
            compute_diff(_env(1, "<p>x</p>"), _env(2, "<p>x</p>"), "json", MAX_BYTES)[1]
        )
        assert payload["stats"] == {"added": 0, "removed": 0}

    def test_insert_only_stats(self):
        a = _env(1, markdown="a")
        b = _env(2, markdown="a\nb\nc")
        payload = json.loads(compute_diff(a, b, "json", MAX_BYTES)[1])
        assert payload["stats"] == {"added": 2, "removed": 0}


# --------------------------------------------------------------------------
# Side-by-side HTML
# --------------------------------------------------------------------------


class TestHtml:
    def test_contains_both_sides_and_header(self):
        a = _env(1, markdown="alpha\nbravo")
        b = _env(2, markdown="alpha\ncharlie")
        content_type, body = compute_diff(a, b, "html", MAX_BYTES)
        assert content_type.startswith("text/html")
        assert body.startswith("<!doctype html>")
        assert "artifact art1: v1 vs v2" in body
        assert "+1" in body and "-1" in body
        assert "bravo" in body
        assert "charlie" in body
        assert 'class="replace"' in body

    def test_is_self_contained(self):
        body = compute_diff(_env(1, "<p>a</p>"), _env(2, "<p>b</p>"), "html", MAX_BYTES)[1]
        assert "<style>" in body
        assert "prefers-color-scheme" in body
        assert "http://" not in body
        assert "<script" not in body

    def test_escapes_content(self):
        a = _env(1, markdown='<img src=x onerror="alert(1)">')
        b = _env(2, markdown="<b>safe</b>")
        body = compute_diff(a, b, "html", MAX_BYTES)[1]
        assert "onerror" in body  # the text is shown …
        assert "<img src=x" not in body  # … but never as live markup
        assert "&lt;img src=x" in body
        assert "&lt;b&gt;safe&lt;/b&gt;" in body

    def test_long_unchanged_run_is_collapsed(self):
        common = "\n".join(f"line {i}" for i in range(40))
        a = _env(1, markdown=f"first old\n{common}\nlast old")
        b = _env(2, markdown=f"first new\n{common}\nlast new")
        body = compute_diff(a, b, "html", MAX_BYTES)[1]
        assert "unchanged lines …" in body
        assert 'class="skip"' in body
        # Context survives around the collapse, the bulk does not.
        assert "line 0" in body
        assert "line 20" not in body

    def test_short_unchanged_run_is_not_collapsed(self):
        common = "\n".join(f"line {i}" for i in range(4))
        a = _env(1, markdown=f"old\n{common}")
        b = _env(2, markdown=f"new\n{common}")
        body = compute_diff(a, b, "html", MAX_BYTES)[1]
        assert "unchanged lines …" not in body
        assert "line 2" in body

    def test_insert_and_delete_rows(self):
        a = _env(1, markdown="keep\ngone")
        b = _env(2, markdown="keep\nadded one\nadded two")
        body = compute_diff(a, b, "html", MAX_BYTES)[1]
        assert 'class="insert"' in body or 'class="replace"' in body
        assert "added two" in body
        assert "gone" in body


# --------------------------------------------------------------------------
# Guards
# --------------------------------------------------------------------------


class TestGuards:
    @pytest.mark.parametrize("fmt", ["html", "unified", "json"])
    def test_too_large_raises_for_every_format(self, fmt):
        big = "x" * 100
        a = _env(1, html=big)
        b = _env(2, html="y")
        with pytest.raises(DiffError):
            compute_diff(a, b, fmt, max_bytes=10)

    def test_large_new_side_also_raises(self):
        with pytest.raises(DiffError):
            compute_diff(_env(1, "y"), _env(2, "x" * 100), "unified", max_bytes=10)

    def test_markdown_side_is_the_one_measured(self):
        # HTML is huge but Markdown is tiny: the Markdown pair is compared, so
        # the guard must not trip.
        a = _env(1, html="x" * 1000, markdown="a")
        b = _env(2, html="y" * 1000, markdown="b")
        _, body = compute_diff(a, b, "unified", max_bytes=100)
        assert "+b" in body

    def test_unknown_format_raises(self):
        with pytest.raises(DiffError):
            compute_diff(_env(1, "a"), _env(2, "b"), "pdf", MAX_BYTES)
