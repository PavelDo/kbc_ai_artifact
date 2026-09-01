"""Human-facing HTML shell pages and their shared mini design system.

Seven pages are rendered by the service itself:

- the landing page at ``/`` — what the hub is, what it does, and how to drive
  it from a terminal,
- the unlock form shown when a password-protected artifact is opened in a
  browser,
- the version picker at ``/a/{id}/versions?format=html``,
- the owner/moderation studio at ``/admin``, a single self-contained page whose
  vanilla JS talks to the same public API a terminal would, and
- the review UI at ``/a/{id}/review``, a two-pane reader that renders the
  artifact inside a sandboxed ``srcdoc`` iframe and keeps inline comment
  threads beside it,
- the changelog at ``/changelog``, this repository's ``CHANGELOG.md`` rendered
  into the same shell rather than into an artifact page, and
- the visual diff at ``/a/{id}/diff/{a}..{b}?format=visual``, two versions of
  one document side by side in their own sandboxed iframes, scrolling in step.

Artifact content itself is never templated here — it is served verbatim from
the version envelope.

**Design system.** One shared stylesheet (:data:`_CSS`) backs all pages:
light-first (a dark variant follows ``prefers-color-scheme``), monospace-forward
(JetBrains Mono for headings, labels and code; Inter for prose, both with full
local fallback stacks), a single electric-blue accent, a faint graph-paper grid
behind the page, ``//`` small-caps section labels, and dark terminal cards that
carry the ``$ curl`` examples as first-class content rather than decoration.
Google Fonts are linked but never required: every family has a local fallback
stack, so the pages degrade cleanly behind a proxy that blocks them.

Every dynamic value is escaped with :func:`html.escape` before it reaches the
markup.
"""

from __future__ import annotations

import html
import re

#: Google Fonts, linked with ``display=swap``. Both families have full local
#: fallback stacks in ``--font-*`` below, so a blocked CDN costs nothing but
#: the exact letterforms.
_FONT_LINKS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
    "family=Inter:wght@400;500;600&"
    'family=JetBrains+Mono:wght@400;500;700&display=swap">\n'
)

#: The shared design system. Light is the designed-for mode; the dark block
#: only re-points the tokens.
_CSS = """
:root {
  color-scheme: light dark;
  --font-mono: "JetBrains Mono", ui-monospace, SFMono-Regular, "SF Mono", Menlo,
    Consolas, "Liberation Mono", monospace;
  --font-sans: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
    "Helvetica Neue", Arial, sans-serif;

  --paper: #f6f8fb;
  --panel: #ffffff;
  --ink: #0d1622;
  --ink-2: #38455a;
  --muted: #697687;
  --line: #d8e0ea;
  --grid: #e6ecf3;
  --accent: #1442e0;
  --accent-ink: #1442e0;
  --accent-soft: #e7ecfd;
  --on-accent: #ffffff;
  --term-bg: #0d1622;
  --term-fg: #d9e3f0;
  --term-dim: #7d8ca3;
  --term-line: #24324a;
  --live: #0a7043;
  --live-soft: #dcf3e7;
  --proposed: #8a5300;
  --proposed-soft: #fbeeda;
  --danger: #b42318;
  --radius: 10px;
}
@media (prefers-color-scheme: dark) {
  :root {
    --paper: #0b1119;
    --panel: #111a25;
    --ink: #e7edf5;
    --ink-2: #b3c0d1;
    --muted: #8e9cae;
    --line: #22303f;
    --grid: #141e2b;
    --accent: #7aa2ff;
    --accent-ink: #9dbaff;
    --accent-soft: #16233d;
    --on-accent: #0b1119;
    --term-bg: #060b12;
    --term-fg: #d9e3f0;
    --term-line: #1d2839;
    --live: #4ec98a;
    --live-soft: #10261c;
    --proposed: #e0a33f;
    --proposed-soft: #2a2010;
    --danger: #ff8a80;
  }
}

* { box-sizing: border-box; }

body {
  margin: 0;
  background: transparent;
  color: var(--ink);
  font-family: var(--font-sans);
  font-size: 16px;
  line-height: 1.65;
  -webkit-font-smoothing: antialiased;
}

/* Graph-paper texture: the only ornament on the page. Painted as a plain
   background on <html> rather than a fixed overlay, so it never becomes its
   own compositing layer. */
html {
  background-color: var(--paper);
  background-image:
    linear-gradient(var(--grid) 1px, transparent 1px),
    linear-gradient(90deg, var(--grid) 1px, transparent 1px);
  background-size: 34px 34px;
}

main { max-width: 60rem; margin: 0 auto; padding: 3.5rem 1.25rem 4rem; }

a { color: var(--accent-ink); text-decoration-thickness: 1px;
  text-underline-offset: .18em; }
a:hover { text-decoration-thickness: 2px; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px;
  border-radius: 4px; }

h1, h2, h3 { font-family: var(--font-mono); font-weight: 700;
  letter-spacing: -.02em; }
h1 { font-size: clamp(1.9rem, 1.2rem + 2.4vw, 2.9rem); line-height: 1.08;
  margin: 0 0 .6rem; }
h2 { font-size: 1.15rem; margin: 0 0 1rem; letter-spacing: -.01em; }
h3 { font-size: .95rem; margin: 0 0 .35rem; }
p { margin: .7rem 0; color: var(--ink-2); }

code, pre, .mono { font-family: var(--font-mono); font-size: .85rem; }
code { background: var(--accent-soft); color: var(--accent-ink);
  padding: .08rem .32rem; border-radius: 4px; }

/* -------- section label: "// what it does" -------------------------------- */
.label {
  font-family: var(--font-mono);
  font-size: .72rem;
  font-weight: 500;
  letter-spacing: .16em;
  text-transform: uppercase;
  color: var(--muted);
  margin: 3.25rem 0 .85rem;
  display: flex;
  align-items: center;
  gap: .6rem;
}
.label::before { content: "//"; color: var(--accent); font-weight: 700; }
.label::after { content: ""; flex: 1; height: 1px; background: var(--line); }

/* -------- badges ---------------------------------------------------------- */
.badge {
  display: inline-flex;
  align-items: center;
  gap: .4rem;
  font-family: var(--font-mono);
  font-size: .74rem;
  font-weight: 500;
  letter-spacing: .02em;
  padding: .2rem .55rem;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: var(--panel);
  color: var(--ink-2);
  white-space: nowrap;
}
.badge--version { border-color: var(--accent); color: var(--accent-ink);
  background: var(--accent-soft); }
.badge--live { border-color: transparent; background: var(--live-soft);
  color: var(--live); }
.badge--proposed { border-color: transparent; background: var(--proposed-soft);
  color: var(--proposed); }
.badge--head { border-color: var(--accent); color: var(--accent-ink);
  background: transparent; }

/* -------- hero ------------------------------------------------------------ */
.hero { margin-bottom: 1rem; }
.hero .lead { font-size: 1.12rem; color: var(--ink-2); max-width: 44rem;
  margin: 0 0 1.25rem; }
.hero-meta { display: flex; flex-wrap: wrap; align-items: center; gap: .6rem;
  margin-bottom: 1.5rem; }
.hero-links { display: flex; flex-wrap: wrap; gap: .5rem; margin: 1.5rem 0 0; }
.hero-links a {
  font-family: var(--font-mono);
  font-size: .82rem;
  text-decoration: none;
  padding: .38rem .8rem;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--panel);
  color: var(--ink);
}
.hero-links a:hover { border-color: var(--accent); color: var(--accent-ink); }
.hero-links a.primary { background: var(--accent); border-color: var(--accent);
  color: var(--on-accent); }
.hero-links a.primary:hover { filter: brightness(1.1);
  color: var(--on-accent); }

/* -------- terminal card (the signature element) --------------------------- */
.term {
  background: var(--term-bg);
  border: 1px solid var(--term-line);
  border-radius: var(--radius);
  overflow: hidden;
  margin: .5rem 0 1.5rem;
  box-shadow: 0 1px 2px rgba(13, 22, 34, .06), 0 10px 24px rgba(13, 22, 34, .06);
}
.term-bar {
  display: flex;
  align-items: center;
  gap: .55rem;
  padding: .5rem .85rem;
  border-bottom: 1px solid var(--term-line);
  font-family: var(--font-mono);
  font-size: .72rem;
  letter-spacing: .1em;
  color: var(--term-dim);
}
.term-bar .dot { width: 9px; height: 9px; border-radius: 50%;
  background: #2f3f59; flex: none; }
.term-bar .dot:nth-child(2) { background: #2a3852; }
.term-bar .dot:nth-child(3) { background: #253048; }
.term-bar .term-title { margin-left: .35rem; }
.term pre {
  margin: 0;
  padding: 1rem 1.1rem;
  overflow-x: auto;
  color: var(--term-fg);
  font-family: var(--font-mono);
  font-size: .82rem;
  line-height: 1.75;
  white-space: pre;
}
.term code { background: none; color: inherit; padding: 0; font-size: inherit; }
.term .p { color: #5fd3a0; user-select: none; }
.term .c { color: #7d8ca3; }
.term .s { color: #ffc98a; }
.term .k { color: #8fb6ff; }

/* -------- feature grid ---------------------------------------------------- */
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(15.5rem, 1fr));
  gap: .8rem;
}
.card {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: 1rem 1.1rem 1.1rem;
}
.card h3 { font-size: .88rem; letter-spacing: .01em; color: var(--ink); }
.card h3::before { content: "┌ "; color: var(--accent); font-weight: 400; }
.card p { margin: 0; font-size: .9rem; line-height: 1.6; color: var(--muted); }

/* -------- tables ---------------------------------------------------------- */
.table-wrap { overflow-x: auto; border: 1px solid var(--line);
  border-radius: var(--radius); background: var(--panel); }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: .55rem .85rem;
  border-bottom: 1px solid var(--line); vertical-align: top; font-size: .9rem; }
tr:last-child td { border-bottom: 0; }
th { font-family: var(--font-mono); font-weight: 500; font-size: .7rem;
  letter-spacing: .12em; text-transform: uppercase; color: var(--muted); }
td .mono, td code { white-space: nowrap; }

.note {
  border-left: 2px solid var(--accent);
  background: var(--panel);
  padding: .7rem 1rem;
  margin: 1rem 0;
  color: var(--muted);
  font-size: .9rem;
  border-radius: 0 var(--radius) var(--radius) 0;
}

footer {
  margin-top: 3.5rem;
  padding-top: 1.1rem;
  border-top: 1px solid var(--line);
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: .75rem;
  font-family: var(--font-mono);
  font-size: .78rem;
  color: var(--muted);
}
footer .spacer { flex: 1; }
"""

_UNLOCK_CSS = """
body { display: flex; align-items: center; justify-content: center;
  min-height: 100vh; padding: 1.25rem; }
.gate { width: 100%; max-width: 25rem; }
.gate .rule { font-family: var(--font-mono); font-size: .72rem;
  letter-spacing: .16em; text-transform: uppercase; color: var(--muted);
  margin-bottom: .6rem; }
.gate .rule::before { content: "//"; color: var(--accent); font-weight: 700;
  margin-right: .5rem; }
.gate .card { padding: 1.5rem; }
.gate h1 { font-size: 1.3rem; margin: 0 0 .3rem; }
.gate p { margin: 0 0 1.25rem; font-size: .9rem; color: var(--muted); }
label { display: block; font-family: var(--font-mono); font-size: .7rem;
  letter-spacing: .12em; text-transform: uppercase; color: var(--muted);
  margin-bottom: .4rem; }
input[type=password] {
  width: 100%;
  padding: .6rem .7rem;
  font-family: var(--font-mono);
  font-size: .9rem;
  color: var(--ink);
  background: var(--paper);
  border: 1px solid var(--line);
  border-radius: 8px;
}
button {
  margin-top: .9rem;
  width: 100%;
  padding: .6rem 1rem;
  font-family: var(--font-mono);
  font-size: .85rem;
  font-weight: 500;
  letter-spacing: .04em;
  color: var(--on-accent);
  background: var(--accent);
  border: 0;
  border-radius: 8px;
  cursor: pointer;
}
button:hover { filter: brightness(1.1); }
.error { color: var(--danger); font-family: var(--font-mono); font-size: .8rem;
  margin: .8rem 0 0; }
.hint { font-size: .8rem; margin: 1.1rem 0 0; color: var(--muted); }
"""

_VERSIONS_CSS = """
.vhead { display: flex; flex-wrap: wrap; align-items: baseline; gap: .6rem;
  margin-bottom: .4rem; }
.vlist { list-style: none; margin: 0; padding: 0; }
.vrow {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: .85rem 1rem;
  margin-bottom: .55rem;
}
.vrow.is-head { border-color: var(--accent); }
.vrow-top { display: flex; flex-wrap: wrap; align-items: center; gap: .5rem; }
.vrow-n { font-family: var(--font-mono); font-weight: 700; font-size: 1rem;
  color: var(--ink); }
.vrow-title { font-size: .92rem; color: var(--ink-2); flex: 1 1 12rem;
  min-width: 0; overflow-wrap: anywhere; }
.vrow-meta { font-family: var(--font-mono); font-size: .74rem;
  color: var(--muted); margin-top: .4rem; display: flex; flex-wrap: wrap;
  gap: .25rem .9rem; }
.vrow-note { font-size: .88rem; color: var(--ink-2); margin: .45rem 0 0;
  padding-left: .7rem; border-left: 2px solid var(--line);
  overflow-wrap: anywhere; }
.vrow-links { margin-top: .55rem; display: flex; flex-wrap: wrap; gap: .9rem;
  font-family: var(--font-mono); font-size: .78rem; }
.empty { font-family: var(--font-mono); font-size: .85rem; color: var(--muted); }
"""


#: Interactive-widget styles shared by every page whose JavaScript toggles
#: things: the studio (``/admin``) and the review UI (``/a/{id}/review``).
#: Structural tokens (colors, badges, cards, tables, terminal cards, footer)
#: stay in :data:`_CSS`.
_CONTROLS_CSS = """
/* Both pages toggle visibility with the `hidden` attribute, and several
   widgets set an explicit `display`, which would otherwise win over the
   user-agent's `[hidden] { display: none }`. */
[hidden] { display: none !important; }

/* -------- buttons --------------------------------------------------------- */
.btn {
  display: inline-flex;
  align-items: center;
  gap: .35rem;
  font-family: var(--font-mono);
  font-size: .78rem;
  font-weight: 500;
  line-height: 1.4;
  text-decoration: none;
  padding: .35rem .7rem;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel);
  color: var(--ink);
  cursor: pointer;
}
.btn:hover:not(:disabled) { border-color: var(--accent); color: var(--accent-ink); }
.btn:disabled { opacity: .5; cursor: progress; }
.btn-primary { background: var(--accent); border-color: var(--accent);
  color: var(--on-accent); }
.btn-primary:hover:not(:disabled) { filter: brightness(1.1);
  color: var(--on-accent); border-color: var(--accent); }
.btn-danger { color: var(--danger); border-color: var(--line); }
.btn-danger:hover:not(:disabled) { border-color: var(--danger);
  color: var(--danger); }
.btn-sm { font-size: .74rem; padding: .25rem .55rem; }
.btn-wide { width: 100%; justify-content: center; padding: .55rem 1rem;
  font-size: .85rem; margin-top: .3rem; }

/* -------- status lines ---------------------------------------------------- */
.err { font-family: var(--font-mono); font-size: .78rem; color: var(--danger);
  margin: .7rem 0 0; overflow-wrap: anywhere; }
.err::before { content: "! "; font-weight: 700; }
.loading { font-family: var(--font-mono); font-size: .8rem; color: var(--muted);
  padding: .6rem 0; }
.empty { font-family: var(--font-mono); font-size: .84rem; color: var(--muted);
  padding: .8rem 0; }
"""

