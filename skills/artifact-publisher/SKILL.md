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
If anything below ever looks stale, `/skill` and `/context` are always the
current truth — re-fetch them rather than trusting a cached copy of this file.

`GET /agent` serves a ready-to-install Claude Code subagent that knows this
entire API (auth, publishing, versioning, moderation) without needing this
skill loaded at all. Install it with:

```bash
install -d ~/.claude/agents && curl -fsSL "$HUB/agent" -o ~/.claude/agents/artifact-hub.md
```

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
  "version": 1,
  "status": "live",
  "head_version": 1,
  "accept_versions": false,
  "url": "https://<hub-host>/a/aBcD3fGhIjKlMnOpQrStUvWx",
  "raw_url": "https://<hub-host>/a/aBcD3fGhIjKlMnOpQrStUvWx/raw",
  "meta_url": "https://<hub-host>/a/aBcD3fGhIjKlMnOpQrStUvWx/meta",
  "versions_url": "https://<hub-host>/a/aBcD3fGhIjKlMnOpQrStUvWx/versions"
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

### Open the artifact to contributions

Add `"accept_versions": true` to a publish or update body to let **other**
Keboola projects submit versions of your artifact. Their submissions always
land as moderated proposals that only you can promote — see *Versioning*
below. The default is `false`: only the owning project may add versions.

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
published the artifact. A `PUT` that carries content adds a **new version**;
nothing is ever overwritten. A `title` lives on a version, so it can only be
changed together with new content (422 otherwise).

### Project-brain settings (`PUT /api/artifacts/{id}`)

Four fields on the same `PUT` govern who may contribute and whether the
document is still open at all:

| Field | Values | Meaning |
|---|---|---|
| `accept_versions_mode` | `"off"` \| `"anyone"` \| `"allowlist"` | Who may submit a version. Supersedes the legacy `accept_versions` boolean (`false`→`off`, `true`→`anyone`); both are still accepted, but send one or the other, not contradictory values. |
| `contributors` | list of `"projectId@stackhost"` | Owner keys allowed to submit versions/comments when the matching mode is `allowlist`. Ignored otherwise. |
| `comments_mode` | `"anyone"` \| `"allowlist"` \| `"off"` | Who may open comment threads and reply. Default `"anyone"`. |
| `status` | `"draft"` \| `"final"` | `"final"` freezes new versions **and** new comments for everyone, the owner included, and shows a banner on the page. Reopen by `PUT`-ing `{"status": "draft"}` (owner only). |

```bash
curl -s -X PUT "$HUB/api/artifacts/aBcD3fGhIjKlMnOpQrStUvWx" \
  -H "X-StorageApi-Token: $KBC_TOKEN" \
  -H "X-Storage-Stack: eu" \
  -H "Content-Type: application/json" \
  -d '{
    "accept_versions_mode": "allowlist",
    "contributors": ["1234@connection.eu-central-1.keboola.com"],
    "comments_mode": "allowlist",
    "status": "draft"
  }'
```

An `allowlist` mode shares the same `contributors` list for both versions and
comments — there is no separate list per capability. Setting `status` to
`"final"` is the signal an agent should treat as "stop proposing changes, the
document is settled" — see *Collaborative review workflow* in the
`artifact-hub` agent for the full loop.

## Versioning

Every submission becomes its own version with a verified author. `GET /a/{id}`
serves the **head** — the newest live version, or one the owner pinned.

Versions have one of two statuses:

- **live** — servable, and eligible to be the head.
- **proposed** — moderated. Its metadata is listed to anyone holding the
  capability URL, but its *content* is readable only by the artifact owner and
  the version's own author, until the owner promotes it.

Who may submit what:

| Caller | `accept_versions` | Result |
|---|---|---|
| The owning project | any | version added as **live** |
| Another project | `false` (default) | **403** — the artifact is closed |
| Another project | `true` | version added as **proposed** |

The canonical copy of a version is always stored in the **submitter's own**
Keboola project, with the submitter's token — whoever wrote a version keeps its
source of truth.

### Submit a version

```bash
curl -s -X POST "$HUB/api/artifacts/aBcD3fGhIjKlMnOpQrStUvWx/versions" \
  -H "X-StorageApi-Token: $KBC_TOKEN" \
  -H "X-Storage-Stack: eu" \
  -H "Content-Type: application/json" \
  -d '{
    "markdown": "# Q3 review\n\nCorrected the revenue table.",
    "note": "fix Q3 totals"
  }'
```

The body takes the same content fields as publishing (exactly one of `html`,
`markdown`, `git_url`, plus the `git_*` extras), an optional `title`, and an
optional `note` describing what changed (max 500 characters). The response is:

