---
name: artifact-publisher
description: Publish HTML/Markdown artifacts to KBC Artifact Hub — a public artifact hosting service backed by Keboola Storage. Use when the user wants to share a document, report, diagram or dashboard as a public URL, authenticated by any Keboola Storage API token.
---

# KBC Artifact Hub

## What this service is

KBC Artifact Hub is a public hosting service for self-contained HTML artifacts,
backed by Keboola Storage. Anyone holding **any** Keboola Storage API token, on
**any** Keboola stack, can publish a document and get back an unguessable
public URL — no separate account or sign-up needed. The canonical copy of what
you publish is stored as a Storage File in **your own** Keboola project (so you
keep ownership of the content); a serving copy lives in the hub's own project
so the read path stays fast and does not depend on your project's
availability. URLs are capabilities: anyone who has the link can view the
artifact, and an optional password adds a second layer of protection on top
of that.

## Base URL discovery

This SKILL.md is served at `GET /skill` on the hub itself — the base URL to
use for every other call below is whatever host you fetched this file from.
Call `GET /context` on that same host for a machine-readable manifest
(supported inputs, current limits, endpoint list) if you need to confirm
capabilities before publishing. For interactive exploration, `GET /docs`
serves a Swagger UI and `GET /openapi.json` the underlying OpenAPI schema.

## Authentication

The management API (everything under `/api/artifacts`) is authenticated with
two headers on every request:

```
X-StorageApi-Token: <your Keboola Storage API token>
X-Storage-Stack: <alias-or-url>
```

`X-Storage-Stack` accepts either a short alias or a full `https://` URL of your
Keboola stack:

| Alias | Resolves to |
|---|---|
| `us` | `https://connection.keboola.com` |
| `gcp-us` | `https://connection.us-east4.gcp.keboola.com` |
| `eu` | `https://connection.eu-central-1.keboola.com` |
| `azure-eu` | `https://connection.north-europe.azure.keboola.com` |
| `gcp-eu` | `https://connection.europe-west3.gcp.keboola.com` |

Any full `https://*.keboola.com` URL is also accepted directly, so you do not
need to know the alias table if you already know your stack's hostname.

The hub verifies the token against your stack's own
`GET /v2/storage/tokens/verify` endpoint and derives your project identity
from the response — it never stores your token.

**Never put the token in a URL, query string, or the request body.** It only
ever belongs in the `X-StorageApi-Token` header.

## Publishing

Use `$HUB` for the base URL and `$KBC_TOKEN` for your Storage token in the
examples below.

### Publish HTML

```bash
curl -s -X POST "$HUB/api/artifacts" \
  -H "X-StorageApi-Token: $KBC_TOKEN" \
  -H "X-Storage-Stack: eu" \
  -H "Content-Type: application/json" \
  -d '{
    "html": "<!doctype html><html><body><h1>Hello</h1></body></html>",
    "title": "My report"
  }'
```

Response:

```json
{
  "id": "aBcD3fGhIjKlMnOpQrStUvWx",
  "url": "https://<hub-host>/a/aBcD3fGhIjKlMnOpQrStUvWx",
  "raw_url": "https://<hub-host>/a/aBcD3fGhIjKlMnOpQrStUvWx/raw",
  "meta_url": "https://<hub-host>/a/aBcD3fGhIjKlMnOpQrStUvWx/meta"
}
```

### Publish Markdown

```bash
curl -s -X POST "$HUB/api/artifacts" \
  -H "X-StorageApi-Token: $KBC_TOKEN" \
  -H "X-Storage-Stack: eu" \
  -H "Content-Type: application/json" \
  -d '{
    "markdown": "# Title\n\nSome **content** with a table:\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
  }'
```

The hub renders Markdown with its built-in template: GFM tables, task lists,
fenced ` ```mermaid ` diagrams, syntax-highlighted code blocks, and automatic
light/dark mode. This is the easiest way to get a good-looking page without
writing any CSS.

### Publish from a git repository

```bash
curl -s -X POST "$HUB/api/artifacts" \
  -H "X-StorageApi-Token: $KBC_TOKEN" \
  -H "X-Storage-Stack: eu" \
  -H "Content-Type: application/json" \
  -d '{
    "git_url": "https://github.com/org/repo",
    "git_ref": "main",
    "git_path": "docs/report.md"
  }'
```

The hub shallow-clones the repository, then picks the entry point in this
order: your `git_path` if given, otherwise `index.html`, otherwise
`README.md`. Markdown entries are rendered the same way as a direct Markdown
publish. Relative local images referenced by the entry file are inlined as
data URIs so the resulting artifact stays a single, self-contained document.

### Publish from a private git repository

Add `git_token` — a personal access token for the git host (GitHub PAT, GitLab
token, …) — to clone a repository that is not public:

```bash
curl -s -X POST "$HUB/api/artifacts" \
  -H "X-StorageApi-Token: $KBC_TOKEN" \
  -H "X-Storage-Stack: eu" \
  -H "Content-Type: application/json" \
  -d '{
    "git_url": "https://github.com/org/private-repo",
    "git_ref": "main",
    "git_path": "docs/report.md",
    "git_token": "your-github-pat"
  }'