#: Studio-only styles. Everything structural comes from :data:`_CSS` and every
#: button/status widget from :data:`_CONTROLS_CSS`; this block only adds what
#: the admin page introduces — the sign-in card, the artifact rows with their
#: expandable detail panel, and the preview modal.
_ADMIN_CSS = """
main { max-width: 68rem; }

.ahead { display: flex; flex-wrap: wrap; align-items: flex-start; gap: 1rem;
  margin-bottom: .5rem; }
.ahead h1 { font-size: clamp(1.5rem, 1.1rem + 1.4vw, 2.1rem); margin: 0 0 .35rem; }
.ahead .lead { margin: 0; color: var(--ink-2); font-size: .95rem;
  max-width: 40rem; }
.ahead-right { margin-left: auto; display: flex; align-items: center;
  gap: .5rem; flex-wrap: wrap; }

/* -------- sign in --------------------------------------------------------- */
.login-card { max-width: 30rem; padding: 1.35rem 1.4rem 1.5rem; }
.login-card label { display: block; font-family: var(--font-mono);
  font-size: .7rem; letter-spacing: .12em; text-transform: uppercase;
  color: var(--muted); margin: .9rem 0 .35rem; }
.login-card label:first-of-type { margin-top: 0; }
.login-card input, .login-card select {
  width: 100%;
  padding: .55rem .65rem;
  font-family: var(--font-mono);
  font-size: .85rem;
  color: var(--ink);
  background: var(--paper);
  border: 1px solid var(--line);
  border-radius: 8px;
}
.hint { font-size: .82rem; color: var(--muted); margin: 1rem 0 0; }
.hint code { font-size: .78rem; }

/* -------- toolbar --------------------------------------------------------- */
.toolbar { display: flex; align-items: center; gap: .6rem; margin-bottom: .7rem; }
.toolbar .spacer { flex: 1; }
.toolbar .mono { font-size: .76rem; color: var(--muted); }

/* -------- artifact rows --------------------------------------------------- */
.alist { list-style: none; margin: 0; padding: 0; }
.arow { background: var(--panel); border: 1px solid var(--line);
  border-radius: var(--radius); margin-bottom: .5rem; overflow: hidden; }
.arow.is-open { border-color: var(--accent); }
.arow-top { display: flex; flex-wrap: wrap; align-items: center; gap: .5rem;
  padding: .7rem .85rem; cursor: pointer; }
.arow-top:hover { background: var(--accent-soft); }
.arow-caret { font-family: var(--font-mono); color: var(--accent);
  width: .9rem; flex: none; }
.arow-title { font-weight: 500; color: var(--ink); flex: 1 1 12rem;
  min-width: 0; overflow-wrap: anywhere; }
.arow-badges { display: flex; flex-wrap: wrap; gap: .3rem; }
.arow-date { font-size: .72rem; color: var(--muted); white-space: nowrap; }
.idcopy {
  font-family: var(--font-mono);
  font-size: .74rem;
  color: var(--muted);
  background: var(--paper);
  border: 1px dashed var(--line);
  border-radius: 6px;
  padding: .12rem .4rem;
  cursor: pointer;
}
.idcopy:hover { border-color: var(--accent); color: var(--accent-ink); }
.idcopy.copied { border-style: solid; border-color: var(--live);
  color: var(--live); }

/* -------- detail panel ---------------------------------------------------- */
.apanel { border-top: 1px solid var(--line); padding: .85rem;
  background: var(--paper); }
.acontrols { display: flex; flex-wrap: wrap; align-items: center; gap: .5rem;
  margin-bottom: .8rem; }
.switch { display: inline-flex; align-items: center; gap: .4rem;
  font-family: var(--font-mono); font-size: .76rem; color: var(--ink-2);
  cursor: pointer; }
.switch input { accent-color: var(--accent); }
.vactions { display: flex; flex-wrap: wrap; gap: .3rem; }
.apanel table td { font-size: .82rem; }
.apanel .vnote { color: var(--muted); font-size: .8rem;
  overflow-wrap: anywhere; }

/* -------- panel sub-sections (stats, webhooks, invitations) --------------- */
.asec { border: 1px solid var(--line); border-radius: var(--radius);
  background: var(--panel); padding: .7rem .8rem; margin-bottom: .7rem; }
.asec h4 { font-family: var(--font-mono); font-size: .7rem; letter-spacing: .12em;
  text-transform: uppercase; color: var(--muted); margin: 0 0 .5rem;
  font-weight: 500; }
.asec .hint { margin: .5rem 0 0; font-size: .78rem; }
.asec-row { display: flex; flex-wrap: wrap; align-items: center; gap: .4rem; }
.asec-row input { flex: 1 1 16rem; min-width: 0; padding: .35rem .55rem;
  font-family: var(--font-mono); font-size: .78rem; color: var(--ink);
  background: var(--paper); border: 1px solid var(--line); border-radius: 8px; }

.chips { list-style: none; margin: 0 0 .5rem; padding: 0; display: flex;
  flex-direction: column; gap: .3rem; }
.chip { display: flex; flex-wrap: wrap; align-items: center; gap: .45rem;
  background: var(--paper); border: 1px solid var(--line); border-radius: 8px;
  padding: .3rem .5rem; }
.chip .chip-main { flex: 1 1 12rem; min-width: 0; font-family: var(--font-mono);
  font-size: .76rem; color: var(--ink); overflow-wrap: anywhere; }
.chip.is-off .chip-main { color: var(--muted); text-decoration: line-through; }

.spark { display: flex; align-items: flex-end; gap: 2px; height: 2.2rem;
  margin: .4rem 0 .2rem; }
.spark i { flex: 1; min-width: 2px; background: var(--accent-soft);
  border-top: 2px solid var(--accent); border-radius: 2px 2px 0 0; }
.stat-total { font-family: var(--font-mono); font-size: 1.15rem;
  font-weight: 700; color: var(--ink); }

/* -------- one-time secret ------------------------------------------------- */
.once { font-family: var(--font-mono); font-size: .76rem;
  background: var(--term-bg); color: var(--term-fg);
  border: 1px solid var(--term-line); border-radius: 8px;
  padding: .6rem .7rem; margin: .5rem 0; overflow-wrap: anywhere;
  user-select: all; }
.once-warn { color: var(--proposed); font-family: var(--font-mono);
  font-size: .76rem; margin: 0 0 .3rem; }

/* -------- preview modal --------------------------------------------------- */
.modal { position: fixed; inset: 0; background: rgba(6, 11, 18, .62);
  display: flex; align-items: center; justify-content: center; padding: 1.5rem;
  z-index: 50; }
.modal[hidden] { display: none; }
.modal-box { width: 100%; max-width: 62rem; height: 85vh; display: flex;
  flex-direction: column; background: var(--panel); border: 1px solid var(--line);
  border-radius: var(--radius); overflow: hidden; }
.modal-bar { display: flex; align-items: center; gap: .6rem;
  padding: .5rem .7rem; border-bottom: 1px solid var(--line); }
.modal-bar .spacer { flex: 1; }
.modal-title { font-size: .76rem; color: var(--muted); overflow-wrap: anywhere; }
.modal iframe { flex: 1; width: 100%; border: 0; background: #fff; }
"""