```json
{
  "id": "aBcD3fGhIjKlMnOpQrStUvWx",
  "version": 2,
  "status": "proposed",
  "note": "fix Q3 totals",
  "url": "https://<hub-host>/a/aBcD3fGhIjKlMnOpQrStUvWx/v/2"
}
```

### List versions

```bash
curl -s "$HUB/a/aBcD3fGhIjKlMnOpQrStUvWx/versions"
```

Returns `{"id", "head_version", "accept_versions", "protected", "versions": [...]}`
newest first; each entry carries `version`, `title`, `status`, `note`,
`created_at`, `is_head`, `size_bytes`, `source_type`, the author's project, and
a `url`. Add `?format=html` for a human-readable picker page with links to each
version and to the diff of every adjacent pair.

### Read one version

```bash
curl -s "$HUB/a/aBcD3fGhIjKlMnOpQrStUvWx/v/2"
```

For a **proposed** version, send your management headers
(`X-StorageApi-Token` + `X-Storage-Stack`) on this read too — the hub serves it
only to the artifact owner and the version's author, and answers 403 to
everyone else.

### Diff two versions

```bash
# Side-by-side HTML page (default)
curl -s "$HUB/a/aBcD3fGhIjKlMnOpQrStUvWx/diff/1..2"

# Unified diff, text/plain — the best format for an agent to read
curl -s "$HUB/a/aBcD3fGhIjKlMnOpQrStUvWx/diff/1..2?format=unified"

# JSON: the unified diff plus added/removed line counts
curl -s "$HUB/a/aBcD3fGhIjKlMnOpQrStUvWx/diff/1..2?format=json"
```

The spec is always `{older}..{newer}`. Markdown is compared when both versions
carry it, otherwise the built HTML. Formats other than `html`, `unified` and
`json` are a 400; a side larger than the configured diff limit is a 413.

### Promote a proposal (owner only)

```bash
curl -s -X POST "$HUB/api/artifacts/aBcD3fGhIjKlMnOpQrStUvWx/versions/2/promote" \
  -H "X-StorageApi-Token: $KBC_TOKEN" \
  -H "X-Storage-Stack: eu"
```

The version becomes live, and — with the default head mode — immediately
becomes what `/a/{id}` serves. Promoting an already-live version is a 409.

### Delete a version

```bash
curl -s -X DELETE "$HUB/api/artifacts/aBcD3fGhIjKlMnOpQrStUvWx/versions/2" \
  -H "X-StorageApi-Token: $KBC_TOKEN" \
  -H "X-Storage-Stack: eu"
```

The owner may delete any version except the last live one (409 — an artifact
must keep one). A contributor may delete only their own proposal, which is how
you withdraw a submission.

### Pin the head

```bash
# Always serve the newest live version (the default)
curl -s -X PUT "$HUB/api/artifacts/aBcD3fGhIjKlMnOpQrStUvWx/head" \
  -H "X-StorageApi-Token: $KBC_TOKEN" \
  -H "X-Storage-Stack: eu" \
  -H "Content-Type: application/json" \
  -d '{"mode": "latest"}'

# Freeze the artifact on one live version
curl -s -X PUT "$HUB/api/artifacts/aBcD3fGhIjKlMnOpQrStUvWx/head" \
  -H "X-StorageApi-Token: $KBC_TOKEN" \
  -H "X-Storage-Stack: eu" \
  -H "Content-Type: application/json" \
  -d '{"mode": "pinned", "version": 1}'
```

Owner only. The pinned version must exist and be live (422 otherwise), and it
is protected from retention pruning. The response reports
`head_version_served`.

### Limits

- **Retention** — at most `HUB_MAX_VERSIONS` (default 50) live versions per
  artifact. The oldest live versions that are neither the head nor pinned are
  pruned. Proposals are never pruned.
- **Rate limit** — `HUB_MAX_VERSIONS_PER_DAY` (default 20) submitted versions
  per contributing project, per artifact, per UTC day. Past that, 429.

### Admin studio

`GET /admin` is a browser-based moderation studio for an artifact's owner:
review proposal diffs, promote or reject them, pin the head version, and
toggle `accept_versions` — all by clicking, no curl required. The author signs
in by pasting their own Storage token directly into the page; that token stays
in the browser's `sessionStorage` and is **never** sent to or stored by the
hub's server outside the individual API calls it makes on the author's behalf.
Point a human at this page instead of asking them to run the promote/reject
curl commands above by hand.

## Inline comments

Any Keboola-token holder can leave inline, threaded comments anchored to a
specific quote in a specific version — the mechanism that turns an artifact
into a shared "project brain" that other agents and humans build on together,
instead of a one-way publish.

