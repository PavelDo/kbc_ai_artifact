"""Human-facing HTML shell pages.

Only two pages are rendered by the service itself: the landing page served at
``/`` (what the hub is, how to authenticate, copy-pasteable curl examples) and
the unlock form shown when a password-protected artifact is opened in a
browser. Artifact content itself is never templated here — it is served
verbatim from the envelope.

Every dynamic value is escaped with :func:`html.escape` before it reaches the
markup. Styling is embedded (no external assets, no fonts, no scripts) so the
pages work behind any proxy and in any network environment.
"""

from __future__ import annotations

import html

#: Shared stylesheet for both pages: system font stack, light/dark via
#: ``prefers-color-scheme``, no external resources.
_CSS = """
:root {
  color-scheme: light dark;
  --bg: #ffffff;
  --fg: #1b1f24;
  --muted: #5b6470;
  --border: #e2e6ea;
  --card: #f7f8fa;
  --code-bg: #f2f4f7;
  --accent: #1f6feb;
  --danger: #b42318;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #11151a;
    --fg: #e8ecf1;
    --muted: #9aa4b1;
    --border: #262d36;
    --card: #171c23;
    --code-bg: #0d1117;
    --accent: #6ea8ff;
    --danger: #ff7b72;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 0;
  background: var(--bg);
  color: var(--fg);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
    "Helvetica Neue", Arial, sans-serif;
  line-height: 1.6;
  font-size: 16px;
}
main { max-width: 52rem; margin: 0 auto; padding: 3rem 1.25rem 5rem; }
h1 { font-size: 2rem; line-height: 1.2; margin: 0 0 .25rem; letter-spacing: -.02em; }
h2 { font-size: 1.15rem; margin: 2.5rem 0 .75rem; letter-spacing: -.01em; }
p { margin: .75rem 0; }
a { color: var(--accent); }
.lead { color: var(--muted); font-size: 1.05rem; margin-top: 0; }
code, pre {
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas,
    "Liberation Mono", monospace;
  font-size: .875rem;
}
code { background: var(--code-bg); padding: .1rem .35rem; border-radius: 4px; }
pre {
  background: var(--code-bg);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: .9rem 1rem;
  overflow-x: auto;
  margin: .5rem 0 1.25rem;
}
pre code { background: none; padding: 0; }
table { border-collapse: collapse; width: 100%; margin: .5rem 0 1.25rem; display: block;
  overflow-x: auto; }
th, td {
  text-align: left;
  padding: .45rem .7rem;
  border-bottom: 1px solid var(--border);
  vertical-align: top;
}
th { font-weight: 600; font-size: .8rem; text-transform: uppercase;
  letter-spacing: .04em; color: var(--muted); }
td code { white-space: nowrap; }
.note {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: .75rem 1rem;
  color: var(--muted);
  font-size: .925rem;
}
footer { margin-top: 3rem; color: var(--muted); font-size: .875rem; }
"""

_UNLOCK_CSS = """
body { display: flex; align-items: center; justify-content: center;
  min-height: 100vh; padding: 1.25rem; }
.card {
  width: 100%;
  max-width: 24rem;
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 1.75rem;
}
.card h1 { font-size: 1.25rem; margin: 0 0 .35rem; }
.card p { color: var(--muted); font-size: .925rem; margin: 0 0 1.25rem; }
label { display: block; font-size: .8rem; text-transform: uppercase;
  letter-spacing: .04em; color: var(--muted); margin-bottom: .35rem; }
input[type=password] {
  width: 100%;
  padding: .6rem .7rem;
  font: inherit;
  color: var(--fg);
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 8px;
}
input[type=password]:focus { outline: 2px solid var(--accent); outline-offset: 1px; }
button {
  margin-top: 1rem;
  width: 100%;
  padding: .6rem 1rem;
  font: inherit;
  font-weight: 600;
  color: #fff;
  background: var(--accent);
  border: 0;
  border-radius: 8px;
  cursor: pointer;
}
button:hover { filter: brightness(1.08); }
.error { color: var(--danger); font-size: .875rem; margin: .75rem 0 0; }
.hint { font-size: .8rem; margin-top: 1.25rem; }
"""


def _page(title: str, extra_css: str, body: str) -> str:
    """Wrap a body fragment in the shared HTML skeleton."""
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{html.escape(title)}</title>\n"
        f"<style>{_CSS}{extra_css}</style>\n"
        "</head>\n<body>\n"
        f"{body}\n"
        "</body>\n</html>\n"
    )