#: The whole studio, as one IIFE. Deliberately dependency-free and readable:
#: it only ever talks to the endpoints a terminal could call with curl, using
#: the two management headers the visitor supplied.
_ADMIN_JS = """
(function () {
  "use strict";

  var BASE = String(window.HUB_BASE || "").replace(/\\/+$/, "");

  /* The credential lives in this closure and in sessionStorage, and nowhere
     else: not in a cookie, not in the URL, and not in any web storage that
     outlives the tab. sessionStorage is per-tab and cleared when the tab
     closes, so a reload keeps the session while closing the tab ends it. */
  var AUTH_KEY = "hub_admin_auth";
  var auth = null;

  function $(id) { return document.getElementById(id); }
  function show(node, on) { node.hidden = !on; }

  /* Tiny escaper, kept for any place that must build markup as a string.
     Everything below prefers textContent, which cannot inject markup at all. */
  function esc(value) {
    return String(value === null || value === undefined ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) { node.className = cls; }
    if (text !== undefined && text !== null) { node.textContent = String(text); }
    return node;
  }

  function badge(text, kind) {
    return el("span", "badge" + (kind ? " badge--" + kind : ""), text);
  }

  function setError(node, message) {
    node.textContent = message || "";
    node.hidden = !message;
  }

  /* ---------------------------------------------------------------- auth */

  function loadAuth() {
    try {
      var raw = window.sessionStorage.getItem(AUTH_KEY);
      if (!raw) { return null; }
      var parsed = JSON.parse(raw);
      if (parsed && parsed.token && parsed.stack) {
        return { token: parsed.token, stack: parsed.stack };
      }
    } catch (err) {
      /* Storage disabled or unreadable: just sign in again. */
    }
    return null;
  }

  function storeAuth(value) {
    try {
      window.sessionStorage.setItem(AUTH_KEY, JSON.stringify(value));
    } catch (err) {
      /* Non-fatal: the session simply will not survive a reload. */
    }
  }

  function clearAuth() {
    try { window.sessionStorage.removeItem(AUTH_KEY); } catch (err) {}
  }

  function headers(withBody) {
    var out = {
      "X-StorageApi-Token": auth.token,
      "X-Storage-Stack": auth.stack
    };
    if (withBody) { out["Content-Type"] = "application/json"; }
    return out;
  }

  /* --------------------------------------------------------------- fetch */

  function apiMessage(status, data, text) {
    if (data && typeof data.detail === "string") { return data.detail; }
    if (data && data.detail) { return JSON.stringify(data.detail); }
    if (data && data.error) {
      return data.error + (data.detail ? " — " + data.detail : "");
    }
    if (text) { return "HTTP " + status + ": " + text.slice(0, 300); }
    return "HTTP " + status;
  }

  async function request(path, options) {
    var opts = options || {};
    var hasBody = opts.body !== undefined;
    var resp = await fetch(BASE + path, {
      method: opts.method || "GET",
      headers: headers(hasBody),
      body: hasBody ? JSON.stringify(opts.body) : undefined
    });
    var text = await resp.text();
    var data = null;
    try { data = text ? JSON.parse(text) : null; } catch (err) { data = null; }
    if (!resp.ok) { throw new Error(apiMessage(resp.status, data, text)); }
    return data;
  }

  /* Proposed versions and diffs are 403 for an anonymous browser tab, so they
     are fetched with the auth headers and rendered into a sandboxed iframe
     rather than opened as a plain link. */
  async function requestHtml(path) {
    var resp = await fetch(BASE + path, { headers: headers(false) });
    var text = await resp.text();
    if (!resp.ok) {
      var data = null;
      try { data = JSON.parse(text); } catch (err) { data = null; }
      throw new Error(apiMessage(resp.status, data, text));
    }
    return text;
  }

  /* --------------------------------------------------------------- modal */

  function openModal(title, htmlText) {
    $("modal-title").textContent = title;
    /* srcdoc + a sandbox WITHOUT allow-same-origin: the preview runs in an
       opaque origin, so nothing inside an artifact can read this page's
       sessionStorage and steal the token. */
    $("modal-frame").srcdoc = htmlText;
    show($("modal"), true);
  }

  function closeModal() {
    show($("modal"), false);
    $("modal-frame").srcdoc = "";
  }

  /* A second, content-only modal. The preview modal above hands untrusted
     artifact HTML to a sandboxed iframe; this one shows nodes this script
     built itself, which is what a copy button needs to live in. */
  function openNote(title, nodes) {
    $("note-title").textContent = title;
    var body = $("note-body");
    body.textContent = "";
    nodes.forEach(function (node) { body.appendChild(node); });
    show($("note-modal"), true);
  }

  function closeNote() {
    show($("note-modal"), false);
    $("note-body").textContent = "";
  }

  /* ------------------------------------------------------------ clipboard */

  function copy(text, node, restore) {
    function done() {
      node.classList.add("copied");
      node.textContent = "copied";
      window.setTimeout(function () {
        node.classList.remove("copied");
        node.textContent = restore;
      }, 900);
    }
    function fallback() { window.prompt("Copy this:", text); }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, fallback);
    } else {
      fallback();
    }
  }

  /* ------------------------------------------------------- artifact list */

  function renderArtifacts(rows) {
    var list = $("artifacts");
    list.textContent = "";
    show($("empty"), rows.length === 0);
    $("count").textContent =
      rows.length + (rows.length === 1 ? " artifact" : " artifacts");
    rows.forEach(function (row) { list.appendChild(artifactRow(row)); });
  }

  function artifactRow(row) {
    var item = el("li", "arow");
    var top = el("div", "arow-top");
    top.setAttribute("role", "button");
    top.tabIndex = 0;

    var caret = el("span", "arow-caret", "\\u25b8");
    top.appendChild(caret);
    top.appendChild(el("span", "arow-title", row.title || "untitled"));

    var idBtn = el("button", "idcopy", row.id);
    idBtn.type = "button";
    idBtn.title = "Copy the artifact id";
    idBtn.addEventListener("click", function (event) {
      event.stopPropagation();
      copy(row.id, idBtn, row.id);
    });
    top.appendChild(idBtn);

    var badges = el("span", "arow-badges");
    /* Status first: a trashed artifact's public link is dead, and that is the
       single most important thing about the row. */
    if (row.status === "trashed") {
      badges.appendChild(badge("trashed \\u00b7 link dead", "proposed"));
    } else if (row.status === "final") {
      badges.appendChild(badge("final"));
    }
    if (row.proposed_count) {
      badges.appendChild(badge(
        row.proposed_count + (row.proposed_count === 1 ? " proposal" : " proposals"),
        "proposed"
      ));
    }
    if (row.accept_versions) { badges.appendChild(badge("accepting versions")); }
    if (row.protected) { badges.appendChild(badge("protected")); }
    if (row.webhooks_count) {
      badges.appendChild(badge(
        row.webhooks_count + (row.webhooks_count === 1 ? " webhook" : " webhooks")
      ));
    }
    if (row.head_version) { badges.appendChild(badge("head v" + row.head_version, "head")); }
    top.appendChild(badges);
    top.appendChild(el("span", "arow-date", String(row.updated_at || "").replace("T", " ")));

    var panel = el("div", "apanel");
    panel.hidden = true;

    function toggle() {
      var opening = panel.hidden;
      panel.hidden = !opening;
      item.classList.toggle("is-open", opening);
      caret.textContent = opening ? "\\u25be" : "\\u25b8";
      if (opening) { loadPanel(row, panel); }
    }

    top.addEventListener("click", toggle);
    top.addEventListener("keydown", function (event) {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        toggle();
      }
    });

    item.appendChild(top);
    item.appendChild(panel);
    return item;
  }

  /* ------------------------------------------------------- detail panel */

  async function loadPanel(row, panel) {
    panel.textContent = "";
    panel.appendChild(el("div", "loading", "loading versions\\u2026"));
    try {
      /* The JSON history (not ?format=html) carries the proposal metadata,
         and the auth headers make proposals visible to the owner. Every /a/
         path is addressed by share_id, which is what the public URL carries
         once the owner has rotated the link; older listings without the field
         fall back to the id, where the two are still the same thing.

         A trashed artifact has no public link at all — /a/{id}/versions is a
         404 by design — so its history is skipped and the panel opens on the
         restore/purge controls, which is all there is to do with it. */
      var data = row.status === "trashed"
        ? { versions: [], head_version: null }
        : await request(
            "/a/" + encodeURIComponent(row.share_id || row.id) + "/versions"
          );
      var invitations = [];
      try {
        var invited = await request(
          "/api/artifacts/" + encodeURIComponent(row.id) + "/invitations"
        );
        invitations = (invited && invited.invitations) || [];
      } catch (err) {
        /* Guests are a side panel, not the point of this view: an artifact
           whose invitations cannot be read still opens for moderation. */
      }
      renderPanel(row, panel, data, invitations);
    } catch (err) {
      panel.textContent = "";
      var box = el("p", "err", err.message);
      panel.appendChild(box);
    }
  }

  function renderPanel(row, panel, data, invitations) {
    panel.textContent = "";

    /* Two identifiers, two jobs: `id` is the internal handle every /api/*
       call addresses, `pubId` is the share id every /a/ URL carries. They are
       equal until the owner rotates the link. */
    var id = encodeURIComponent(row.id);
    var share = row.share_id || row.id;
    var pubId = encodeURIComponent(share);
    var head = data.head_version;
    var publicUrl = BASE + "/a/" + share;
    var trashed = row.status === "trashed";

    var errBox = el("p", "err");
    errBox.hidden = true;

    function refresh() { loadPanel(row, panel); }

    function action(label, kind, handler) {
      var btn = el("button", "btn btn-sm" + (kind ? " btn-" + kind : ""), label);
      btn.type = "button";
      btn.addEventListener("click", async function () {
        btn.disabled = true;
        setError(errBox, "");
        try {
          await handler();
        } catch (err) {
          setError(errBox, err.message);
        } finally {
          btn.disabled = false;
        }
      });
      return btn;
    }

    /* ---- artifact-level controls ---- */
    var controls = el("div", "acontrols");

    var toggleLabel = el("label", "switch");
    var checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = !!data.accept_versions;
    checkbox.addEventListener("change", async function () {
      checkbox.disabled = true;
      setError(errBox, "");
      try {
        await request("/api/artifacts/" + id, {
          method: "PUT",
          body: { accept_versions: checkbox.checked }
        });
        refresh();
      } catch (err) {
        checkbox.checked = !checkbox.checked;
        setError(errBox, err.message);
      } finally {
        checkbox.disabled = false;
      }
    });
    toggleLabel.appendChild(checkbox);
    toggleLabel.appendChild(el("span", null, "accept versions from other projects"));
    controls.appendChild(toggleLabel);

    var openLink = el("a", "btn btn-sm", "Open artifact");
    openLink.href = publicUrl;
    openLink.target = "_blank";
    openLink.rel = "noopener";
    controls.appendChild(openLink);

    /* The review UI: the same document with its inline comment threads. */
    var reviewLink = el("a", "btn btn-sm", "Review");
    reviewLink.href = publicUrl + "/review";
    reviewLink.target = "_blank";
    reviewLink.rel = "noopener";
    controls.appendChild(reviewLink);

    var copyBtn = el("button", "btn btn-sm", "Copy public URL");
    copyBtn.type = "button";
    copyBtn.addEventListener("click", function () {
      copy(publicUrl, copyBtn, "Copy public URL");
    });
    controls.appendChild(copyBtn);

    controls.appendChild(action("Serve latest", "", async function () {
      await request("/api/artifacts/" + id + "/head", {
        method: "PUT",
        body: { mode: "latest" }
      });
      refresh();
    }));

    /* Rotating mints a new share id, so every URL on this page — and every
       link anybody was ever sent — changes. The whole listing is reloaded
       afterwards rather than this one panel. */
    controls.appendChild(action("Rotate link", "danger", async function () {
      if (!window.confirm(
        "Rotate the public link of this artifact?\\n\\n" +
        "The current URL stops working immediately and there is no way back. " +
        "Everyone who should still have access needs the new link."
      )) { return; }
      var result = await request("/api/artifacts/" + id + "/rotate-link",
        { method: "POST" });
      showNewLink(result);
      reload();
    }));

    if (trashed) {
      controls.appendChild(action("Restore", "primary", async function () {
        await request("/api/artifacts/" + id + "/restore", { method: "POST" });
        reload();
      }));
    } else {
      controls.appendChild(action("Trash", "danger", async function () {
        if (!window.confirm(
          "Move this artifact to the trash?\\n\\n" +
          "Its public link stops resolving and new versions and comments are " +
          "frozen. You can restore it later on the same URL."
        )) { return; }
        await request("/api/artifacts/" + id, { method: "DELETE" });
        reload();
      }));
    }

    /* Purge is the one irreversible button in the studio, so it asks for the
       word rather than for a click: a mis-aimed Enter cannot trigger it. */
    controls.appendChild(action("Purge", "danger", async function () {
      var typed = window.prompt(
        "This permanently erases every version, comment thread and view " +
        "statistic of this artifact. It cannot be undone.\\n\\n" +
        "Type PURGE to confirm:"
      );
      if (String(typed || "").trim().toUpperCase() !== "PURGE") { return; }
      await request("/api/artifacts/" + id + "/purge", { method: "DELETE" });
      reload();
    }));

    panel.appendChild(controls);
    panel.appendChild(errBox);

    panel.appendChild(statsSection(id, action));
    panel.appendChild(webhookSection(row, id, action));
    panel.appendChild(invitationSection(row, id, action, invitations || []));

    /* ---- version table ---- */
    var versions = data.versions || [];
    if (!versions.length) {
      panel.appendChild(el("p", "empty",
        trashed ? "in the trash \\u2014 restore it to see its versions"
                : "no versions"));
      return;
    }

    var wrap = el("div", "table-wrap");
    var table = document.createElement("table");
    var thead = document.createElement("tr");
    ["version", "status", "author", "note", "created", "actions"].forEach(
      function (name) { thead.appendChild(el("th", null, name)); }
    );
    table.appendChild(thead);

    versions.forEach(function (version) {
      var n = version.version;
      var proposed = version.status === "proposed";
      var tr = document.createElement("tr");

      tr.appendChild(el("td", "mono", "v" + n));

      var statusCell = el("td", "arow-badges");
      statusCell.appendChild(badge(version.status || "live",
        proposed ? "proposed" : "live"));
      if (version.is_head) { statusCell.appendChild(badge("HEAD", "head")); }
      tr.appendChild(statusCell);

      var author = version.author || {};
      tr.appendChild(el("td", null,
        author.project_name || author.project_id || "unknown"));
      tr.appendChild(el("td", "vnote", version.note || "\\u2014"));
      tr.appendChild(el("td", "mono",
        String(version.created_at || "").replace("T", " ")));

      var actions = el("td", "vactions");

      actions.appendChild(action("View", "", async function () {
        if (proposed) {
          var body = await requestHtml("/a/" + pubId + "/v/" + n);
          openModal("proposed v" + n + " \\u2014 " + row.id, body);
        } else {
          window.open(publicUrl + "/v/" + n, "_blank", "noopener");
        }
      }));

      if (head !== null && head !== undefined && n !== head) {
        actions.appendChild(action("Diff vs head", "", async function () {
          var body = await requestHtml(
            "/a/" + pubId + "/diff/" + head + ".." + n + "?format=html"
          );
          openModal("diff v" + head + "..v" + n + " \\u2014 " + row.id, body);
        }));
      }

      if (proposed) {
        actions.appendChild(action("Promote", "primary", async function () {
          if (!window.confirm(
            "Promote v" + n + " to live? It becomes servable immediately."
          )) { return; }
          await request("/api/artifacts/" + id + "/versions/" + n + "/promote",
            { method: "POST" });
          refresh();
        }));
        actions.appendChild(action("Reject", "danger", async function () {
          if (!window.confirm(
            "Reject proposal v" + n + "? It is deleted permanently."
          )) { return; }
          await request("/api/artifacts/" + id + "/versions/" + n,
            { method: "DELETE" });
          refresh();
        }));
      } else {
        actions.appendChild(action("Pin here", "", async function () {
          await request("/api/artifacts/" + id + "/head", {
            method: "PUT",
            body: { mode: "pinned", version: n }
          });
          refresh();
        }));
        if (n !== head) {
          actions.appendChild(action("Delete", "danger", async function () {
            if (!window.confirm(
              "Delete version v" + n + "? This is permanent."
            )) { return; }
            await request("/api/artifacts/" + id + "/versions/" + n,
              { method: "DELETE" });
            refresh();
          }));
        }
      }

      tr.appendChild(actions);
      table.appendChild(tr);
    });

    wrap.appendChild(table);
    panel.appendChild(wrap);
  }

  /* ------------------------------------------------- panel sub-sections */

  function section(heading) {
    var box = el("div", "asec");
    box.appendChild(el("h4", null, heading));
    return box;
  }

  /* A copyable block for something the server will never show again. */
  function onceBlock(label, value) {
    var wrap = el("div", null);
    wrap.appendChild(el("p", "once-warn", label));
    wrap.appendChild(el("div", "once", value));
    var btn = el("button", "btn btn-sm btn-primary", "Copy link");
    btn.type = "button";
    btn.addEventListener("click", function () { copy(value, btn, "Copy link"); });
    wrap.appendChild(btn);
    return wrap;
  }

  function showNewLink(result) {
    var url = result.url || (BASE + "/a/" + result.share_id);
    openNote("new public link", [
      onceBlock("The previous link is dead as of now. The new one:", url),
      el("p", "hint", result.warning ||
        "Reshare this URL with everyone who should still have access.")
    ]);
  }

  /* ---- view statistics ---- */

  function statsSection(id, action) {
    var box = section("views");
    var out = el("div", null);
    out.appendChild(el("p", "hint",
      "Read counts per surface and per day. Numbers only \\u2014 no reader " +
      "identity, address or referrer is recorded."));

    box.appendChild(action("Load stats", "", async function () {
      var data = await request("/api/artifacts/" + id + "/stats");
      renderStats(out, data);
    }));
    box.appendChild(out);
    return box;
  }

  function renderStats(out, data) {
    out.textContent = "";
    var total = Number(data.total || 0);
    out.appendChild(el("div", "stat-total", total + (total === 1 ? " view" : " views")));

    var days = data.by_day || [];
    if (days.length) {
      var peak = days.reduce(function (top, day) {
        return Math.max(top, Number(day.count || 0));
      }, 1);
      var spark = el("div", "spark");
      days.forEach(function (day) {
        var bar = document.createElement("i");
        bar.style.height =
          Math.max(6, Math.round((Number(day.count || 0) / peak) * 100)) + "%";
        bar.title = day.day + ": " + day.count;
        spark.appendChild(bar);
      });
      out.appendChild(spark);

      /* The bars carry the shape; the list carries the numbers, newest first
         and short enough to read at a glance. */
      var recent = days.slice(-7).reverse().map(function (day) {
        return day.day + "  " + day.count;
      });
      out.appendChild(el("p", "mono hint", recent.join("   \\u00b7   ")));
    } else {
      out.appendChild(el("p", "hint", "nobody has opened this artifact yet"));
    }

    var kinds = data.by_kind || {};
    var names = Object.keys(kinds);
    if (names.length) {
      out.appendChild(el("p", "mono hint", names.map(function (kind) {
        return kind + " " + kinds[kind];
      }).join("   \\u00b7   ")));
    }
  }

  /* ---- webhooks ---- */

  function webhookSection(row, id, action) {
    var box = section("webhooks");
    var body = el("div", null);
    /* A webhook URL is a capability (a Slack hook's path *is* its
       credential), so the hub returns the list only in the PUT that sets it —
       GET /api/artifacts reports a count. That is why an artifact with hooks
       already configured starts out unknown here: the only honest edit is a
       replacement, until a save teaches this tab what the set is. */
    var hooks = row.webhooks_count ? null : [];

    function save(next) {
      return async function () {
        var result = await request("/api/artifacts/" + id, {
          method: "PUT",
          body: { webhooks: next }
        });
        hooks = result.webhooks || [];
        row.webhooks_count = hooks.length;
        render();
      };
    }

    function render() {
      body.textContent = "";

      if (hooks === null) {
        body.appendChild(el("p", "hint",
          "This artifact has " + row.webhooks_count + " webhook URL(s). The " +
          "hub never lists them again after they are set, so they cannot be " +
          "removed one by one here \\u2014 saving below replaces the whole set."));
      } else if (hooks.length) {
        var list = el("ul", "chips");
        hooks.forEach(function (url) {
          var chip = el("li", "chip");
          chip.appendChild(el("span", "chip-main", url));
          chip.appendChild(action("Remove", "danger", save(
            hooks.filter(function (other) { return other !== url; })
          )));
          list.appendChild(chip);
        });
        body.appendChild(list);
      } else {
        body.appendChild(el("p", "hint",
          "No webhooks. Add an https URL to be notified about new versions, " +
          "proposals and comments; a hooks.slack.com URL gets Slack's own " +
          "message shape."));
      }

      var line = el("div", "asec-row");
      var input = document.createElement("input");
      input.type = "url";
      input.placeholder = "https://hooks.slack.com/services/...";
      input.spellcheck = false;
      line.appendChild(input);
      line.appendChild(action(
        hooks === null ? "Replace all" : "Add",
        "",
        async function () {
          var url = input.value.trim();
          if (!url) { return; }
          if (hooks === null && !window.confirm(
            "Replace all " + row.webhooks_count + " existing webhook URL(s) " +
            "with this one?"
          )) { return; }
          await save((hooks || []).concat([url]))();
        }
      ));
      body.appendChild(line);
    }

    render();
    box.appendChild(body);
    return box;
  }

  /* ---- guest invitations ---- */

  function invitationSection(row, id, action, invitations) {
    var box = section("guests");
    var body = el("div", null);
    var list = invitations.slice();

    function render() {
      body.textContent = "";
      var live = list.filter(function (inv) { return !inv.revoked; });

      if (list.length) {
        var chips = el("ul", "chips");
        list.forEach(function (inv) {
          var chip = el("li", "chip" + (inv.revoked ? " is-off" : ""));
          chip.appendChild(el("span", "chip-main",
            inv.name + (inv.revoked ? "  (revoked)" : "")));
          chip.appendChild(el("span", "arow-date",
            String(inv.created_at || "").replace("T", " ")));
          if (!inv.revoked) {
            chip.appendChild(action("Revoke", "danger", async function () {
              if (!window.confirm(
                "Revoke the invitation for " + inv.name + "?\\n\\n" +
                "Their link stops working immediately. Comments they already " +
                "left stay, and every other guest keeps their access."
              )) { return; }
              await request("/api/artifacts/" + id + "/invitations/" +
                encodeURIComponent(inv.id), { method: "DELETE" });
              inv.revoked = true;
              render();
            }));
          }
          chips.appendChild(chip);
        });
        body.appendChild(chips);
      }

      body.appendChild(el("p", "hint",
        live.length
          ? live.length + " active invitation(s). A guest can comment, reply " +
            "and resolve their own threads \\u2014 nothing else."
          : "Invite someone without a Keboola account to comment. They get a " +
            "review link that works only for them and only for this artifact."));

      var line = el("div", "asec-row");
      var input = document.createElement("input");
      input.type = "text";
      input.maxLength = 80;
      input.placeholder = "who is this for? e.g. Jana (legal)";
      line.appendChild(input);
      line.appendChild(action("Invite", "primary", async function () {
        var name = input.value.trim();
        if (!name) { return; }
        var result = await request("/api/artifacts/" + id + "/invitations", {
          method: "POST",
          body: { name: name }
        });
        input.value = "";
        list.push({
          id: result.invitation_id,
          name: result.name,
          created_at: "",
          revoked: false
        });
        render();
        openNote("invitation for " + result.name, [
          onceBlock(
            "This link is shown once and cannot be recovered \\u2014 copy it " +
            "now and send it to " + result.name + ".",
            result.review_url
          ),
          el("p", "hint", result.warning ||
            "Anyone holding this link can comment as " + result.name +
            " until you revoke it.")
        ]);
      }));
      body.appendChild(line);
    }

    render();
    box.appendChild(body);
    return box;
  }

  /* ------------------------------------------------------------ sessions */

  function enterStudio(data) {
    show($("login"), false);
    show($("studio"), true);
    show($("logout"), true);
    var pill = $("project-badge");
    pill.textContent = "project " + (data.project_id || "?") + " \\u00b7 " + auth.stack;
    show(pill, true);
    renderArtifacts(data.artifacts || []);
  }

  function leaveStudio() {
    clearAuth();
    auth = null;
    $("artifacts").textContent = "";
    $("token").value = "";
    show($("studio"), false);
    show($("logout"), false);
    show($("project-badge"), false);
    show($("login"), true);
  }

  async function reload() {
    setError($("list-error"), "");
    show($("loading"), true);
    try {
      var data = await request("/api/artifacts");
      renderArtifacts(data.artifacts || []);
    } catch (err) {
      setError($("list-error"), err.message);
    } finally {
      show($("loading"), false);
    }
  }

  /* --------------------------------------------------------------- wiring */

  $("stack").addEventListener("change", function () {
    show($("custom-wrap"), $("stack").value === "__custom__");
  });

  $("login-form").addEventListener("submit", async function (event) {
    event.preventDefault();
    setError($("login-error"), "");

    var token = $("token").value.trim();
    var choice = $("stack").value;
    var stack = choice === "__custom__" ? $("custom").value.trim() : choice;
    if (!token) { setError($("login-error"), "Enter a Storage API token."); return; }
    if (!stack) { setError($("login-error"), "Enter the stack URL."); return; }

    var btn = $("login-btn");
    btn.disabled = true;
    auth = { token: token, stack: stack };
    try {
      var data = await request("/api/artifacts");
      storeAuth(auth);
      $("token").value = "";
      enterStudio(data);
    } catch (err) {
      auth = null;
      setError($("login-error"), err.message);
    } finally {
      btn.disabled = false;
    }
  });

  $("logout").addEventListener("click", leaveStudio);
  $("refresh").addEventListener("click", reload);
  $("modal-close").addEventListener("click", closeModal);
  $("modal").addEventListener("click", function (event) {
    if (event.target === $("modal")) { closeModal(); }
  });
  $("note-close").addEventListener("click", closeNote);
  $("note-modal").addEventListener("click", function (event) {
    if (event.target === $("note-modal")) { closeNote(); }
  });
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") { return; }
    if (!$("modal").hidden) { closeModal(); }
    if (!$("note-modal").hidden) { closeNote(); }
  });

  /* --------------------------------------------------------------- start */

  auth = loadAuth();
  if (auth) {
    var options = Array.prototype.map.call(
      $("stack").options, function (option) { return option.value; }
    );
    if (options.indexOf(auth.stack) !== -1) {
      $("stack").value = auth.stack;
    } else {
      $("stack").value = "__custom__";
      $("custom").value = auth.stack;
      show($("custom-wrap"), true);
    }
    show($("loading"), true);
    request("/api/artifacts").then(function (data) {
      show($("loading"), false);
      enterStudio(data);
    }, function (err) {
      show($("loading"), false);
      clearAuth();
      auth = null;
      setError($("login-error"), "That session is no longer valid: " + err.message);
    });
  }
})();
"""