```

`git_username` is optional and defaults to `x-access-token`, which is what
GitHub PATs and GitLab deploy tokens expect. Set it only for hosts that
require a real username.

**Transient credentials.** Exactly like your Storage token, `git_token` is
used only for the clone inside that one request: it is never written to the
stored artifact, never appears in logs or error messages, and is never
returned in any response. Nothing is remembered, so a later `PUT` that
re-publishes from the same private repository must send the token again. Use a
token with the narrowest possible scope (read-only, single repository) and
revoke it when you no longer need it.

Both `git_token` and `git_username` are only meaningful alongside `git_url`;
sending either without it is a 422.

### Password protection

Add `"password": "secret"` to any of the publish bodies above (create or
update). Human visitors get an unlock form in the browser; machine clients
authenticate by sending the header `X-Artifact-Password: secret` on reads.

### Update and delete

```bash
# Update (must use a token from the owning project)
curl -s -X PUT "$HUB/api/artifacts/aBcD3fGhIjKlMnOpQrStUvWx" \
  -H "X-StorageApi-Token: $KBC_TOKEN" \
  -H "X-Storage-Stack: eu" \
  -H "Content-Type: application/json" \
  -d '{"markdown": "# Updated title\n\nNew content."}'

# Delete
curl -s -X DELETE "$HUB/api/artifacts/aBcD3fGhIjKlMnOpQrStUvWx" \
  -H "X-StorageApi-Token: $KBC_TOKEN" \
  -H "X-Storage-Stack: eu"

# List your own project's artifacts
curl -s "$HUB/api/artifacts" \
  -H "X-StorageApi-Token: $KBC_TOKEN" \
  -H "X-Storage-Stack: eu"
```

`PUT` and `DELETE` both require a token from the project that originally
published the artifact.

## Reading (public, no token required)

| Endpoint | Returns |
|---|---|
| `GET /a/{id}` | Human-readable rendered page (or the password unlock form) |
| `GET /a/{id}/raw` | Exact HTML that will be rendered — no chrome around it |
| `GET /a/{id}/source` | Original source you submitted (markdown or html) |
| `GET /a/{id}/meta` | JSON metadata (title, created/updated time, content type — no owner details) |

If the artifact is password-protected, machine clients pass
`X-Artifact-Password: <password>` on these requests; browsers get an HTML
unlock form instead.

## Content authoring guidance for agents

When you are the one generating the content to publish, keep these rules in
mind:

- **Artifacts must be self-contained single HTML documents.** Inline all CSS
  and JavaScript in `<style>`/`<script>` tags, and embed images as data URIs
  rather than linking to external image hosts.
- **External CDN libraries are allowed** for well-known libraries served over
  HTTPS from jsdelivr or similar — e.g. `mermaid`, `chart.js`,
  `highlight.js`. Don't rely on arbitrary third-party scripts.
- **Charts**: use `chart.js` via CDN, or hand-roll inline SVG if you want zero
  external dependencies.
- **Mermaid diagrams**: either embed `<pre class="mermaid">...</pre>` blocks
  plus a mermaid ESM import in your own HTML, or — simpler — publish
  Markdown with ` ```mermaid ` fenced code blocks and let the hub's own
  template render them for you.
- **Size limit**: keep the total built HTML under 15 MB.
- **Responsive and dark-mode aware**: use relative units and avoid
  hard-coded light-only colors; the hub's Markdown template already handles
  this for you automatically.
- **When in doubt, publish Markdown.** Letting the hub's template do the
  styling is the fastest way to get a clean, consistent, responsive result
  without hand-writing CSS.

## Error semantics

| Status | Meaning |
|---|---|
| 400 | Unknown or disallowed `X-Storage-Stack` value |
| 401 | Storage token rejected by the stack, or wrong artifact password |
| 403 | Token is valid but not from the owning project (update/delete) |
| 404 | Unknown artifact id (identical response whether it never existed or was deleted) |
| 422 | Build failure — bad git repo, no entry file found, markdown render error, or `git_token`/`git_username` sent without `git_url` |
| 502 | The Keboola stack itself could not be reached to verify the token |

## Safety notes

- The artifact URL is a **capability**: anyone holding the link can read the
  content, with no further authentication. Do not publish secrets, credentials,
  or anything you would not want exposed to anyone who guesses or receives
  the link.
- A password adds a second layer on top of the URL, but does not make the
  content private from anyone the URL is deliberately shared with.
- Publishing, updating, and deleting always require a valid Keboola Storage
  token — only reading is anonymous.
- Publishing from a private repository publishes its content to a **public**
  URL. The `git_token` protects the clone, not the artifact; add a `password`
  if the result should not be readable by everyone holding the link.