def landing_page(base_url: str) -> str:
    """Render the public landing page documenting the service."""
    base = html.escape(base_url.rstrip("/"))
    return _page(
        "KBC Artifact Hub",
        "",
        f"""<main>
<h1>KBC Artifact Hub</h1>
<p class="lead">Public hosting for self-contained HTML artifacts, backed by
Keboola Storage. Publish with any Keboola Storage API token, from any stack,
and get back an unguessable public URL.</p>

<p>The canonical copy of what you publish is stored as a Storage File in
<strong>your own</strong> Keboola project, so you keep ownership of the
content. A serving copy lives in the hub's project so reads stay fast and do
not depend on your project. There is no public listing: artifact URLs are
capabilities, and an optional password adds a second layer on top.</p>

<h2>Authentication</h2>
<p>Everything under <code>/api/artifacts</code> is authenticated with two
headers. The hub verifies the token against your stack's own
<code>/v2/storage/tokens/verify</code> endpoint and never stores it.</p>
<pre><code>X-StorageApi-Token: &lt;your Keboola Storage API token&gt;
X-Storage-Stack: &lt;alias or https URL&gt;</code></pre>
<p><code>X-Storage-Stack</code> accepts an alias (<code>us</code>,
<code>gcp-us</code>, <code>eu</code>, <code>azure-eu</code>,
<code>gcp-eu</code>) or any full <code>https://*.keboola.com</code> URL.
Ownership is the pair (stack, project id); updating or deleting an artifact
requires a token from the project that published it.</p>

<h2>Publish HTML</h2>
<pre><code>curl -s -X POST "{base}/api/artifacts" \\
  -H "X-StorageApi-Token: $KBC_TOKEN" \\
  -H "X-Storage-Stack: eu" \\
  -H "Content-Type: application/json" \\
  -d '{{"html": "&lt;h1&gt;Hello&lt;/h1&gt;", "title": "My report"}}'</code></pre>

<h2>Publish Markdown</h2>
<p>Markdown is rendered by the built-in template: GFM tables, task lists,
mermaid fences and syntax highlighting.</p>
<pre><code>curl -s -X POST "{base}/api/artifacts" \\
  -H "X-StorageApi-Token: $KBC_TOKEN" \\
  -H "X-Storage-Stack: eu" \\
  -H "Content-Type: application/json" \\
  -d '{{"markdown": "# Q3 review\\n\\n- shipped\\n- measured\\n",
       "title": "Q3 review", "password": "optional"}}'</code></pre>

<h2>Publish from a git repository</h2>
<p>The repository is shallow-cloned; the entry document is
<code>git_path</code>, otherwise <code>index.html</code>, then
<code>README.md</code>, then a single root-level <code>*.html</code>. Relative
images are inlined as data URIs so the result is self-contained.</p>
<pre><code>curl -s -X POST "{base}/api/artifacts" \\
  -H "X-StorageApi-Token: $KBC_TOKEN" \\
  -H "X-Storage-Stack: eu" \\
  -H "Content-Type: application/json" \\
  -d '{{"git_url": "https://github.com/owner/repo",
       "git_ref": "main", "git_path": "docs/report.md"}}'</code></pre>
<p>For a private repository, add <code>git_token</code> (a personal access
token for the git host) and, only if the host needs one,
<code>git_username</code> — it defaults to <code>x-access-token</code>. Like
your Storage token, it is used only for the clone during that request: it is
never stored, logged, or returned.</p>

<h2>Reading an artifact</h2>
<table>
<tr><th>Method</th><th>Path</th><th>Returns</th></tr>
<tr><td>GET</td><td><code>/a/{{id}}</code></td><td>Rendered page (or the unlock
form when protected)</td></tr>
<tr><td>POST</td><td><code>/a/{{id}}/unlock</code></td><td>Password form target;
sets a signed cookie scoped to the artifact</td></tr>
<tr><td>GET</td><td><code>/a/{{id}}/raw</code></td><td>The HTML itself, for
machines</td></tr>
<tr><td>GET</td><td><code>/a/{{id}}/source</code></td><td>Original source
(Markdown or HTML)</td></tr>
<tr><td>GET</td><td><code>/a/{{id}}/meta</code></td><td>Public metadata JSON,
no owner details</td></tr>
</table>
<p class="note">Protected artifacts accept the password over the
<code>X-Artifact-Password</code> header on <code>/raw</code> and
<code>/source</code>; <code>/meta</code> stays public either way.</p>

<h2>Managing your artifacts</h2>
<table>
<tr><th>Method</th><th>Path</th><th>Purpose</th></tr>
<tr><td>POST</td><td><code>/api/artifacts</code></td><td>Publish</td></tr>
<tr><td>PUT</td><td><code>/api/artifacts/{{id}}</code></td><td>Update content
and/or password (owner only)</td></tr>
<tr><td>GET</td><td><code>/api/artifacts</code></td><td>List your project's
artifacts</td></tr>
<tr><td>DELETE</td><td><code>/api/artifacts/{{id}}</code></td><td>Delete the
serving copy (owner only)</td></tr>
</table>

<h2>For agents</h2>
<p><a href="{base}/skill">{base}/skill</a> — SKILL.md teaching an agent how to
author and publish artifacts.<br>
<a href="{base}/context">{base}/context</a> — machine-readable manifest:
endpoints, auth model, limits.<br>
<a href="{base}/docs">{base}/docs</a> — interactive Swagger UI for this API,
with a machine-readable schema at <a href="{base}/openapi.json">{base}/openapi.json</a>.</p>

<footer>kbc-artifact-hub 0.1.0 &middot;
<a href="{base}/health">/health</a></footer>
</main>""",
    )


def unlock_page(artifact_id: str, error: str | None) -> str:
    """Render the password form for a protected artifact."""
    safe_id = html.escape(artifact_id)
    error_html = (
        f'<p class="error">{html.escape(error)}</p>' if error else ""
    )
    return _page(
        "Password required",
        _UNLOCK_CSS,
        f"""<div class="card">
<h1>Password required</h1>
<p>This artifact is protected. Enter its password to continue.</p>
<form method="post" action="/a/{safe_id}/unlock">
<label for="password">Password</label>
<input type="password" id="password" name="password" autocomplete="current-password"
  autofocus required>
<button type="submit">Unlock</button>
{error_html}
</form>
<p class="hint">Machines can send the password in the
<code>X-Artifact-Password</code> header instead.</p>
</div>""",
    )