### Anchor model

A comment is anchored the W3C `TextQuoteSelector` way: `exact` (the quoted
text) plus a `prefix` and `suffix` of roughly 32 characters of surrounding
context, all captured from the **rendered text** of one specific version. This
lets a highlight be re-found even when the exact quote occurs more than once
in the document. A thread stays bound to the version it was opened on — there
is no cross-version re-anchoring in v1, so a thread opened on v2 keeps
pointing at v2's text even after v3 is published.

### Read all threads (public)

```bash
curl -s "$HUB/a/aBcD3fGhIjKlMnOpQrStUvWx/comments"
```

Public JSON, password-gated exactly like the other read endpoints. Returns
every thread — open and resolved — with its selector, body, author, and
replies.

### Open a thread

```bash
curl -s -X POST "$HUB/api/artifacts/aBcD3fGhIjKlMnOpQrStUvWx/comments" \
  -H "X-StorageApi-Token: $KBC_TOKEN" \
  -H "X-Storage-Stack: eu" \
  -H "Content-Type: application/json" \
  -d '{
    "version": 2,
    "exact": "the Q3 revenue total",
    "prefix": "...as shown in ",
    "suffix": " on the summary page...",
    "body": "This number looks off versus the source table — can you check?"
  }'
```

`version` pins the thread to the version whose rendered text the quote came
from. `exact`, `prefix`, and `suffix` should be captured from that version's
actual rendered output, not retyped from memory. Response carries the new
thread's `id`.

### Reply to a thread

```bash
curl -s -X POST "$HUB/api/artifacts/aBcD3fGhIjKlMnOpQrStUvWx/comments/<tid>/replies" \
  -H "X-StorageApi-Token: $KBC_TOKEN" \
  -H "X-Storage-Stack: eu" \
  -H "Content-Type: application/json" \
  -d '{"body": "Fixed in v3 — see the diff for the corrected totals."}'
```

Anyone allowed to comment (see *Moderation* below) may reply to an existing
thread, not only its original author.

### Resolve or reopen a thread

```bash
# Mark resolved
curl -s -X POST "$HUB/api/artifacts/aBcD3fGhIjKlMnOpQrStUvWx/comments/<tid>/resolve" \
  -H "X-StorageApi-Token: $KBC_TOKEN" \
  -H "X-Storage-Stack: eu" \
  -H "Content-Type: application/json" \
  -d '{"resolved": true}'

# Reopen
curl -s -X POST "$HUB/api/artifacts/aBcD3fGhIjKlMnOpQrStUvWx/comments/<tid>/resolve" \
  -H "X-StorageApi-Token: $KBC_TOKEN" \
  -H "X-Storage-Stack: eu" \
  -H "Content-Type: application/json" \
  -d '{"resolved": false}'
```

Only the artifact owner or the thread's own author may resolve or reopen it.

### Delete a thread

```bash
curl -s -X DELETE "$HUB/api/artifacts/aBcD3fGhIjKlMnOpQrStUvWx/comments/<tid>" \
  -H "X-StorageApi-Token: $KBC_TOKEN" \
  -H "X-Storage-Stack: eu"
```

Only the artifact owner or the thread's own author may delete it. Deletion is
irreversible and removes the whole thread, replies included.

### Moderation semantics

- `comments_mode` on the artifact (`"anyone"` | `"allowlist"` | `"off"`,
  default `"anyone"`) governs who may open a thread or reply, mirroring how
  `accept_versions_mode` governs who may submit a version. The owner may
  always comment regardless of mode.
- Opening or replying to a thread when comments are `"off"`, or when the
  caller's project is not in `contributors` under `"allowlist"`, is a **403**.
- Opening or replying on an artifact whose `status` is `"final"` is a
  **409** — a final document is frozen for everyone, owner included.
- Each contributing project is capped at `HUB_MAX_COMMENTS_PER_DAY` (default
  100) new threads plus replies per artifact per UTC day; past that, **429**.

### Review UI

`GET /a/{id}/review` is a browser-based review page: select text in the
rendered document to open a comment composer anchored to that selection; the
sidebar lists every thread, and clicking one scrolls to and highlights its
anchor. The artifact itself renders inside a **sandboxed** `srcdoc` iframe
(`allow-scripts`, no `allow-same-origin`) so the artifact's own JavaScript runs
cross-origin and can never see anything outside it. Sign-in works like
`/admin`: the reviewer pastes their own Storage token, which stays in the
browser's `sessionStorage` and is never sent to or stored by the hub's server
outside the individual API calls it makes on the reviewer's behalf. Point a
human reviewer at this page instead of asking them to run the comment curl
commands above by hand.