#: Stack choices offered by the studio's dropdown, in the order shown. Kept as
#: the short, unambiguous aliases; anything else goes through "custom URL",
#: which the server validates exactly as it validates a curl call.
_ADMIN_STACKS = ("us", "gcp-us", "eu", "azure-eu", "gcp-eu")


def _page(title: str, extra_css: str, body: str) -> str:
    """Wrap a body fragment in the shared HTML skeleton."""
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{html.escape(title)}</title>\n"
        f"{_FONT_LINKS}"
        f"<style>{_CSS}{extra_css}</style>\n"
        "</head>\n<body>\n"
        f"{body}\n"
        "</body>\n</html>\n"
    )


def _term(title: str, body: str) -> str:
    """A terminal card. ``body`` is pre-escaped markup with optional spans."""
    return (
        f'<div class="term"><div class="term-bar">'
        '<span class="dot"></span><span class="dot"></span><span class="dot"></span>'
        f'<span class="term-title">{html.escape(title)}</span></div>'
        f"<pre><code>{body}</code></pre></div>"
    )


def _badge(text: str, kind: str = "") -> str:
    suffix = f" badge--{kind}" if kind else ""
    return f'<span class="badge{suffix}">{html.escape(text)}</span>'


def _card(heading: str, text: str) -> str:
    return (
        f'<div class="card"><h3>{html.escape(heading)}</h3>'
        f"<p>{text}</p></div>"
    )


def landing_page(base_url: str, service_version: str, github_url: str) -> str:
    """Render the public landing page: what the hub is and how to drive it."""
    base = html.escape(base_url.rstrip("/"))
    version = html.escape(service_version)
    repo = html.escape(github_url.rstrip("/"))

    hero_term = _term(
        "PUBLISH → PUBLIC URL",
        '<span class="p">$</span> curl -sX POST '
        f'<span class="s">"{base}/api/artifacts"</span> \\\n'
        '    -H <span class="s">"X-StorageApi-Token: $KBC_TOKEN"</span> \\\n'
        '    -H <span class="s">"X-Storage-Stack: eu"</span> \\\n'
        '    -d <span class="s">\'{"markdown": "# Q3 review\\n\\nShipped."}\'</span>\n'
        '<span class="c">'
        '{"id": "aBcD3fGhIjKlMnOpQrSt", "version": 1, "head_version": 1,\n'
        f' "url": "{base}/a/aBcD3fGhIjKlMnOpQrSt"}}</span>\n'
        '<span class="p">$</span> open '
        f'<span class="k">{base}/a/aBcD3fGhIjKlMnOpQrSt</span>',
    )

    features = "".join(
        [
            _card(
                "publish anything",
                "HTML served as-is, Markdown rendered by the built-in template, "
                "or a git repository — public, or private with a transient "
                "access token that is used for the clone and never stored.",
            ),
            _card(
                "urls are capabilities",
                "Every artifact gets an unguessable id. There is no public "
                "listing and no index, and every read carries "
                "<code>X-Robots-Tag: noindex</code>.",
            ),
            _card(
                "optional password",
                "PBKDF2-hashed, with a browser unlock form and an "
                "<code>X-Artifact-Password</code> header for machines.",
            ),
            _card(
                "community versioning",
                "Every update is a new version. Open an artifact to "
                "contributions and other projects can submit proposals for you "
                "to review, diff and promote.",
            ),
            _card(
                "built for agents",
                f'<a href="{base}/context">/context</a> is a machine-readable '
                f'manifest, <a href="{base}/skill">/skill</a> is a SKILL.md an '
                f'agent reads to publish unassisted, <a href="{base}/agent">'
                "/agent</a> is a drop-in Claude Code subagent, and every read "
                "endpoint answers JSON or raw HTML.",
            ),
            _card(
                "moderate in the browser",
                f'<a href="{base}/admin">/admin</a> is an admin studio for '
                "owners: list your artifacts, read proposals, diff them "
                "against the head, then promote, reject, pin or delete. Your "
                "token never leaves the tab.",
            ),
            _card(
                "review and comment",
                "Every artifact has a review UI at "
                "<code>/a/{id}/review</code>: highlight a passage, leave an "
                "inline comment, reply and resolve — and export the whole "
                "trail as an Obsidian vault.",
            ),
            _card(
                "your project, your content",
                "The canonical copy of what you publish is a Storage File in "
                "your own Keboola project. The hub keeps only a serving copy, "
                "so a restart rebuilds everything from Storage.",
            ),
        ]
    )

    publish_term = _term(
        "POST /api/artifacts",
        '<span class="c"># Markdown — GFM tables, task lists, mermaid, '
        "highlighting</span>\n"
        f'<span class="p">$</span> curl -sX POST <span class="s">"{base}/api/artifacts"</span> \\\n'
        '    -H <span class="s">"X-StorageApi-Token: $KBC_TOKEN"</span> \\\n'
        '    -H <span class="s">"X-Storage-Stack: eu"</span> \\\n'
        '    -H <span class="s">"Content-Type: application/json"</span> \\\n'
        '    -d <span class="s">\'{"markdown": "# Report\\n\\nBody.", '
        '"title": "Report", "accept_versions": true}\'</span>\n'
        "\n"
        '<span class="c"># A git repository (add git_token for a private one)</span>\n'
        f'<span class="p">$</span> curl -sX POST <span class="s">"{base}/api/artifacts"</span> \\\n'
        '    -H <span class="s">"X-StorageApi-Token: $KBC_TOKEN"</span> \\\n'
        '    -H <span class="s">"X-Storage-Stack: eu"</span> \\\n'
        '    -d <span class="s">\'{"git_url": "https://github.com/org/repo", '
        '"git_path": "docs/report.md"}\'</span>',
    )

    versions_term = _term(
        "VERSIONING",
        '<span class="c"># Submit a version to someone else\'s artifact</span>\n'
        f'<span class="p">$</span> curl -sX POST <span class="s">"{base}/api/artifacts/$ID/versions"</span> \\\n'
        '    -H <span class="s">"X-StorageApi-Token: $KBC_TOKEN"</span> \\\n'
        '    -H <span class="s">"X-Storage-Stack: eu"</span> \\\n'
        '    -d <span class="s">\'{"markdown": "# Report\\n\\nFixed the totals.", '
        '"note": "fix Q3 totals"}\'</span>\n'
        '<span class="c">{"version": 2, "status": "proposed"}</span>\n'
        "\n"
        '<span class="c"># Owner reviews, diffs, and promotes it</span>\n'
        f'<span class="p">$</span> open <span class="k">{base}/a/$ID/versions?format=html</span>\n'
        f'<span class="p">$</span> open <span class="k">{base}/a/$ID/diff/1..2</span>\n'
        f'<span class="p">$</span> curl -sX POST <span class="s">"{base}/api/artifacts/$ID/versions/2/promote"</span> \\\n'
        '    -H <span class="s">"X-StorageApi-Token: $KBC_TOKEN"</span> '
        '-H <span class="s">"X-Storage-Stack: eu"</span>',
    )

    agents_term = _term(
        "FOR AGENTS",
        '<span class="c"># Install the ready-made Claude Code subagent</span>\n'
        f'<span class="p">$</span> install -d ~/.claude/agents &amp;&amp; curl -fsSL '
        f'<span class="s">{base}/agent</span> -o <span class="k">~/.claude/agents/artifact-hub.md</span>\n'
        "\n"
        '<span class="c"># Or hand any agent the skill and the manifest</span>\n'
        f'<span class="p">$</span> curl -s <span class="s">{base}/skill</span>\n'
        f'<span class="p">$</span> curl -s <span class="s">{base}/context</span> | jq .endpoints',
    )

    # Hoisted out of the f-string below: Python 3.11 (the app runtime) does
    # not allow backslashes inside f-string expressions (PEP 701 is 3.12+).
    headers_term = _term(
        "HEADERS",
        '<span class="k">X-StorageApi-Token</span>: &lt;your Keboola Storage API token&gt;\n'
        '<span class="k">X-Storage-Stack</span>: us | gcp-us | eu | azure-eu | gcp-eu\n'
        "                 | https://*.keboola.com",
    )

    return _page(
        "KBC Artifact Hub",
        "",
        f"""<main>
<section class="hero">
<h1>KBC Artifact Hub</h1>
<p class="lead">Turn a document into a public URL with one curl call. Any
Keboola Storage API token, on any stack, is the only credential you need.</p>
<div class="hero-meta">
{_badge(f"kbc-artifact-hub v{service_version}", "version")}
{_badge("no sign-up")}
{_badge("no build step")}
</div>
{hero_term}
<div class="hero-links">
<a class="primary" href="{base}/admin">Admin studio</a>
<a href="{repo}">GitHub repo</a>
<a href="{base}/docs">/docs</a>
<a href="{base}/skill">/skill</a>
<a href="{base}/agent">/agent</a>
<a href="{base}/context">/context</a>
<a href="{base}/changelog">Changelog</a>
</div>
</section>

<h2 class="label">what it does</h2>
<div class="grid">{features}</div>

<h2 class="label">authentication</h2>
<p>Everything under <code>/api/artifacts</code> is authenticated with two
headers. The hub verifies the token against your own stack's
<code>/v2/storage/tokens/verify</code> endpoint and never stores it.</p>
{headers_term}
<p>Ownership is the pair (stack, project id). Updating, deleting, promoting and
pinning all require a token from the project that published the artifact.</p>

<h2 class="label">quick start</h2>
{publish_term}
<p class="note">Add <code>"password": "secret"</code> to protect the artifact,
or <code>"accept_versions": true</code> to let other projects submit versions
for your review.</p>

<h2 class="label">community versioning</h2>
<p>Every update is a new version, and nothing is overwritten. Owners publish
live versions; other projects submit <strong>proposals</strong> that stay
private to their author and the owner until the owner promotes one. The head
pointer decides what <code>/a/{{id}}</code> serves — the newest live version,
or one you pin.</p>
{versions_term}

<h2 class="label">for agents</h2>
<p><a href="{base}/agent">/agent</a> serves a ready-to-install Claude Code
subagent definition; drop it in <code>~/.claude/agents/</code> and the agent
knows how to publish, update and moderate artifacts on its own.
<a href="{base}/skill">/skill</a> is the same knowledge as a SKILL.md for any
other agent runtime, and <a href="{base}/context">/context</a> is the
machine-readable manifest of endpoints, limits and the auth model.</p>
{agents_term}

<h2 class="label">moderating in the browser</h2>
<p>Prefer clicking to curling? <a href="{base}/admin">/admin</a> is a
single-page admin studio for artifact owners: sign in with your Storage token,
see every artifact your project owns with its pending proposals, read a
proposal, diff it against the head, then promote, reject, pin or delete. The
token stays in that browser tab — the studio is a static page that calls the
same public API.</p>

<h2 class="label">reading an artifact</h2>
<div class="table-wrap"><table>
<tr><th>Method</th><th>Path</th><th>Returns</th></tr>
<tr><td class="mono">GET</td><td><code>/a/{{id}}</code></td><td>The head version, rendered (or the unlock form)</td></tr>
<tr><td class="mono">GET</td><td><code>/a/{{id}}/v/{{n}}</code></td><td>One specific version</td></tr>
<tr><td class="mono">GET</td><td><code>/a/{{id}}/versions</code></td><td>Version history as JSON, or <code>?format=html</code> for a picker page</td></tr>
<tr><td class="mono">GET</td><td><code>/a/{{id}}/diff/{{a}}..{{b}}</code></td><td>Side-by-side diff; <code>?format=unified</code> or <code>json</code> for machines</td></tr>
<tr><td class="mono">GET</td><td><code>/a/{{id}}/raw</code></td><td>The HTML itself, no chrome</td></tr>
<tr><td class="mono">GET</td><td><code>/a/{{id}}/source</code></td><td>Original submitted source (Markdown or HTML)</td></tr>
<tr><td class="mono">GET</td><td><code>/a/{{id}}/meta</code></td><td>Public metadata JSON, no owner details</td></tr>
<tr><td class="mono">GET</td><td><code>/a/{{id}}/review</code></td><td>Two-pane review UI: the document plus its inline comment threads</td></tr>
<tr><td class="mono">GET</td><td><code>/a/{{id}}/comments</code></td><td>Every comment thread as JSON</td></tr>
<tr><td class="mono">GET</td><td><code>/a/{{id}}/export/markdown</code></td><td>The head version's source as a downloadable file</td></tr>
<tr><td class="mono">GET</td><td><code>/a/{{id}}/export/vault</code></td><td>A ready-to-open Obsidian vault (ZIP) of the whole history</td></tr>
<tr><td class="mono">POST</td><td><code>/a/{{id}}/unlock</code></td><td>Password form target; sets a signed, path-scoped cookie</td></tr>
</table></div>
<p class="note">Protected artifacts accept the password over the
<code>X-Artifact-Password</code> header on every read;
<code>/meta</code> stays public either way. Proposed versions are visible only
to the artifact owner and the version's author, who authenticate with the same
two management headers.</p>

<h2 class="label">managing your artifacts</h2>
<div class="table-wrap"><table>
<tr><th>Method</th><th>Path</th><th>Purpose</th></tr>
<tr><td class="mono">POST</td><td><code>/api/artifacts</code></td><td>Publish a new artifact</td></tr>
<tr><td class="mono">PUT</td><td><code>/api/artifacts/{{id}}</code></td><td>Add a live version, or change password / <code>accept_versions</code></td></tr>
<tr><td class="mono">GET</td><td><code>/api/artifacts</code></td><td>List your project's artifacts</td></tr>
<tr><td class="mono">DELETE</td><td><code>/api/artifacts/{{id}}</code></td><td>Delete every version and the meta record</td></tr>
<tr><td class="mono">POST</td><td><code>/api/artifacts/{{id}}/versions</code></td><td>Submit a version (live for the owner, proposed for everyone else)</td></tr>
<tr><td class="mono">POST</td><td><code>/api/artifacts/{{id}}/versions/{{n}}/promote</code></td><td>Owner approves a proposal</td></tr>
<tr><td class="mono">DELETE</td><td><code>/api/artifacts/{{id}}/versions/{{n}}</code></td><td>Owner removes a version; a contributor withdraws their own proposal</td></tr>
<tr><td class="mono">PUT</td><td><code>/api/artifacts/{{id}}/head</code></td><td>Serve the latest live version, or pin one</td></tr>
<tr><td class="mono">POST</td><td><code>/api/artifacts/{{id}}/comments</code></td><td>Open an inline comment thread on a quoted passage</td></tr>
<tr><td class="mono">POST</td><td><code>/api/artifacts/{{id}}/comments/{{tid}}/replies</code></td><td>Reply in a thread</td></tr>
<tr><td class="mono">POST</td><td><code>/api/artifacts/{{id}}/comments/{{tid}}/resolve</code></td><td>Resolve a thread, or reopen it with <code>{{"resolved": false}}</code></td></tr>
<tr><td class="mono">DELETE</td><td><code>/api/artifacts/{{id}}/comments/{{tid}}</code></td><td>Delete a thread (owner, or its author)</td></tr>
</table></div>

<footer>
<span>kbc-artifact-hub v{version}</span>
<span class="spacer"></span>
<a href="{base}/admin">Admin studio</a>
<a href="{base}/agent">/agent</a>
<a href="{base}/changelog">Changelog</a>
<a href="{repo}">github.com/padak/kbc_ai_artifact</a>
<a href="{base}/health">/health</a>
</footer>
</main>""",
    )


