"""Human-facing HTML shell pages and their shared mini design system.

Four pages are rendered by the service itself:

- the landing page at ``/`` — what the hub is, what it does, and how to drive
  it from a terminal,
- the unlock form shown when a password-protected artifact is opened in a
  browser,
- the version picker at ``/a/{id}/versions?format=html``, and
- the owner/moderation studio at ``/admin``, a single self-contained page whose
  vanilla JS talks to the same public API a terminal would.

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


#: Studio-only styles. Everything structural (tokens, badges, cards, tables,
#: terminal cards, footer) comes from :data:`_CSS`; this block only adds the
#: widgets the admin page introduces — buttons, the sign-in card, the artifact
#: rows with their expandable detail panel, and the preview modal.
_ADMIN_CSS = """
main { max-width: 68rem; }

/* The studio toggles visibility with the `hidden` attribute, and several
   widgets below set an explicit `display`, which would otherwise win over the
   user-agent's `[hidden] { display: none }`. */
[hidden] { display: none !important; }

.ahead { display: flex; flex-wrap: wrap; align-items: flex-start; gap: 1rem;
  margin-bottom: .5rem; }
.ahead h1 { font-size: clamp(1.5rem, 1.1rem + 1.4vw, 2.1rem); margin: 0 0 .35rem; }
.ahead .lead { margin: 0; color: var(--ink-2); font-size: .95rem;
  max-width: 40rem; }
.ahead-right { margin-left: auto; display: flex; align-items: center;
  gap: .5rem; flex-wrap: wrap; }

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

/* -------- status lines ---------------------------------------------------- */
.err { font-family: var(--font-mono); font-size: .78rem; color: var(--danger);
  margin: .7rem 0 0; overflow-wrap: anywhere; }
.err::before { content: "! "; font-weight: 700; }
.loading { font-family: var(--font-mono); font-size: .8rem; color: var(--muted);
  padding: .6rem 0; }
.empty { font-family: var(--font-mono); font-size: .84rem; color: var(--muted);
  padding: .8rem 0; }
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
    if (row.proposed_count) {
      badges.appendChild(badge(
        row.proposed_count + (row.proposed_count === 1 ? " proposal" : " proposals"),
        "proposed"
      ));
    }
    if (row.accept_versions) { badges.appendChild(badge("accepting versions")); }
    if (row.protected) { badges.appendChild(badge("protected")); }
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
         and the auth headers make proposals visible to the owner. */
      var data = await request("/a/" + encodeURIComponent(row.id) + "/versions");
      renderPanel(row, panel, data);
    } catch (err) {
      panel.textContent = "";
      var box = el("p", "err", err.message);
      panel.appendChild(box);
    }
  }

  function renderPanel(row, panel, data) {
    panel.textContent = "";

    var id = encodeURIComponent(row.id);
    var head = data.head_version;
    var publicUrl = BASE + "/a/" + row.id;

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

    panel.appendChild(controls);
    panel.appendChild(errBox);

    /* ---- version table ---- */
    var versions = data.versions || [];
    if (!versions.length) {
      panel.appendChild(el("p", "empty", "no versions"));
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
          var body = await requestHtml("/a/" + id + "/v/" + n);
          openModal("proposed v" + n + " \\u2014 " + row.id, body);
        } else {
          window.open(BASE + "/a/" + row.id + "/v/" + n, "_blank", "noopener");
        }
      }));

      if (head !== null && head !== undefined && n !== head) {
        actions.appendChild(action("Diff vs head", "", async function () {
          var body = await requestHtml(
            "/a/" + id + "/diff/" + head + ".." + n + "?format=html"
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
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && !$("modal").hidden) { closeModal(); }
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
</table></div>

<footer>
<span>kbc-artifact-hub v{version}</span>
<span class="spacer"></span>
<a href="{base}/admin">Admin studio</a>
<a href="{base}/agent">/agent</a>
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
your Keboola project owns — in the browser, over the same API you can drive
with curl.</p>
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
        _ADMIN_CSS,
        body + _ADMIN_JS + "</script>",
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