## Export

Two read-only export endpoints turn an artifact's full history into files a
human — or another AI agent — can work with outside the hub. Both are
password-gated exactly like the other read endpoints.

### Markdown source

```bash
curl -s "$HUB/a/aBcD3fGhIjKlMnOpQrStUvWx/export/markdown"
```

Returns the head version's original Markdown source, or the built HTML
document when the head version has no Markdown (e.g. it was published as raw
HTML or from a non-Markdown git file).

### Obsidian vault

```bash
curl -s "$HUB/a/aBcD3fGhIjKlMnOpQrStUvWx/export/vault" -o vault.zip
```

Returns an in-memory ZIP that is a ready-to-open Obsidian vault:

- `INDEX.md` — the hub note, wikilinked to every version and every comment
  thread, plus artifact-level frontmatter (status, owner, version and comment
  counts).
- `document.md` — the served (head) version's content.
- `versions/v{n}.md` — one note per version: author, date, status, and note in
  the frontmatter, plus a unified diff against the previous version.
- `comments/{tid}.md` — one note per thread: the quoted passage, the full
  reply chain, and its resolution state.
- `reasoning.md` — a deterministic, chronological timeline merging every
  version and every comment event — the "how this document got here" trail.

Obsidian's own graph view over these wikilinks **is** the knowledge graph;
there is no separate graph engine to run. This is the artifact's endpoint for
"archive everything that happened here" — pull it once a document is marked
`final` to keep a permanent, browsable record of the discussion that produced
it.

## Reading (public, no token required)

| Endpoint | Returns |
|---|---|
| `GET /a/{id}` | Head version as a human-readable page (or the password unlock form) |
| `GET /a/{id}/v/{n}` | One specific version (owner/author only when proposed) |
| `GET /a/{id}/versions` | Version history JSON, or `?format=html` for a picker page |
| `GET /a/{id}/diff/{a}..{b}` | Diff of two versions (`?format=html\|unified\|json`) |
| `GET /a/{id}/raw` | Exact HTML that will be rendered — no chrome around it |
| `GET /a/{id}/source` | Original source you submitted (markdown or html) |
| `GET /a/{id}/meta` | JSON metadata (title, timestamps, head version, version counts, content type, `accept_versions_mode`, `contributors`, `comments_mode`, `status` — no owner details) |
| `GET /a/{id}/comments` | Every inline comment thread (open and resolved), as JSON |
| `GET /a/{id}/review` | Browser review UI: select text to comment, sidebar of threads, sandboxed artifact iframe |
| `GET /a/{id}/export/markdown` | Head version's Markdown source (or HTML when there is no Markdown) |
| `GET /a/{id}/export/vault` | ZIP of a ready-to-open Obsidian vault (versions, comments, and a chronological reasoning trail) |
| `GET /admin` | Browser moderation studio for the artifact owner (token pasted client-side, never stored server-side) |
| `GET /agent` | This hub's SKILL.md distilled into a ready-to-install Claude Code subagent |

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
| 400 | Unknown or disallowed `X-Storage-Stack` value, a malformed diff spec (use `{older}..{newer}`), or an unknown diff `format` |
| 401 | Storage token rejected by the stack, or wrong artifact password |
| 403 | Token is valid but not from the owning project (update, delete, promote, head); the artifact does not accept versions from other projects; you asked for a proposal you did not author; or comments are closed (`comments_mode: "off"`) or you are not on the `contributors` allowlist |
| 404 | Unknown artifact id (identical response whether it never existed or was deleted), or no such version or comment thread |
| 409 | Promoting a version that is already live; deleting the only live version of an artifact; submitting a version or a comment on an artifact whose `status` is `"final"`; or resolving/reopening a thread already in that state |
| 413 | Built HTML over the size limit, or a diff side over `HUB_DIFF_MAX_BYTES` |
| 422 | Build failure — bad git repo, no entry file found, markdown render error, `git_token`/`git_username` sent without `git_url`, a `title` sent without content, or pinning the head to a version that does not exist or is not live |
| 429 | Your project reached the daily version-submission cap for this artifact, or the daily `HUB_MAX_COMMENTS_PER_DAY` comment cap |
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
- `accept_versions` lets **anyone with a Keboola token** attach content to your
  artifact. Their submissions can never be served on their own — they stay
  proposals until you promote one — but their metadata (project name, note,
  timestamps) is visible to everyone holding the capability URL. Review the
  diff before promoting; a promoted version becomes what `/a/{id}` serves.
- Publishing from a private repository publishes its content to a **public**
  URL. The `git_token` protects the clone, not the artifact; add a `password`
  if the result should not be readable by everyone holding the link.