def admin_page(base_url: str, service_version: str, github_url: str) -> str:
    """Render the owner/moderation studio served at ``/admin``.

    The page is public and the server learns nothing from serving it: the
    visitor's Storage token is entered in the browser, kept in a JS closure and
    in ``sessionStorage`` (per-tab, cleared when the tab closes), and attached
    to the very same API calls a terminal would make with ``curl``. Nothing is
    proxied, stored or logged on this side — ``/admin`` is a static document.
    """
    base = html.escape(base_url.rstrip("/"))
    version = html.escape(service_version)
    repo = html.escape(github_url.rstrip("/"))

    options = "".join(
        f'<option value="{html.escape(alias)}">{html.escape(alias)}</option>'
        for alias in _ADMIN_STACKS
    )
    options += '<option value="__custom__">custom URL…</option>'

    body = f"""<main>
<header class="ahead">
<div>
<h1>Artifact Hub · Admin studio</h1>
<p class="lead">Review, diff, promote and prune the versions of the artifacts
your Keboola project owns — then watch their traffic, rotate a link that went
to the wrong person, invite a guest who has no Keboola account, wire up
webhooks, and trash or purge what is done. In the browser, over the same API
you can drive with curl.</p>
</div>
<div class="ahead-right">
<span class="badge badge--version" id="project-badge" hidden></span>
<button type="button" class="btn" id="logout" hidden>Log out</button>
</div>
</header>

<div class="loading" id="loading" hidden>loading…</div>

<section id="login">
<h2 class="label">sign in</h2>
<div class="card login-card">
<form id="login-form" autocomplete="off">
<label for="token">Storage API token</label>
<input type="password" id="token" name="token" autocomplete="off"
  spellcheck="false" placeholder="your Keboola Storage API token">
<label for="stack">Stack</label>
<select id="stack" name="stack">{options}</select>
<div id="custom-wrap" hidden>
<label for="custom">Stack URL</label>
<input type="text" id="custom" name="custom" spellcheck="false"
  placeholder="https://connection.keboola.com">
</div>
<button type="submit" class="btn btn-primary btn-wide" id="login-btn">Open studio</button>
<p class="err" id="login-error" hidden></p>
</form>
<p class="hint">Your token stays in this browser tab. Every call goes straight
to the same API you can use with curl.</p>
<p class="hint">It is held in <code>sessionStorage</code> only — never in a
cookie, never in the URL, never in any storage that outlives this tab — so a
reload keeps you signed in and closing the tab forgets the token.
<strong>Log out</strong> clears it right away.</p>
</div>
</section>

<section id="studio" hidden>
<h2 class="label">artifacts owned by this project</h2>
<div class="toolbar">
<button type="button" class="btn" id="refresh">Refresh</button>
<span class="spacer"></span>
<span class="mono" id="count"></span>
</div>
<p class="err" id="list-error" hidden></p>
<ul class="alist" id="artifacts"></ul>
<p class="empty" id="empty" hidden>No artifacts yet. Publish one with
<code>POST /api/artifacts</code> and it shows up here.</p>
</section>

<div class="modal" id="modal" hidden>
<div class="modal-box">
<div class="modal-bar">
<span class="modal-title mono" id="modal-title"></span>
<span class="spacer"></span>
<button type="button" class="btn" id="modal-close">Close</button>
</div>
<iframe id="modal-frame" title="artifact preview"
  sandbox="allow-scripts allow-popups"></iframe>
</div>
</div>

<div class="modal" id="note-modal" hidden>
<div class="modal-box" style="height:auto;max-width:38rem">
<div class="modal-bar">
<span class="modal-title mono" id="note-title"></span>
<span class="spacer"></span>
<button type="button" class="btn" id="note-close">Close</button>
</div>
<div class="apanel" id="note-body"></div>
</div>
</div>

<footer>
<span>kbc-artifact-hub v{version}</span>
<span class="spacer"></span>
<a href="{base}/">hub home</a>
<a href="{base}/docs">/docs</a>
<a href="{repo}">source</a>
</footer>
</main>
<script>window.HUB_BASE = "{base}";</script>
<script>"""

    return _page(
        "Artifact Hub · Admin studio",
        _CONTROLS_CSS + _ADMIN_CSS,
        body + _ADMIN_JS + "</script>",
    )


def artifact_frame_page(title: str, artifact_html: str) -> str:
    """Wrap one artifact's built HTML in a zero-chrome sandboxed iframe.

    Published artifacts are publisher-controlled documents that may run
    arbitrary JavaScript. Serving them directly on the hub's own origin put
    that script in the same origin as ``/admin`` and ``/a/{id}/review``, whose
    ``sessionStorage`` holds a visitor's Keboola Storage token — one artifact
    could read another visitor's credential. Here the document is handed to
    the browser as the ``srcdoc`` of an iframe sandboxed *without*
    ``allow-same-origin``, so it runs in an opaque origin: no access to this
    origin's storage, cookies or DOM, while scripts, forms, popups and
    downloads inside the document keep working.

    ``srcdoc`` carries the whole document as an attribute *value*, so it is
    escaped with ``quote=True`` — an unescaped ``"`` in the artifact would
    otherwise close the attribute and put artifact markup back at top level.

    Visually this is a no-op: the frame has no border and fills the viewport,
    so a reader sees exactly what they saw before. Machines that want the
    bytes themselves keep using ``/a/{id}/raw``.
    """
    safe_title = html.escape(title or "Artifact", quote=True)
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{safe_title}</title>\n"
        "<style>html,body{margin:0;padding:0;border:0;width:100%;height:100%;"
        "overflow:hidden}"
        "iframe{margin:0;padding:0;border:0;width:100%;height:100vh;"
        "display:block}</style>\n"
        "</head>\n<body>\n"
        f'<iframe title="{safe_title}" '
        'sandbox="allow-scripts allow-popups allow-forms allow-downloads" '
        f'srcdoc="{html.escape(artifact_html, quote=True)}"></iframe>\n'
        "</body>\n</html>\n"
    )


def unlock_page(artifact_id: str, error: str | None) -> str:
    """Render the password form for a protected artifact."""
    safe_id = html.escape(artifact_id)
    error_html = f'<p class="error">&gt; {html.escape(error)}</p>' if error else ""
    return _page(
        "Password required",
        _UNLOCK_CSS,
        f"""<div class="gate">
<div class="rule">locked artifact</div>
<div class="card">
<h1>Password required</h1>
<p>This artifact is protected. Enter its password to continue.</p>
<form method="post" action="/a/{safe_id}/unlock">
<label for="password">Password</label>
<input type="password" id="password" name="password"
  autocomplete="current-password" autofocus required>
<button type="submit">Unlock</button>
{error_html}
</form>
<p class="hint">Machines send the password in the
<code>X-Artifact-Password</code> header instead.</p>
</div>
</div>""",
    )


def _version_row(
    base: str,
    artifact_id: str,
    version_meta: dict,
    older: int | None,
) -> str:
    """One row of the version picker; ``older`` is the adjacent older version."""
    number = version_meta.get("version")
    status = str(version_meta.get("status") or "live")
    is_head = bool(version_meta.get("is_head"))
    title = str(version_meta.get("title") or "untitled")
    note = version_meta.get("note")
    created = str(version_meta.get("created_at") or "")
    size = version_meta.get("size_bytes")
    author = version_meta.get("author") or {}
    project = author.get("project_name") or author.get("project_id") or "unknown"

    badges = _badge(status, "proposed" if status == "proposed" else "live")
    if is_head:
        badges += _badge("head", "head")

    meta_bits = [f"by {html.escape(str(project))}"]
    if created:
        meta_bits.append(html.escape(created))
    if isinstance(size, int):
        meta_bits.append(f"{size:,} bytes".replace(",", " "))
    source_type = version_meta.get("source_type")
    if source_type:
        meta_bits.append(html.escape(str(source_type)))

    note_html = (
        f'<p class="vrow-note">{html.escape(str(note))}</p>' if note else ""
    )

    links = [f'<a href="{base}/a/{artifact_id}/v/{number}">view v{number}</a>']
    if older is not None:
        links.append(
            f'<a href="{base}/a/{artifact_id}/diff/{older}..{number}">'
            f"diff v{older}..v{number}</a>"
        )

    return (
        f'<li class="vrow{" is-head" if is_head else ""}">'
        f'<div class="vrow-top"><span class="vrow-n">v{number}</span>'
        f'<span class="vrow-title">{html.escape(title)}</span>{badges}</div>'
        f'<div class="vrow-meta">{"".join(f"<span>{bit}</span>" for bit in meta_bits)}</div>'
        f"{note_html}"
        f'<div class="vrow-links">{"".join(links)}</div>'
        "</li>"
    )


def versions_page(
    base_url: str,
    artifact_id: str,
    versions: list[dict],
    head_version: int | None,
    accept_versions: bool,
    protected: bool,
) -> str:
    """Render the human-facing version history for one artifact.

    ``versions`` is the store's public metadata list, newest first, as returned
    by :meth:`~src.store.ArtifactStore.list_versions`.
    """
    base = html.escape(base_url.rstrip("/"))
    safe_id = html.escape(artifact_id)

    rows = "".join(
        _version_row(
            base,
            safe_id,
            version_meta,
            versions[index + 1].get("version") if index + 1 < len(versions) else None,
        )
        for index, version_meta in enumerate(versions)
    )
    if not rows:
        rows = '<li class="vrow"><span class="empty">no versions</span></li>'

    proposed = sum(1 for v in versions if v.get("status") == "proposed")
    facts = [
        _badge(f"{len(versions)} versions"),
        _badge(f"head v{head_version}" if head_version else "no live head", "head"),
    ]
    if proposed:
        facts.append(_badge(f"{proposed} proposed", "proposed"))
    facts.append(
        _badge("open to contributions" if accept_versions else "owner only")
    )
    if protected:
        facts.append(_badge("password protected"))

    submit = ""
    if accept_versions:
        submit = (
            '<h2 class="label">submit a version</h2>'
            "<p>Anyone with a Keboola Storage API token can propose a new "
            "version. Proposals stay private to you and the owner until the "
            "owner promotes one.</p>"
            + _term(
                "POST /api/artifacts/…/versions",
                f'<span class="p">$</span> curl -sX POST '
                f'<span class="s">"{base}/api/artifacts/{safe_id}/versions"</span> \\\n'
                '    -H <span class="s">"X-StorageApi-Token: $KBC_TOKEN"</span> \\\n'
                '    -H <span class="s">"X-Storage-Stack: eu"</span> \\\n'
                '    -H <span class="s">"Content-Type: application/json"</span> \\\n'
                '    -d <span class="s">\'{"markdown": "# Updated\\n\\n...", '
                '"note": "what changed"}\'</span>',
            )
        )

    return _page(
        f"Versions — {artifact_id}",
        _VERSIONS_CSS,
        f"""<main>
<div class="vhead"><h1>Version history</h1></div>
<p class="lead"><code>{safe_id}</code></p>
<div class="hero-meta">{"".join(facts)}</div>
<div class="hero-links">
<a class="primary" href="{base}/a/{safe_id}">open head version</a>
<a href="{base}/a/{safe_id}/review">review &amp; comment</a>
<a href="{base}/a/{safe_id}/versions">JSON</a>
<a href="{base}/a/{safe_id}/meta">metadata</a>
</div>

<h2 class="label">versions</h2>
<ul class="vlist">{rows}</ul>
{submit}

<footer>
<span>kbc-artifact-hub</span>
<span class="spacer"></span>
<a href="{base}/">hub home</a>
</footer>
</main>""",
    )


# --------------------------------------------------------------------------
# Review UI (/a/{id}/review)
# --------------------------------------------------------------------------

#: Review-only styles: the two-pane frame, the thread cards and the composer.
#: Buttons and status lines come from :data:`_CONTROLS_CSS`.
_REVIEW_CSS = """
html, body { height: 100%; }
html { background-image: none; }

.rv { display: flex; flex-direction: column; height: 100vh; }

.rv-top { display: flex; align-items: center; flex-wrap: wrap; gap: .5rem;
  padding: .5rem .85rem; border-bottom: 1px solid var(--line);
  background: var(--panel); }
.rv-brand { font-family: var(--font-mono); font-weight: 700; font-size: .88rem;
  letter-spacing: -.01em; }
.rv-top .spacer { flex: 1; }

.rv-body { flex: 1; display: flex; min-height: 0; }
.rv-doc { flex: 1; min-width: 0; background: #ffffff; }
.rv-doc iframe { width: 100%; height: 100%; border: 0; background: #ffffff; }

.rv-side { width: 25rem; max-width: 45vw; flex: none; overflow-y: auto;
  border-left: 1px solid var(--line); background: var(--panel);
  padding: .85rem .9rem 2rem; }
.rv-side h2 { font-family: var(--font-mono); font-size: .72rem;
  letter-spacing: .16em; text-transform: uppercase; color: var(--muted);
  margin: 1.1rem 0 .5rem; }
.rv-side h2:first-child { margin-top: 0; }

.rv-hint { font-size: .82rem; color: var(--muted); margin: .3rem 0 0; }
.rv-quote { font-family: var(--font-mono); font-size: .78rem; color: var(--ink-2);
  border-left: 2px solid var(--accent); padding: .25rem .55rem; margin: 0 0 .5rem;
  background: var(--accent-soft); border-radius: 0 6px 6px 0;
  overflow-wrap: anywhere; }

.rv-field { width: 100%; padding: .5rem .6rem; font-family: var(--font-sans);
  font-size: .86rem; color: var(--ink); background: var(--paper);
  border: 1px solid var(--line); border-radius: 8px; }
textarea.rv-field { min-height: 5rem; resize: vertical; }
.rv-side label { display: block; font-family: var(--font-mono); font-size: .68rem;
  letter-spacing: .12em; text-transform: uppercase; color: var(--muted);
  margin: .6rem 0 .3rem; }
.rv-actions { display: flex; flex-wrap: wrap; gap: .35rem; margin-top: .5rem; }

.rv-threads { list-style: none; margin: .3rem 0 0; padding: 0; }
.rv-thread { border: 1px solid var(--line); border-radius: var(--radius);
  background: var(--paper); padding: .6rem .7rem; margin-bottom: .5rem; }
.rv-thread.is-active { border-color: var(--accent); }
.rv-thread.is-resolved { opacity: .72; }
.rv-thread-top { display: flex; flex-wrap: wrap; align-items: center;
  gap: .35rem; margin-bottom: .35rem; }
.rv-who { font-family: var(--font-mono); font-size: .74rem; color: var(--ink); }
.rv-when { font-family: var(--font-mono); font-size: .7rem; color: var(--muted); }
.rv-text { font-size: .88rem; color: var(--ink-2); margin: .35rem 0 0;
  overflow-wrap: anywhere; white-space: pre-wrap; }
.rv-replies { list-style: none; margin: .45rem 0 0; padding: 0 0 0 .6rem;
  border-left: 2px solid var(--line); }
.rv-replies li { margin-bottom: .35rem; }
.rv-orphan { font-family: var(--font-mono); font-size: .7rem; color: var(--proposed); }
"""

#: The script injected into the artifact HTML before it is loaded into the
#: sandboxed iframe.
#:
#: It runs in an **opaque origin** (the iframe declares
#: ``sandbox="allow-scripts allow-popups"`` with no ``allow-same-origin``), so
#: it can never read the shell's DOM, cookies or ``sessionStorage`` — the
#: Storage token is structurally out of reach of anything an artifact author
#: wrote. Its only channel to the shell is ``postMessage``:
#:
#: - out: ``ah-ready``, ``ah-select`` (a TextQuoteSelector for the current
#:   selection), ``ah-open`` (a highlight was clicked) and ``ah-anchored``
#:   (which thread IDs actually matched this rendering),
#: - in: ``ah-anchors`` — the quotes to highlight.
#:
#: Served to the browser inside a ``<script type="text/plain">`` element and
#: injected by the shell as a real script, so it must never contain the closing
#: script tag sequence.
_ANNOTATION_JS = """
(function () {
  "use strict";

  var MAX_EXACT = 2000;
  var CTX = 32;
  var ATTR = "data-ah-tid";

  var style = document.createElement("style");
  style.textContent =
    "mark[" + ATTR + "]{background:rgba(250,204,21,.35);color:inherit;" +
    "outline:2px solid #f59e0b;outline-offset:1px;border-radius:2px;" +
    "cursor:pointer}" +
    "mark[" + ATTR + "]:hover{background:rgba(250,204,21,.62)}";
  (document.head || document.documentElement).appendChild(style);

  function post(message) {
    try { parent.postMessage(message, "*"); } catch (err) { /* detached */ }
  }

  /* The rendered document as one string, plus the text nodes that built it.
     Recomputed on demand: wrapping a quote in <mark> keeps the text identical
     but rearranges the nodes. */
  function flatten() {
    var root = document.body || document.documentElement;
    var parts = [];
    var text = "";
    if (!root) { return { text: text, parts: parts }; }
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    var node;
    while ((node = walker.nextNode())) {
      var tag = node.parentNode ? node.parentNode.nodeName : "";
      if (tag === "SCRIPT" || tag === "STYLE" || tag === "NOSCRIPT") { continue; }
      var value = node.nodeValue || "";
      if (!value) { continue; }
      parts.push({ node: node, start: text.length, end: text.length + value.length });
      text += value;
    }
    return { text: text, parts: parts };
  }

  function offsetOf(flat, container, offset) {
    for (var i = 0; i < flat.parts.length; i++) {
      if (flat.parts[i].node === container) { return flat.parts[i].start + offset; }
    }
    return -1;
  }

  /* Longest common run of two strings, from the start or from the end. */
  function overlap(a, b, fromEnd) {
    var limit = Math.min(a.length, b.length);
    var i = 0;
    while (i < limit) {
      var ca = fromEnd ? a.charAt(a.length - 1 - i) : a.charAt(i);
      var cb = fromEnd ? b.charAt(b.length - 1 - i) : b.charAt(i);
      if (ca !== cb) { break; }
      i += 1;
    }
    return i;
  }

  /* Offset of the best occurrence of spec.exact, or -1. Ties between repeated
     quotes are broken by how much of the recorded prefix/suffix matches. */
  function locate(text, spec) {
    var exact = String(spec.exact || "");
    if (!exact) { return -1; }
    var best = -1;
    var bestScore = -1;
    var from = 0;
    for (;;) {
      var at = text.indexOf(exact, from);
      if (at < 0) { break; }
      var before = text.slice(Math.max(0, at - CTX), at);
      var after = text.slice(at + exact.length, at + exact.length + CTX);
      var score = overlap(before, String(spec.prefix || ""), true) +
        overlap(after, String(spec.suffix || ""), false);
      if (score > bestScore) { bestScore = score; best = at; }
      from = at + 1;
    }
    return best;
  }

  function rangeFor(flat, start, end) {
    var range = document.createRange();
    var started = false;
    for (var i = 0; i < flat.parts.length; i++) {
      var part = flat.parts[i];
      if (!started && start >= part.start && start < part.end) {
        range.setStart(part.node, start - part.start);
        started = true;
      }
      if (started && end > part.start && end <= part.end) {
        range.setEnd(part.node, end - part.start);
        return range;
      }
    }
    return null;
  }

  function clearMarks() {
    var marks = document.querySelectorAll("mark[" + ATTR + "]");
    for (var i = 0; i < marks.length; i++) {
      var mark = marks[i];
      var parent = mark.parentNode;
      if (!parent) { continue; }
      while (mark.firstChild) { parent.insertBefore(mark.firstChild, mark); }
      parent.removeChild(mark);
      if (parent.normalize) { parent.normalize(); }
    }
  }

  function wrap(range, tid) {
    var mark = document.createElement("mark");
    mark.setAttribute(ATTR, tid);
    try {
      range.surroundContents(mark);
      return true;
    } catch (err) {
      /* The quote crosses element boundaries; rebuild it instead. */
    }
    try {
      mark.appendChild(range.extractContents());
      range.insertNode(mark);
      return true;
    } catch (err2) {
      return false;
    }
  }

  /* Highlight every quote we can still find. A quote that no longer matches
     this rendering is skipped silently; the shell learns which ones anchored
     from the ah-anchored reply and labels the rest. */
  function applyAnchors(list) {
    clearMarks();
    var done = [];
    for (var i = 0; i < list.length; i++) {
      var spec = list[i];
      if (!spec || !spec.tid || !spec.exact) { continue; }
      var flat = flatten();
      var at = locate(flat.text, spec);
      if (at < 0) { continue; }
      var range = rangeFor(flat, at, at + String(spec.exact).length);
      if (!range) { continue; }
      if (wrap(range, String(spec.tid))) { done.push(String(spec.tid)); }
    }
    post({ type: "ah-anchored", tids: done });
  }

  document.addEventListener("mouseup", function () {
    /* Let the browser finish updating the selection first. */
    window.setTimeout(function () {
      var selection = window.getSelection();
      if (!selection || selection.isCollapsed || selection.rangeCount === 0) {
        return;
      }
      var range = selection.getRangeAt(0);
      var flat = flatten();
      var start = offsetOf(flat, range.startContainer, range.startOffset);
      var end = offsetOf(flat, range.endContainer, range.endOffset);
      var exact;
      if (start >= 0 && end > start) {
        /* Slicing the flattened text guarantees the quote can be found again
           by the very same code path that anchors it. */
        exact = flat.text.slice(start, end);
      } else {
        exact = String(selection.toString());
        start = flat.text.indexOf(exact);
        end = start < 0 ? -1 : start + exact.length;
      }
      if (!exact || !exact.replace(/\\s+/g, "")) { return; }
      if (exact.length > MAX_EXACT) {
        exact = exact.slice(0, MAX_EXACT);
        end = start + exact.length;
      }
      var prefix = "";
      var suffix = "";
      if (start >= 0) {
        prefix = flat.text.slice(Math.max(0, start - CTX), start);
        suffix = flat.text.slice(end, end + CTX);
      }
      post({ type: "ah-select", exact: exact, prefix: prefix, suffix: suffix });
    }, 0);
  });

  document.addEventListener("click", function (event) {
    var node = event.target;
    while (node && node !== document) {
      if (node.nodeType === 1 && node.hasAttribute && node.hasAttribute(ATTR)) {
        post({ type: "ah-open", tid: node.getAttribute(ATTR) });
        return;
      }
      node = node.parentNode;
    }
  });

  window.addEventListener("message", function (event) {
    var data = event.data;
    if (!data || typeof data !== "object") { return; }
    if (data.type === "ah-anchors") { applyAnchors(data.anchors || []); }
  });

  post({ type: "ah-ready" });
})();
"""

#: The review shell, as one IIFE. It owns the credential; the artifact owns
#: nothing but its own opaque-origin iframe.
_REVIEW_JS = """
(function () {
  "use strict";

  var BASE = String(window.HUB_BASE || "").replace(/\\/+$/, "");
  var ID = String(window.HUB_ARTIFACT_ID || "");
  var PATH = BASE + "/a/" + encodeURIComponent(ID);

  /* Shared with /admin on purpose: one sign-in serves both pages. */
  var AUTH_KEY = "hub_admin_auth";
  var auth = null;

  /* An invited guest's credential, taken from the URL *fragment*
     (#invite={invitation_id}.{secret}). The fragment is never sent to a
     server by the browser, and this script never puts it in a URL either: it
     only ever travels in the X-Artifact-Guest request header, so it stays out
     of access logs, Referer headers and the hub's own routing. */
  var guest = null;

  var threads = [];
  var headVersion = null;
  var commentsMode = "anyone";
  var artifactStatus = "draft";
  var anchoredIds = {};
  var selection = null;
  var activeId = null;

  function $(id) { return document.getElementById(id); }
  function show(node, on) { if (node) { node.hidden = !on; } }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) { node.className = cls; }
    if (text !== undefined && text !== null) { node.textContent = String(text); }
    return node;
  }

  function badge(text, kind) {
    return el("span", "badge" + (kind ? " badge--" + kind : ""), text);
  }

  function setError(node, message) {
    node.textContent = message || "";
    node.hidden = !message;
  }

  function when(value) {
    return String(value || "").replace("T", " ").replace("+00:00", "");
  }

  function who(identity) {
    var data = identity || {};
    /* A guest is published as {kind: "guest", name}: no project, no stack. */
    if (data.kind === "guest") {
      return (data.name ? String(data.name) : "guest") + " (guest)";
    }
    if (data.project_name) { return String(data.project_name); }
    if (data.project_id !== undefined && data.project_id !== null) {
      return "project " + data.project_id;
    }
    return "unknown project";
  }

  /* ---------------------------------------------------------------- guest */

  /* Read #invite={invitation_id}.{secret} once, then clear the fragment from
     the address bar so the secret does not sit in a shared screen, a
     screenshot or the next person's history entry. The credential lives on in
     this closure for as long as the tab does. */
  function readInvite() {
    var hash = String(window.location.hash || "");
    var at = hash.indexOf("invite=");
    if (at < 0) { return null; }
    var value = hash.slice(at + 7).split("&")[0];
    if (!value || value.indexOf(".") < 1) { return null; }
    try {
      window.history.replaceState(null, "",
        window.location.pathname + window.location.search);
    } catch (err) {
      /* Non-fatal: the fragment simply stays visible. */
    }
    return { credential: decodeURIComponent(value), name: "" };
  }

  async function checkInvite() {
    if (!guest) { return; }
    try {
      /* Header only — the credential must never become part of a URL. This
         doubles as the validation step: a revoked or malformed invitation
         answers 401 here, before anybody writes a comment that would fail. */
      var data = JSON.parse(await read("/guest", true));
      guest.name = data.name || "";
    } catch (err) {
      guest = null;
      setError($("rv-signin-error"),
        "That invitation link is not valid any more: " + err.message);
    }
    renderIdentity();
    renderThreads();
  }

  /* Exactly one of the three identity blocks is on screen. A Storage token
     wins over an invitation, because it names a verified project and the
     header code prefers it — the banner must never claim otherwise. */
  function renderIdentity() {
    var asProject = !!auth;
    var asGuest = !asProject && !!guest;
    show($("rv-account"), asProject);
    show($("rv-guest"), asGuest);
    show($("rv-signin"), !asProject && !asGuest);
    if (asGuest) {
      $("rv-guest-name").textContent =
        "Commenting as " + (guest.name || "a guest") + " (guest)";
    }
  }

  /* ---------------------------------------------------------------- auth */

  function loadAuth() {
    try {
      var raw = window.sessionStorage.getItem(AUTH_KEY);
      if (!raw) { return null; }
      var parsed = JSON.parse(raw);
      if (parsed && parsed.token && parsed.stack) {
        return { token: parsed.token, stack: parsed.stack };
      }
    } catch (err) {
      /* Storage disabled or unreadable: just sign in again. */
    }
    return null;
  }

  function storeAuth(value) {
    try { window.sessionStorage.setItem(AUTH_KEY, JSON.stringify(value)); }
    catch (err) { /* Non-fatal: the session will not survive a reload. */ }
  }

  function clearAuth() {
    try { window.sessionStorage.removeItem(AUTH_KEY); } catch (err) {}
  }

  /* Whichever credential this visitor has. A guest never has a token and a
     signed-in project never needs the invitation, so the two are mutually
     exclusive; the token wins when somebody has both, because it identifies a
     verified project and the guest header does not. */
  function headers(withBody) {
    var out = {};
    if (auth) {
      out["X-StorageApi-Token"] = auth.token;
      out["X-Storage-Stack"] = auth.stack;
    } else if (guest) {
      out["X-Artifact-Guest"] = guest.credential;
    }
    if (withBody) { out["Content-Type"] = "application/json"; }
    return out;
  }

  function canWrite() { return !!(auth || guest); }

  function apiMessage(status, data, text) {
    if (data && typeof data.detail === "string") { return data.detail; }
    if (data && data.detail) { return JSON.stringify(data.detail); }
    if (data && data.error) {
      return data.error + (data.detail ? " \\u2014 " + data.detail : "");
    }
    if (text) { return "HTTP " + status + ": " + text.slice(0, 300); }
    return "HTTP " + status;
  }

  async function api(path, options) {
    var opts = options || {};
    var hasBody = opts.body !== undefined;
    var resp = await fetch(BASE + path, {
      method: opts.method || "GET",
      headers: headers(hasBody),
      body: hasBody ? JSON.stringify(opts.body) : undefined
    });
    var text = await resp.text();
    var data = null;
    try { data = text ? JSON.parse(text) : null; } catch (err) { data = null; }
    if (!resp.ok) { throw new Error(apiMessage(resp.status, data, text)); }
    return data;
  }

  /* Public reads. credentials:"same-origin" carries the unlock cookie of a
     password-protected artifact, which is scoped to /a/{id}. ``withGuest``
     adds the invitation header for the one read that needs it (/guest); the
     credential is never appended to the path or the query string. */
  async function read(path, withGuest) {
    var init = { credentials: "same-origin" };
    if (withGuest && guest) {
      init.headers = { "X-Artifact-Guest": guest.credential };
    }
    var resp = await fetch(PATH + path, init);
    var text = await resp.text();
    if (!resp.ok) {
      var data = null;
      try { data = JSON.parse(text); } catch (err) { data = null; }
      throw new Error(apiMessage(resp.status, data, text));
    }
    return text;
  }

  /* ------------------------------------------------------------- document */

  /* The artifact is loaded into a srcdoc iframe sandboxed WITHOUT
     allow-same-origin, so its scripts run in an opaque origin and can reach
     neither this document nor the token in sessionStorage. */
  function inject(htmlText) {
    var source = $("rv-anno").textContent;
    var snippet = "<scr" + "ipt>" + source + "</scr" + "ipt>";
    var lower = String(htmlText).toLowerCase();
    var at = lower.lastIndexOf("</body>");
    if (at < 0) { return htmlText + snippet; }
    return htmlText.slice(0, at) + snippet + htmlText.slice(at);
  }

  async function loadDocument() {
    var text = await read("/raw");
    $("rv-frame").srcdoc = inject(text);
  }

  async function loadVersions() {
    var data = JSON.parse(await read("/versions"));
    headVersion = data.head_version;
    $("rv-head").textContent = headVersion
      ? "commenting on v" + headVersion
      : "no live version";
  }

  async function loadThreads() {
    var data = JSON.parse(await read("/comments"));
    threads = data.threads || [];
    commentsMode = data.comments_mode || "anyone";
    artifactStatus = data.status || "draft";
    renderThreads();
    sendAnchors();
  }

  function sendAnchors() {
    var frame = $("rv-frame");
    if (!frame || !frame.contentWindow) { return; }
    var list = threads.map(function (thread) {
      var selector = thread.selector || {};
      return {
        tid: thread.id,
        exact: selector.exact || "",
        prefix: selector.prefix || "",
        suffix: selector.suffix || ""
      };
    });
    frame.contentWindow.postMessage({ type: "ah-anchors", anchors: list }, "*");
  }

  /* --------------------------------------------------------------- threads */

  function renderThreads() {
    var list = $("rv-threads");
    list.textContent = "";
    $("rv-count").textContent = threads.length +
      (threads.length === 1 ? " thread" : " threads");
    show($("rv-empty"), threads.length === 0);
    threads.forEach(function (thread) {
      list.appendChild(threadCard(thread));
    });
    var frozen = artifactStatus === "final";
    show($("rv-frozen"), frozen);
    show($("rv-closed"), !frozen && commentsMode === "off");
  }

  function threadCard(thread) {
    var item = el("li", "rv-thread");
    item.id = "rv-thread-" + thread.id;
    if (thread.resolved) { item.classList.add("is-resolved"); }
    if (activeId === thread.id) { item.classList.add("is-active"); }

    var top = el("div", "rv-thread-top");
    top.appendChild(el("span", "rv-who", who(thread.author)));
    top.appendChild(el("span", "rv-when", when(thread.created_at)));
    if (thread.version !== headVersion) {
      top.appendChild(badge("v" + thread.version));
    }
    if (thread.resolved) { top.appendChild(badge("resolved", "live")); }
    item.appendChild(top);

    var selector = thread.selector || {};
    item.appendChild(el("div", "rv-quote", selector.exact || ""));
    if (!anchoredIds[thread.id]) {
      item.appendChild(
        el("div", "rv-orphan", "quote not found on this version")
      );
    }
    item.appendChild(el("p", "rv-text", thread.body || ""));

    var replies = thread.replies || [];
    if (replies.length) {
      var replyList = el("ul", "rv-replies");
      replies.forEach(function (reply) {
        var row = document.createElement("li");
        var head = el("div", "rv-thread-top");
        head.appendChild(el("span", "rv-who", who(reply.author)));
        head.appendChild(el("span", "rv-when", when(reply.created_at)));
        row.appendChild(head);
        row.appendChild(el("p", "rv-text", reply.body || ""));
        replyList.appendChild(row);
      });
      item.appendChild(replyList);
    }

    /* Guests get the same buttons; the server decides what they may actually
       do with them (their own threads only) and says so in the error line. */
    if (canWrite()) { item.appendChild(threadActions(thread)); }
    item.addEventListener("click", function () {
      activeId = thread.id;
      renderThreads();
    });
    return item;
  }

  function threadActions(thread) {
    var wrap = el("div", null);
    var errBox = el("p", "err");
    errBox.hidden = true;

    var box = el("textarea", "rv-field");
    box.hidden = true;
    box.placeholder = "Reply\\u2026";

    function run(button, handler) {
      button.disabled = true;
      setError(errBox, "");
      handler().then(function () {
        button.disabled = false;
      }, function (err) {
        setError(errBox, err.message);
        button.disabled = false;
      });
    }

    var actions = el("div", "rv-actions");
    var path = "/api/artifacts/" + encodeURIComponent(ID) + "/comments/" +
      encodeURIComponent(thread.id);

    var replyBtn = el("button", "btn btn-sm", "Reply");
    replyBtn.type = "button";
    replyBtn.addEventListener("click", function (event) {
      event.stopPropagation();
      if (box.hidden) { box.hidden = false; box.focus(); return; }
      var text = box.value.trim();
      if (!text) { box.hidden = true; return; }
      run(replyBtn, async function () {
        await api(path + "/replies", { method: "POST", body: { body: text } });
        box.value = "";
        await loadThreads();
      });
    });
    actions.appendChild(replyBtn);

    var resolveBtn = el("button", "btn btn-sm",
      thread.resolved ? "Reopen" : "Resolve");
    resolveBtn.type = "button";
    resolveBtn.addEventListener("click", function (event) {
      event.stopPropagation();
      run(resolveBtn, async function () {
        await api(path + "/resolve", {
          method: "POST",
          body: { resolved: !thread.resolved }
        });
        await loadThreads();
      });
    });
    actions.appendChild(resolveBtn);

    var deleteBtn = el("button", "btn btn-sm btn-danger", "Delete");
    deleteBtn.type = "button";
    deleteBtn.addEventListener("click", function (event) {
      event.stopPropagation();
      if (!window.confirm("Delete this thread? This is permanent.")) { return; }
      run(deleteBtn, async function () {
        await api(path, { method: "DELETE" });
        await loadThreads();
      });
    });
    actions.appendChild(deleteBtn);

    wrap.appendChild(box);
    wrap.appendChild(actions);
    wrap.appendChild(errBox);
    return wrap;
  }

  function focusThread(tid) {
    activeId = tid;
    renderThreads();
    var node = $("rv-thread-" + tid);
    if (node && node.scrollIntoView) {
      node.scrollIntoView({ block: "center" });
    }
    show($("rv-side"), true);
  }

  /* -------------------------------------------------------------- composer */

  function openComposer(data) {
    selection = { exact: data.exact, prefix: data.prefix, suffix: data.suffix };
    $("rv-quote").textContent = data.exact;
    setError($("rv-composer-error"), "");
    show($("rv-composer"), true);
    show($("rv-side"), true);
    $("rv-comment").focus();
  }

  function closeComposer() {
    selection = null;
    $("rv-comment").value = "";
    show($("rv-composer"), false);
  }

  async function submitComment() {
    if (!selection) { return; }
    if (!canWrite()) {
      setError($("rv-composer-error"),
        "Sign in first to leave a comment \\u2014 or open this document " +
        "through an invitation link.");
      return;
    }
    var text = $("rv-comment").value.trim();
    if (!text) {
      setError($("rv-composer-error"), "Write something first.");
      return;
    }
    var button = $("rv-post");
    button.disabled = true;
    setError($("rv-composer-error"), "");
    try {
      await api("/api/artifacts/" + encodeURIComponent(ID) + "/comments", {
        method: "POST",
        body: {
          version: headVersion,
          exact: selection.exact,
          prefix: selection.prefix,
          suffix: selection.suffix,
          body: text
        }
      });
      closeComposer();
      await loadThreads();
    } catch (err) {
      setError($("rv-composer-error"), err.message);
    } finally {
      button.disabled = false;
    }
  }

  /* --------------------------------------------------------------- signin */

  function signedIn(projectId) {
    $("rv-project").textContent = "project " + (projectId || "?") +
      " \\u00b7 " + auth.stack;
    renderIdentity();
  }

  function signedOut() {
    auth = null;
    clearAuth();
    $("rv-token").value = "";
    /* Falls back to the guest banner when this visitor arrived through an
       invitation and signed in on top of it. */
    renderIdentity();
    renderThreads();
  }

  async function signIn(token, stack) {
    auth = { token: token, stack: stack };
    try {
      var data = await api("/api/artifacts");
      storeAuth(auth);
      $("rv-token").value = "";
      signedIn(data.project_id);
      renderThreads();
    } catch (err) {
      auth = null;
      throw err;
    }
  }

  /* ------------------------------------------------------------- messages */

  window.addEventListener("message", function (event) {
    var frame = $("rv-frame");
    /* Only the artifact frame may talk to the shell. */
    if (!frame || event.source !== frame.contentWindow) { return; }
    var data = event.data;
    if (!data || typeof data !== "object") { return; }
    if (data.type === "ah-ready") { sendAnchors(); return; }
    if (data.type === "ah-anchored") {
      anchoredIds = {};
      (data.tids || []).forEach(function (tid) {
        anchoredIds[String(tid)] = true;
      });
      renderThreads();
      return;
    }
    if (data.type === "ah-select") { openComposer(data); return; }
    if (data.type === "ah-open") { focusThread(String(data.tid)); }
  });

  /* --------------------------------------------------------------- wiring */

  $("rv-stack").addEventListener("change", function () {
    show($("rv-custom-wrap"), $("rv-stack").value === "__custom__");
  });

  $("rv-signin-form").addEventListener("submit", function (event) {
    event.preventDefault();
    setError($("rv-signin-error"), "");
    var token = $("rv-token").value.trim();
    var choice = $("rv-stack").value;
    var stack = choice === "__custom__" ? $("rv-custom").value.trim() : choice;
    if (!token) {
      setError($("rv-signin-error"), "Enter a Storage API token.");
      return;
    }
    if (!stack) {
      setError($("rv-signin-error"), "Enter the stack URL.");
      return;
    }
    var button = $("rv-signin-btn");
    button.disabled = true;
    signIn(token, stack).catch(function (err) {
      setError($("rv-signin-error"), err.message);
    }).then(function () {
      button.disabled = false;
    });
  });

  $("rv-logout").addEventListener("click", signedOut);
  $("rv-post").addEventListener("click", submitComment);
  $("rv-cancel").addEventListener("click", closeComposer);
  $("rv-toggle").addEventListener("click", function () {
    var side = $("rv-side");
    show(side, side.hidden);
  });

  /* ---------------------------------------------------------------- start */

  guest = readInvite();
  if (guest) { checkInvite(); }

  auth = loadAuth();
  if (auth) {
    var stacks = Array.prototype.map.call($("rv-stack").options,
      function (option) { return option.value; });
    if (stacks.indexOf(auth.stack) === -1) {
      $("rv-stack").value = "__custom__";
      $("rv-custom").value = auth.stack;
      show($("rv-custom-wrap"), true);
    } else {
      $("rv-stack").value = auth.stack;
    }
    api("/api/artifacts").then(function (data) {
      signedIn(data.project_id);
      renderThreads();
    }, function (err) {
      auth = null;
      clearAuth();
      renderIdentity();
      setError($("rv-signin-error"),
        "That session is no longer valid: " + err.message);
    });
  }

  loadDocument().catch(function (err) {
    setError($("rv-error"), err.message);
  });
  loadVersions().then(loadThreads).catch(function (err) {
    setError($("rv-error"), err.message);
  });
})();
"""


def review_page(base_url: str, artifact_id: str, service_version: str) -> str:
    """Render the two-pane review UI served at ``/a/{id}/review``.

    The page ships no artifact content and no credential of its own. Its
    JavaScript fetches ``/raw``, ``/versions`` and ``/comments`` for this
    artifact, injects :data:`_ANNOTATION_JS` into the fetched HTML and loads
    the result into a ``srcdoc`` iframe sandboxed *without*
    ``allow-same-origin``. The artifact therefore runs in an opaque origin: its
    scripts cannot read this document, its cookies or the Storage token the
    visitor may keep in ``sessionStorage`` (the same ``hub_admin_auth`` entry
    ``/admin`` uses, so one sign-in serves both pages). The two sides exchange
    nothing but ``postMessage`` envelopes.

    **Guest mode.** Opened through an invitation link — the URL the owner got
    from ``POST /api/artifacts/{id}/invitations``, whose ``#invite=`` fragment
    carries ``{invitation_id}.{secret}`` — the page reads that fragment, clears
    it from the address bar, validates it against ``GET /a/{id}/guest`` and
    then comments with an ``X-Artifact-Guest`` header instead of a token. The
    fragment never reaches the server as part of a URL, and the credential
    never leaves this tab.
    """
    base = html.escape(base_url.rstrip("/"))
    safe_id = html.escape(artifact_id)

    options = "".join(
        f'<option value="{html.escape(alias)}">{html.escape(alias)}</option>'
        for alias in _ADMIN_STACKS
    )
    options += '<option value="__custom__">custom URL…</option>'

    body = f"""<div class="rv">
<header class="rv-top">
<span class="rv-brand">Artifact Hub · Review</span>
<span class="badge badge--version">v{html.escape(service_version)}</span>
<span class="badge" id="rv-head">loading…</span>
<span class="spacer"></span>
<a class="btn btn-sm" href="{base}/a/{safe_id}">Open document</a>
<a class="btn btn-sm" href="{base}/a/{safe_id}/versions?format=html">Versions</a>
<a class="btn btn-sm" href="{base}/a/{safe_id}/export/vault">Export vault</a>
<button type="button" class="btn btn-sm" id="rv-toggle">Comments</button>
</header>
<div class="rv-body">
<div class="rv-doc">
<iframe id="rv-frame" title="artifact under review"
  sandbox="allow-scripts allow-popups"></iframe>
</div>
<aside class="rv-side" id="rv-side">
<h2>sign in</h2>
<div id="rv-signin">
<form id="rv-signin-form" autocomplete="off">
<label for="rv-token">Storage API token</label>
<input class="rv-field" type="password" id="rv-token" autocomplete="off"
  spellcheck="false" placeholder="your Keboola Storage API token">
<label for="rv-stack">Stack</label>
<select class="rv-field" id="rv-stack">{options}</select>
<div id="rv-custom-wrap" hidden>
<label for="rv-custom">Stack URL</label>
<input class="rv-field" type="text" id="rv-custom" spellcheck="false"
  placeholder="https://connection.keboola.com">
</div>
<button type="submit" class="btn btn-primary btn-wide" id="rv-signin-btn">Sign in to comment</button>
<p class="err" id="rv-signin-error" hidden></p>
</form>
<p class="rv-hint">Reading is public; commenting needs any Keboola Storage
API token. The token stays in this browser tab (<code>sessionStorage</code>,
shared with <a href="{base}/admin">/admin</a>) and is never sent anywhere but
this hub's own API.</p>
<p class="rv-hint">No Keboola account? The artifact's owner can send you a
guest invitation link, which lets you comment here without one.</p>
</div>
<div id="rv-account" hidden>
<div class="rv-thread-top">
<span class="badge badge--version" id="rv-project"></span>
<button type="button" class="btn btn-sm" id="rv-logout">Log out</button>
</div>
</div>
<div id="rv-guest" hidden>
<div class="rv-thread-top">
<span class="badge badge--version" id="rv-guest-name"></span>
</div>
<p class="rv-hint">You are here on an invitation, so no Keboola account is
needed. You can open threads, reply, and resolve or delete the threads you
opened. The invitation lives in this tab only — keep the link if you want to
come back, and expect it to stop working once whoever invited you revokes
it.</p>
</div>

<h2>comment</h2>
<div id="rv-composer" hidden>
<div class="rv-quote" id="rv-quote"></div>
<label for="rv-comment">Your comment</label>
<textarea class="rv-field" id="rv-comment"
  placeholder="What about this passage?"></textarea>
<div class="rv-actions">
<button type="button" class="btn btn-primary btn-sm" id="rv-post">Comment</button>
<button type="button" class="btn btn-sm" id="rv-cancel">Cancel</button>
</div>
<p class="err" id="rv-composer-error" hidden></p>
</div>
<p class="rv-hint" id="rv-hint">Select any text in the document to start a
thread. Threads stay attached to the version they were made on, so one made on
an older version may no longer match this text.</p>

<h2>threads</h2>
<div class="rv-thread-top">
<span class="rv-when" id="rv-count"></span>
</div>
<p class="rv-hint" id="rv-frozen" hidden>This document is final: no new
versions and no new comments.</p>
<p class="rv-hint" id="rv-closed" hidden>Commenting is closed on this
document.</p>
<p class="err" id="rv-error" hidden></p>
<p class="empty" id="rv-empty" hidden>No comments yet.</p>
<ul class="rv-threads" id="rv-threads"></ul>
</aside>
</div>
</div>
<script>window.HUB_BASE = "{base}"; window.HUB_ARTIFACT_ID = "{safe_id}";</script>
<script type="text/plain" id="rv-anno">"""

    return _page(
        f"Review — {artifact_id}",
        _CONTROLS_CSS + _REVIEW_CSS,
        body + _ANNOTATION_JS + "</script>\n<script>" + _REVIEW_JS + "</script>",
    )


# --------------------------------------------------------------------------
# Changelog
# --------------------------------------------------------------------------

#: Prose styles for the changelog body. The shell's own rules already size h1
#: and h2; what a long Markdown document adds is list, table and release-heading
#: rhythm. Each ``h2`` (one release) is dressed as the shell's ``//`` section
#: label — same monospace, same rule running to the right margin — so a rendered
#: changelog reads as a page of this service rather than as a pasted document.
_CHANGELOG_CSS = """
.changelog { max-width: 48rem; }
.changelog h2 {
  display: flex;
  align-items: center;
  gap: .6rem;
  font-size: 1rem;
  margin: 2.75rem 0 .85rem;
  padding-top: .2rem;
}
.changelog h2::before { content: "//"; color: var(--accent); font-weight: 700; }
.changelog h2::after { content: ""; flex: 1; height: 1px;
  background: var(--line); }
.changelog h3 { font-size: .82rem; letter-spacing: .12em;
  text-transform: uppercase; color: var(--muted); margin: 1.5rem 0 .4rem; }
.changelog ul, .changelog ol { padding-left: 1.15rem; margin: .5rem 0 1rem; }
.changelog li { margin: .3rem 0; color: var(--ink-2); }
.changelog li::marker { color: var(--accent); }
.changelog a { overflow-wrap: anywhere; }
.changelog table { margin: .5rem 0 1.25rem; }
.changelog pre {
  overflow-x: auto;
  background: var(--term-bg);
  color: var(--term-fg);
  border: 1px solid var(--term-line);
  border-radius: var(--radius);
  padding: .9rem 1rem;
  font-size: .8rem;
  line-height: 1.7;
}
.changelog pre code { background: none; color: inherit; padding: 0; }
.changelog blockquote { margin: 1rem 0; padding: .1rem 0 .1rem 1rem;
  border-left: 2px solid var(--accent); color: var(--muted); }
.changelog hr { border: 0; border-top: 1px solid var(--line); margin: 2rem 0; }
"""


#: Pulls the document's own leading ``<h1>`` out of the rendered fragment, so
#: it can become the page's hero instead of appearing a second time under one.
_LEADING_H1_RE = re.compile(r"\A\s*<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)


def _hoist_heading(body_html: str, fallback: str) -> tuple[str, str]:
    """Split a leading ``<h1>`` off a rendered fragment: ``(heading, rest)``.

    A Markdown document titles itself, and the shell wants that title in its
    hero rather than repeated below one. Anything else — a document that opens
    on a paragraph, or on an ``h2`` — keeps its shape and gets ``fallback``.
    """
    match = _LEADING_H1_RE.match(body_html)
    if match is None:
        return fallback, body_html
    heading = match.group(1).strip()
    return (heading or fallback), body_html[match.end():].lstrip()


def changelog_page(body_html: str, service_version: str, github_url: str) -> str:
    """Wrap pre-rendered changelog Markdown in the service's own shell.

    ``body_html`` is an HTML *fragment* — the caller renders it (``src.main``
    uses the builder's configured markdown-it, so the dialect matches what a
    published artifact gets) and this function supplies the chrome: the
    graph-paper grid, the monospace hero, the ``//`` label treatment adapted so
    each release heading reads like a section rule, and the same footer the
    landing page carries. The document's own leading ``h1`` becomes that hero,
    so the page is titled by the file rather than by this function.

    The fragment is trusted markup, not user input: it comes from a file in
    this repository, rendered by this process. Everything else on the page is
    escaped as usual.
    """
    version = html.escape(service_version)
    repo = html.escape(github_url.rstrip("/"))
    heading, body_html = _hoist_heading(body_html, "Changelog")

    body = f"""<main>
<header class="hero">
<h1>{heading}</h1>
<p class="lead">Every released change to the Artifact Hub, newest first. The
same file is served verbatim at <a href="/changelog.md">/changelog.md</a> for
machines.</p>
<div class="hero-meta">
<span class="badge badge--version">v{version}</span>
<span class="badge">running now</span>
</div>
</header>

<article class="changelog">
{body_html}
</article>

<footer>
<span>kbc-artifact-hub v{version}</span>
<span class="spacer"></span>
<a href="/">hub home</a>
<a href="/changelog.md">/changelog.md</a>
<a href="/docs">/docs</a>
<a href="{repo}">source</a>
</footer>
</main>"""

    return _page("Changelog · Artifact Hub", _CHANGELOG_CSS, body)


# --------------------------------------------------------------------------
# Visual diff
# --------------------------------------------------------------------------

_VISUAL_DIFF_CSS = """
html, body { height: 100%; }
html { background-image: none; }

.vd { display: flex; flex-direction: column; height: 100vh; }

.vd-top { display: flex; align-items: center; flex-wrap: wrap; gap: .5rem;
  padding: .5rem .85rem; border-bottom: 1px solid var(--line);
  background: var(--panel); }
.vd-brand { font-family: var(--font-mono); font-weight: 700; font-size: .88rem;
  letter-spacing: -.01em; }
.vd-top .spacer { flex: 1; }
.vd-added { color: var(--live); font-weight: 700; }
.vd-removed { color: var(--danger); font-weight: 700; }
.vd-stat { font-family: var(--font-mono); font-size: .76rem;
  color: var(--muted); }

.vd-body { flex: 1; display: flex; min-height: 0; }
.vd-pane { flex: 1 1 50%; min-width: 0; display: flex; flex-direction: column;
  background: #ffffff; }
.vd-pane + .vd-pane { border-left: 1px solid var(--line); }
.vd-head { display: flex; align-items: center; gap: .45rem;
  padding: .35rem .7rem; border-bottom: 1px solid var(--line);
  background: var(--panel); }
.vd-title { font-size: .78rem; color: var(--muted); overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; }
.vd-pane iframe { flex: 1; width: 100%; border: 0; background: #ffffff; }

@media (max-width: 45rem) {
  .vd-body { flex-direction: column; }
  .vd-pane + .vd-pane { border-left: 0; border-top: 1px solid var(--line); }
}
"""

#: Injected into each side's document before it goes into its ``srcdoc``.
#:
#: It runs in an **opaque origin** (both frames are sandboxed without
#: ``allow-same-origin``), so its only channel to the shell is ``postMessage``:
#: it reports its own scroll position as a *ratio* of scrollable height, and
#: applies a ratio the shell relays from the other side. Ratios rather than
#: pixels, because the two versions are different documents of different
#: heights — matching absolute offsets would drift immediately.
#:
#: Everything here is best effort by design. An artifact whose own scripts throw
#: still renders; it just stops driving the other pane.
_VISUAL_SYNC_JS = """
(function () {
  "use strict";

  var SIDE = window.name || "";

  /* The ratio this frame was last *told* to scroll to, or -1 when its position
     is its own doing. It is how an echo is recognised without a timer: the
     scroll event caused by applying a relayed position looks exactly like this
     value, so it is swallowed once and the pair settles instead of chasing
     each other. Deliberately no setTimeout anywhere — a background tab
     throttles timers hard, and scroll sync that only works in a foreground tab
     would be worse than none. */
  var applied = -1;

  function scroller() {
    return document.scrollingElement || document.documentElement ||
      document.body;
  }

  function span() {
    var node = scroller();
    if (!node) { return 0; }
    return node.scrollHeight - node.clientHeight;
  }

  function ratio() {
    var node = scroller();
    var height = span();
    if (!node || height <= 0) { return 0; }
    return node.scrollTop / height;
  }

  /* Two positions count as the same when they are within a pixel or two of
     each other: scrollTop is an integer, so a ratio can never be reproduced
     exactly, and an off-by-one pixel must not read as a fresh scroll. */
  function same(a, b) {
    var height = span();
    if (height <= 0) { return true; }
    return Math.abs(a - b) * height < 2;
  }

  function post(message) {
    try { parent.postMessage(message, "*"); } catch (err) { /* detached */ }
  }

  window.addEventListener("scroll", function () {
    var now = ratio();
    if (applied >= 0 && same(now, applied)) {
      /* This is the scroll we were asked to make. Report nothing, and go back
         to treating our own movement as news. */
      applied = -1;
      return;
    }
    applied = -1;
    post({ type: "ahd-scroll", side: SIDE, ratio: now });
  }, { passive: true });

  window.addEventListener("message", function (event) {
    var data = event.data;
    if (!data || typeof data !== "object") { return; }
    if (data.type !== "ahd-scroll-to") { return; }
    var value = Number(data.ratio);
    if (!isFinite(value)) { return; }
    value = Math.max(0, Math.min(1, value));
    var node = scroller();
    var height = span();
    if (!node || height <= 0) { return; }
    /* Already there: moving nothing also emits nothing, which is what stops a
       relay from bouncing back and forth. */
    if (same(ratio(), value)) { return; }
    applied = value;
    try {
      node.scrollTop = value * height;
    } catch (err) {
      applied = -1;
    }
  });

  post({ type: "ahd-ready", side: SIDE });
})();
"""

#: The shell half of the scroll sync: relay each side's reported ratio to the
#: other, and never back to the sender.
_VISUAL_DIFF_JS = """
(function () {
  "use strict";

  var frames = {
    older: document.getElementById("vd-older"),
    newer: document.getElementById("vd-newer")
  };

  function other(side) {
    return side === "older" ? frames.newer : frames.older;
  }

  window.addEventListener("message", function (event) {
    var data = event.data;
    if (!data || typeof data !== "object") { return; }
    if (data.type !== "ahd-scroll") { return; }
    /* Only our own two frames may drive this page. */
    if (event.source !== frames.older.contentWindow &&
        event.source !== frames.newer.contentWindow) { return; }
    var side = event.source === frames.older.contentWindow ? "older" : "newer";
    var target = other(side);
    if (!target || !target.contentWindow) { return; }
    try {
      target.contentWindow.postMessage(
        { type: "ahd-scroll-to", ratio: data.ratio }, "*"
      );
    } catch (err) {
      /* One pane failing to follow must never break the other. */
    }
  });
})();
"""


def _inject_sync(document_html: str) -> str:
    """Append the scroll-sync script to one side's document.

    Inserted before the last ``</body>`` when there is one so the artifact's own
    ``DOMContentLoaded`` handlers still see the document they expect, and simply
    appended otherwise — a published artifact is not guaranteed to be a
    well-formed document.
    """
    snippet = f"<script>{_VISUAL_SYNC_JS}</script>"
    lower = document_html.lower()
    at = lower.rfind("</body>")
    if at < 0:
        return document_html + snippet
    return document_html[:at] + snippet + document_html[at:]


def _visual_pane(side: str, label: str, title: str, document_html: str) -> str:
    """One half of the visual diff: a header plus its sandboxed iframe.

    The document goes into ``srcdoc`` — escaped with ``quote=True``, since it is
    an attribute *value* — inside a frame sandboxed **without**
    ``allow-same-origin``. Both versions therefore run in their own opaque
    origins: they can neither read this page nor each other, which matters
    doubly here, where two mutually distrusting versions of a document are on
    screen at once.
    """
    safe_side = html.escape(side, quote=True)
    frame_title = html.escape(f"{label} — {title}", quote=True)
    return (
        f'<section class="vd-pane">'
        f'<div class="vd-head">'
        f'<span class="badge badge--version">{html.escape(label)}</span>'
        f'<span class="vd-title">{html.escape(title or "untitled")}</span>'
        "</div>"
        f'<iframe id="vd-{safe_side}" name="{safe_side}" title="{frame_title}" '
        'sandbox="allow-scripts allow-popups" '
        f'srcdoc="{html.escape(_inject_sync(document_html), quote=True)}">'
        "</iframe></section>"
    )


def visual_diff_page(
    older: object,
    newer: object,
    *,
    added: int | None = None,
    removed: int | None = None,
) -> str:
    """Render two versions of one artifact side by side, as they look.

    ``older`` and ``newer`` are version envelopes (anything carrying
    ``version``, ``title`` and ``html``). Each is loaded into its own sandboxed
    ``srcdoc`` iframe and the two panes scroll in step, so a reviewer compares
    the *rendered* documents rather than the source that produced them — which
    is what ``?format=html`` already does well.

    Scroll sync is best effort and says so: an artifact whose own scripts break,
    or one that scrolls an inner element rather than the document, simply stops
    driving the other pane. Nothing else on the page depends on it.

    ``added``/``removed`` are the line counts from the ordinary diff; pass
    ``None`` when they could not be computed and the header quietly omits them.
    """
    older_version = getattr(older, "version", "?")
    newer_version = getattr(newer, "version", "?")
    older_label = f"v{older_version}"
    newer_label = f"v{newer_version}"

    if added is None or removed is None:
        stats = '<span class="vd-stat">line counts unavailable</span>'
    else:
        stats = (
            '<span class="vd-stat">'
            f'<span class="vd-added">+{int(added)}</span> '
            f'<span class="vd-removed">-{int(removed)}</span></span>'
        )

    heading = html.escape(f"{older_label} → {newer_label}")
    panes = _visual_pane(
        "older",
        older_label,
        str(getattr(older, "title", "") or ""),
        str(getattr(older, "html", "") or ""),
    ) + _visual_pane(
        "newer",
        newer_label,
        str(getattr(newer, "title", "") or ""),
        str(getattr(newer, "html", "") or ""),
    )

    body = f"""<div class="vd">
<header class="vd-top">
<span class="vd-brand">Artifact Hub · Visual diff</span>
<span class="badge">{heading}</span>
{stats}
<span class="spacer"></span>
<span class="vd-stat">scrolling is synchronized</span>
</header>
<div class="vd-body">
{panes}
</div>
</div>
<script>{_VISUAL_DIFF_JS}</script>"""

    return _page(
        f"Visual diff — {older_label} vs {newer_label}",
        _CONTROLS_CSS + _VISUAL_DIFF_CSS,
        body,
    )
