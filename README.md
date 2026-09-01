# KBC Artifact Hub

[![repo](https://img.shields.io/badge/github-padak%2Fkbc__ai__artifact-1442e0)](https://github.com/padak/kbc_ai_artifact)
[![releases](https://img.shields.io/badge/releases-latest-1442e0)](https://github.com/padak/kbc_ai_artifact/releases)
[![python](https://img.shields.io/badge/python-3.11%2B-1442e0)](https://www.python.org/)

Source, issues and tagged releases live at
<https://github.com/padak/kbc_ai_artifact> — the deployed service reports the
version it is running at `GET /health` and `GET /context`, which is the single
source of truth (read from the installed package metadata, itself derived from
`pyproject.toml`).

A Keboola App (FastAPI) that hosts self-contained HTML/Markdown artifacts
under public, unguessable URLs. Anyone holding **any** Keboola Storage API
token, on **any** Keboola stack, can publish a document; the service returns
a public URL that humans can open in a browser and machines can fetch as raw
content, source, or metadata over a small JSON API.

## Features

- Publish HTML, Markdown, or a git repository (public, or private via a
  transient access token) as a hosted artifact
- Unguessable capability URLs (`token_urlsafe`, 24 chars) — no public listing,
  `X-Robots-Tag: noindex` on every artifact response
- Optional password protection, with a web unlock form and a machine header
- **Community versioning**: every update adds a version instead of overwriting,
  and `accept_versions` lets other Keboola projects submit versions of your
  artifact
- **Moderated proposals**: a submission from another project lands as a
  proposal whose content only you and its author can read, until you promote it
- **Diffs**: side-by-side HTML, unified text, or JSON with add/remove counts —
  standard library only, no new dependencies
- **Head pointer**: `/a/{id}` serves the newest live version, or one you pin
- Machine-readable API: `/context` manifest and a `/skill` SKILL.md an AI
  agent can read to learn how to publish and contribute, unassisted
- **Admin studio** (`/admin`): a browser moderation UI where an artifact's
  owner pastes their Storage token client-side (kept in `sessionStorage`,
  never sent to or stored by the server) to review proposal diffs, promote or
  reject them, pin the head version, and toggle `accept_versions` — all by
  clicking
- **Installable agent** (`/agent`): serves a ready-to-use Claude Code subagent
  definition, distilled from `/skill`, that a user can drop straight into
  `~/.claude/agents/`
- **Inline comments and a review UI**: anyone with a Keboola token can leave
  threaded comments anchored to a quoted passage of a specific version
  (`GET/POST /a/{id}/comments`), and `GET /a/{id}/review` is a browser page
  where selecting text opens a comment composer — the artifact renders in a
  sandboxed iframe so its own scripts never see the reviewer's token
- **Obsidian vault export** (`GET /a/{id}/export/vault`): a ZIP containing a
  ready-to-open vault — an `INDEX.md` hub, one note per version with its diff,
  one note per comment thread, and a chronological `reasoning.md` timeline —
  so Obsidian's own graph view becomes the artifact's knowledge graph
- **Contributor allowlist**: `accept_versions_mode` and `comments_mode` can
  each be `off` / `anyone` / `allowlist`, restricting who may submit versions
  or comment to a `contributors` list of Keboola projects
- **Final status**: an owner can mark an artifact `final`, freezing new
  versions and comments for everyone (including the owner) until it is
  reopened
- **Trash, restore, and permanent purge**: `DELETE /api/artifacts/{id}` is a
  soft delete (public link dies, everything else is kept and restorable);
  `POST .../restore` brings it back on the same URL; `DELETE .../purge` is the
  separate, irreversible one
- **Link rotation**: `POST /api/artifacts/{id}/rotate-link` mints a fresh
  public share id and kills the old link instantly — the way to revoke a URL
  that was shared with the wrong person
- **`base_version` and staleness detection**: a submitted version can declare
  which version it was written against; `GET /a/{id}/versions` flags a
  proposal `outdated` when the head has moved on since, so reviewers know
  when a proposal's own diff no longer tells the whole story
- **Outbound webhooks**: register up to `HUB_MAX_WEBHOOKS_PER_ARTIFACT` https
  URLs per artifact and get a signed (`X-Hub-Signature-256`) JSON POST — or a
  formatted Slack message for a `hooks.slack.com` URL — on every version,
  comment, finalize, trash/restore and link-rotation event
- **Guest invitations**: `POST /api/artifacts/{id}/invitations` mints a named,
  revocable capability that lets one person with no Keboola account comment
  through the review UI, secret carried in the URL fragment and never logged
- **View statistics**: `GET /api/artifacts/{id}/stats` reports read counts by
  day and by surface (page/raw/source/version) for the artifact's owner
- **Visual diff**: `GET /a/{id}/diff/{a}..{b}?format=visual` renders both
  versions side by side in synced-scroll sandboxed iframes, for comparing what
  a reader actually sees rather than the underlying source
- Markdown rendering with GFM tables, task lists, mermaid diagrams, and
  syntax-highlighted code
- Survives restarts: the only durable state is Keboola Storage Files; local
  disk is a cache, not a source of truth
- Interactive API docs at `/docs` (Swagger UI) and a machine-readable schema
  at `/openapi.json`

## Architecture

**Publish flow.** A client sends content (`html`, `markdown`, or `git_url`)
plus a Storage token and stack to `POST /api/artifacts`. The service verifies
the token against the caller's own stack to establish project identity, then
builds the final HTML (rendering Markdown or cloning+resolving a git repo as
needed). Two copies are written:

- a **canonical copy** of the built HTML as a Storage File in the **author's
  own project**, uploaded with the author's token, tagged `kbc-artifact` and
  `artifact-id-<id>`;
- a **version envelope** (HTML + source + verified author + status, as JSON) as
  a Storage File in the **host project**, named `artifact-<id>-v<n>.json` and
  tagged `artifact-hub`, `artifact-id-<id>`, `artifact-ver-<n>`, alongside an
  artifact-level **meta record** `artifact-<id>-meta.json` (owner, password
  hash, `accept_versions`, head pointer) tagged `artifact-hub`,
  `artifact-id-<id>`, `artifact-meta`, `artifact-owner-<key>`.

Updates never overwrite a version: each one uploads the next version file. A
submission from a project other than the owner is stored with status
`proposed` and is not served until the owner promotes it. Legacy single-file
envelopes from before versioning are read as version 1 and migrated on the
next write.

The service never persists client tokens; they are used only for the
duration of the request. The same holds for an optional `git_token` used to
clone a private repository: it lives only in the clone subprocess argument,
and git's output is scrubbed of credentials before any of it reaches a log
line or an error message.

**Read flow.** `GET /a/{id}` (and `/raw`, `/source`, `/meta`, `/v/{n}`,
`/versions`, `/diff/{a}..{b}`) is served from an in-memory index that maps
artifact ids to their version and meta files, backed by a disk LRU cache.
`/a/{id}` resolves the *head* — the newest live version, or the one the owner
pinned — and, since 0.6.0, `id` in that path is the artifact's **share id**:
equal to its internal id until the owner rotates the link, after which only
the new share id resolves publicly. On startup, since the app container has
no permanent disk, the service rebuilds this index from scratch by listing
every Storage File tagged `artifact-hub` in the host project — the Storage
Files of the host project are the single source of truth, and local disk
holds only a cache of recently-served envelopes.

**Rendering.** `/a/{id}` and `/a/{id}/v/{n}` serve the artifact inside a
`srcdoc` iframe sandboxed without `allow-same-origin` (0.6.0): the published
document's own scripts run in an opaque origin and can never reach the hub's
own origin, where `/admin` and `/review` keep a visitor's Storage token in
`sessionStorage`. `/a/{id}/raw` is unaffected — it stays the byte-exact
document for machine clients that just want the HTML.

## Project brain workflow

A published artifact is not just a document to link — it is one URL a whole
team of humans and AI agents can collaborate around. An agent publishes a
first draft; teammates and other agents open it, leave inline comments on the
specific passages they have questions about, or submit whole proposed
versions with a note explaining the change; anyone can fetch `/meta`,
`/versions`, and `/comments` to catch up on what has already been said before
adding their own point, so the discussion converges instead of repeating
itself. The owner reviews diffs and threads at `/admin` and `/review`,
promotes or rejects proposals, and — once the document is settled — marks it
`final`, freezing further changes for everyone. At that point `/export/vault`
turns the whole history (every version, every thread, every decision) into a
ready-to-open Obsidian vault: a permanent, browsable record of how the
document got to where it is, with nothing to reconstruct from memory.

## API reference

Public (no auth):

| Method | Path | Description |
|---|---|---|
| GET | `/` | Landing page (docs) |
| POST | `/` | Returns 200 (platform health check) |
| GET | `/context` | Machine-readable manifest |
| GET | `/skill` | SKILL.md (`text/markdown`) teaching agents how to publish |
| GET | `/agent` | Ready-to-install Claude Code subagent definition (`text/markdown`) |
| GET | `/admin` | Browser moderation studio (owner pastes their Storage token client-side; never stored server-side) |
| GET | `/docs` | Interactive Swagger UI for this API |
| GET | `/openapi.json` | Machine-readable OpenAPI schema for this API |
| GET | `/a/{id}` | Head version rendered in a sandboxed iframe, or the password unlock form |
| POST | `/a/{id}/unlock` | Password form target; sets a signed unlock cookie |
| GET | `/a/{id}/v/{n}` | One specific version (owner/author only when proposed) |
| GET | `/a/{id}/versions` | Version history JSON, proposed rows flagged `outdated`; `?format=html` renders a picker page |
| GET | `/a/{id}/diff/{a}..{b}` | Diff two versions; `?format=html\|unified\|json\|visual` |
| GET | `/a/{id}/raw` | Raw built HTML, byte-exact, no iframe (password via `X-Artifact-Password` if protected) |
| GET | `/a/{id}/source` | Original submitted source (markdown or html) |
| GET | `/a/{id}/meta` | Public metadata JSON (no owner details) |
| GET | `/a/{id}/comments` | Every inline comment thread (open and resolved) as JSON |
| GET | `/a/{id}/guest` | Resolve an `X-Artifact-Guest` credential to its display name |
| GET | `/a/{id}/review` | Browser review UI: select text to comment, sandboxed artifact iframe; also the guest entry point via a `#invite=` fragment |
| GET | `/a/{id}/export/markdown` | Head version's Markdown source (or HTML when it has none) |
| GET | `/a/{id}/export/vault` | ZIP of a ready-to-open Obsidian vault (versions, comments, reasoning timeline) |
| GET | `/changelog` / `/changelog.md` | Rendered changelog (hub's own design) / raw source |
| GET | `/health` | Liveness check + service version + index stats |

Authenticated (`X-StorageApi-Token` + `X-Storage-Stack` headers):

| Method | Path | Description |
|---|---|---|
| POST | `/api/artifacts` | Publish `{html \| markdown \| git_url[, git_ref, git_path, git_token, git_username], title?, password?, accept_versions?}` → `{id, version, head_version, url, raw_url, meta_url, versions_url, ...}` |
| PUT | `/api/artifacts/{id}` | Add a live version and/or change `password` / `clear_password` / `accept_versions_mode` / `contributors` / `comments_mode` / `status` / `webhooks` (owner project only) |
| GET | `/api/artifacts` | List the caller's project's own artifacts (trashed ones included, with `webhooks_count`) |
| DELETE | `/api/artifacts/{id}` | **Soft delete**: move to the trash — public link dies, everything is kept and restorable (owner project only) |
| POST | `/api/artifacts/{id}/restore` | Undo the soft delete: back on the same share id, same status as before (owner project only) |
| DELETE | `/api/artifacts/{id}/purge` | **Permanent** delete: erase every version, comment thread and the meta record — no undo (owner project only) |
| POST | `/api/artifacts/{id}/rotate-link` | Mint a fresh share id; the old link (and the bare internal id) stop resolving immediately (owner project only) |
| GET | `/api/artifacts/{id}/stats` | View counts: `total`, `by_day` (last 30 UTC days), `by_kind` (owner project only) |
| POST | `/api/artifacts/{id}/invitations` | Invite a guest to comment `{name}` → one-time `review_url` with the secret in the URL fragment (owner project only) |
| GET | `/api/artifacts/{id}/invitations` | List an artifact's guest invitations, no secrets (owner project only) |
| DELETE | `/api/artifacts/{id}/invitations/{iid}` | Revoke one invitation; everyone else's keeps working (owner project only) |
| POST | `/api/artifacts/{id}/versions` | Submit a version `{html \| markdown \| git_url, title?, note?, base_version?}` — live for the owner, proposed for any other project (409 when `status` is `final` or trashed) |
| POST | `/api/artifacts/{id}/versions/{n}/promote` | Promote a proposal to live (owner project only) |
| DELETE | `/api/artifacts/{id}/versions/{n}` | Delete a version (owner), or withdraw your own proposal (contributor) |
| PUT | `/api/artifacts/{id}/head` | `{"mode": "latest"}` or `{"mode": "pinned", "version": n}` (owner project only) |
| POST | `/api/artifacts/{id}/comments` | Open a comment thread `{version, exact, prefix, suffix, body}` (403 if closed/allowlisted, 409 if frozen, 429 past the daily cap); also accepts an `X-Artifact-Guest` credential in place of a Storage token |
| POST | `/api/artifacts/{id}/comments/{tid}/replies` | Reply to a thread `{body}` (guest credential accepted here too) |
| POST | `/api/artifacts/{id}/comments/{tid}/resolve` | Resolve or reopen a thread `{"resolved": true \| false}` (owner, thread author, or the guest who opened it) |
| DELETE | `/api/artifacts/{id}/comments/{tid}` | Delete a thread (owner, thread author, or the guest who opened it) |

Every version, comment/reply, finalize, trash/restore and link-rotation event
also fires any webhooks the artifact has registered (`X-Hub-Signature-256`
HMAC-signed JSON, or Slack's `{"text": ...}` shape for a `hooks.slack.com`
URL) — see *Outbound webhooks* above.

## Quick start (curl)

Set `$HUB` to the deployed base URL, `your-token` to a real Keboola Storage
API token, and pick the `X-Storage-Stack` alias for your stack (`us`, `gcp-us`,
`eu`, `azure-eu`, `gcp-eu`, or any full `https://*.keboola.com` URL).

```bash
# Publish HTML
curl -s -X POST "$HUB/api/artifacts" \
  -H "X-StorageApi-Token: your-token" \
  -H "X-Storage-Stack: eu" \
  -H "Content-Type: application/json" \
  -d '{"html": "<!doctype html><html><body><h1>Hello</h1></body></html>", "title": "My report"}'

# Publish Markdown
curl -s -X POST "$HUB/api/artifacts" \
  -H "X-StorageApi-Token: your-token" \
  -H "X-Storage-Stack: eu" \
  -H "Content-Type: application/json" \
  -d '{"markdown": "# Title\n\nSome content."}'

# Publish from a public git repo
curl -s -X POST "$HUB/api/artifacts" \
  -H "X-StorageApi-Token: your-token" \
  -H "X-Storage-Stack: eu" \
  -H "Content-Type: application/json" \
  -d '{"git_url": "https://github.com/org/repo", "git_ref": "main", "git_path": "docs/report.md"}'

# Publish from a private git repo (git_token is transient — see below)
curl -s -X POST "$HUB/api/artifacts" \
  -H "X-StorageApi-Token: your-token" \
  -H "X-Storage-Stack: eu" \
  -H "Content-Type: application/json" \
  -d '{"git_url": "https://github.com/org/private-repo", "git_path": "docs/report.md", "git_token": "your-github-pat"}'

# Read (public, no token)
curl -s "$HUB/a/<id>/raw"

# Update (owner project only)
curl -s -X PUT "$HUB/api/artifacts/<id>" \
  -H "X-StorageApi-Token: your-token" \
  -H "X-Storage-Stack: eu" \
  -H "Content-Type: application/json" \
  -d '{"markdown": "# Updated"}'

# Delete (owner project only)
curl -s -X DELETE "$HUB/api/artifacts/<id>" \
  -H "X-StorageApi-Token: your-token" \
  -H "X-Storage-Stack: eu"
```

Add `"password": "secret"` to any publish/update body to protect the
artifact; readers then need `X-Artifact-Password: secret` (machines) or the
web unlock form (browsers).

### Versioning (curl)

```bash
# Open an artifact to submissions from other projects
curl -s -X PUT "$HUB/api/artifacts/<id>" \
  -H "X-StorageApi-Token: your-token" -H "X-Storage-Stack: eu" \
  -H "Content-Type: application/json" \
  -d '{"accept_versions": true}'

# Submit a version (any project; live for the owner, proposed for others)
curl -s -X POST "$HUB/api/artifacts/<id>/versions" \
  -H "X-StorageApi-Token: contributor-token" -H "X-Storage-Stack: eu" \
  -H "Content-Type: application/json" \
  -d '{"markdown": "# Updated", "note": "fix the totals table"}'

# Review: history, one version, and the diff
curl -s "$HUB/a/<id>/versions"
curl -s "$HUB/a/<id>/v/2" -H "X-StorageApi-Token: your-token" -H "X-Storage-Stack: eu"
curl -s "$HUB/a/<id>/diff/1..2?format=unified"

# Promote a proposal (owner project only)
curl -s -X POST "$HUB/api/artifacts/<id>/versions/2/promote" \
  -H "X-StorageApi-Token: your-token" -H "X-Storage-Stack: eu"

# Pin the head to one live version, or go back to "latest"
curl -s -X PUT "$HUB/api/artifacts/<id>/head" \
  -H "X-StorageApi-Token: your-token" -H "X-Storage-Stack: eu" \
  -H "Content-Type: application/json" \
  -d '{"mode": "pinned", "version": 1}'

# Withdraw your own proposal (contributor project)
curl -s -X DELETE "$HUB/api/artifacts/<id>/versions/2" \
  -H "X-StorageApi-Token: contributor-token" -H "X-Storage-Stack: eu"
```

`GET /a/{id}/versions?format=html` renders the same history as a styled page
with status badges and a diff link for every adjacent pair.

### Private repositories

`git_token` is a personal access token for the git host (GitHub PAT, GitLab
token, …); the optional `git_username` defaults to `x-access-token`, which is
what GitHub PATs and GitLab deploy tokens expect. Both are only valid
together with `git_url` — sending either on its own is a 422.

The token is **transient, exactly like the Storage token**: it is injected
into the clone URL for the duration of that one `git clone` subprocess and
nothing else. It is never written to the stored envelope (the recorded
`source.git` holds only the unauthenticated `url`, `ref`, `path` and
`commit`), never logged, never included in an error message, and never
returned in a response — so a later `PUT` re-publishing from the same private
repository has to send it again. Use the narrowest scope possible and revoke
the token when it is no longer needed. Note that the published artifact is
still served from a public URL: the token protects the clone, not the result.

## Install the agent / skill

Two ways to teach an AI agent this API, both served directly by the hub:

```bash
# Claude Code subagent — fully self-contained, no other skill needs to load
install -d ~/.claude/agents && curl -fsSL "$HUB/agent" -o ~/.claude/agents/artifact-hub.md

# SKILL.md — for a skills-based setup (e.g. skills/artifact-publisher/)
install -d ~/.claude/skills/artifact-publisher && curl -fsSL "$HUB/skill" -o ~/.claude/skills/artifact-publisher/SKILL.md
```

## Local development

```bash
uv sync

HUB_STORAGE_TOKEN=your-token \
HUB_STACK_URL=https://connection.eu-central-1.keboola.com \
HUB_SECRET_KEY=some-local-secret \
uv run uvicorn src.main:app --port 8050
```

Run the test suite:

```bash
uv run pytest
```

Tests use `InMemoryFilesBackend` in place of real Keboola Storage and mock
token verification (via `respx`/`monkeypatch`); no live Keboola calls are
made during testing.

## Configuration

All configuration comes from environment variables (`src/config.py`).
Required variables have no defaults — the app fails fast at startup if any
are missing. Everything else has a documented default, overridable via env.

| Variable | Default | Meaning |
|---|---|---|
| `HUB_STORAGE_TOKEN` | *required* | Storage API token for the host project (where serving envelopes live) |
| `HUB_STACK_URL` | *required* | Base URL of the host project's Keboola stack |
| `HUB_SECRET_KEY` | *required* | Secret used to sign password-unlock cookies |
| `HUB_PUBLIC_BASE_URL` | unset | Absolute base URL used when building returned artifact URLs, if the app can't infer it from the request |
| `HUB_CACHE_DIR` | `/tmp/artifact-cache` | Local disk LRU cache directory (not a source of truth) |
| `HUB_MAX_HTML_BYTES` | `15728640` (15 MB) | Max size of built HTML per artifact |
| `HUB_MAX_INLINE_IMAGE_BYTES` | `5242880` (5 MB) | Max size of a single image inlined as a data URI |
| `HUB_MAX_INLINE_TOTAL_BYTES` | `15728640` (15 MB) | Max total size of all inlined images per artifact |
| `HUB_GIT_CLONE_TIMEOUT_S` | `90` | Timeout for shallow git clones |
| `HUB_GIT_MAX_REPO_BYTES` | `209715200` (200 MB) | Max repository size accepted for a git-sourced publish |
| `HUB_GIT_ALLOW_PRIVATE_HOSTS` | `false` | Disable the SSRF guard that rejects `git_url` hosts resolving to private/loopback/link-local/reserved/metadata addresses — trusted self-hosting only |
| `HUB_CACHE_MAX_ENTRIES` | `200` | Max number of envelopes kept in the disk LRU cache |
| `HUB_UNLOCK_COOKIE_MAX_AGE_S` | `43200` (12 h) | Lifetime of a signed password-unlock cookie |
| `HUB_TOKEN_VERIFY_TIMEOUT_S` | `15` | Timeout for the `token verify` call to a caller's stack |
| `HUB_MAX_VERSIONS` | `50` | Live versions kept per artifact; older non-head, non-pinned ones are pruned (proposals are never pruned) |
| `HUB_MAX_VERSIONS_PER_DAY` | `20` | Versions one project may submit for one artifact per UTC day |
| `HUB_MAX_COMMENTS_PER_DAY` | `100` | Comment threads plus replies one project (or one guest invitation) may submit for one artifact per UTC day |
| `HUB_DIFF_MAX_BYTES` | `2097152` (2 MB) | Largest per-side payload the diff renderer (including `format=visual`'s rendered HTML) will process (413 above it) |
| `HUB_EXTRA_STACKS` | empty | Comma-separated extra stack URLs allowed beyond the `*.keboola.com` rule |
| `HUB_MAX_ENVELOPE_BYTES` | `20971520` (20 MB) | Largest persisted envelope/meta record the store will download or read from cache before refusing it as a DoS guard; `0` disables the bound |
| `HUB_MAX_PROPOSED_VERSIONS` | `50` | Per-artifact cap on retained proposed versions; the oldest proposals above this are pruned (proposals are never served as head, so this is always safe) |
| `HUB_TRUST_FORWARDED_HEADERS` | `false` | Trust `X-Forwarded-Host`/`X-Forwarded-Proto` to name the public origin when `HUB_PUBLIC_BASE_URL` is unset — only for local development behind a proxy; a direct client can forge these headers |
| `HUB_MAX_UNLOCK_ATTEMPTS_PER_HOUR` | `30` | Failed password-unlock attempts allowed per (artifact, client IP) per UTC hour before the gate answers 429 (each attempt costs a full PBKDF2 verification) |
| `HUB_STATE_SNAPSHOT_INTERVAL_S` | `300` | Seconds between snapshots of the rate-limit/analytics sidecar into Storage Files; `0` disables the background thread (snapshots then need an explicit call, as the tests do) |
| `HUB_STATE_MAX_SNAPSHOT_BYTES` | `52428800` (50 MB) | Largest state snapshot the hub will upload or restore; an oversized one is skipped with a warning rather than restored (`0` disables the bound) |
| `HUB_STATE_DB_FILENAME` | `state.sqlite3` | File name of the SQLite sidecar holding rate-limit counters and view analytics, under `HUB_CACHE_DIR` |
| `HUB_WEBHOOK_TIMEOUT_S` | `10` | Per-request HTTP timeout for one webhook delivery attempt |
| `HUB_WEBHOOK_MAX_ATTEMPTS` | `3` | Total POST attempts per (URL, event) before giving up; retries back off `2**n` seconds, capped at 60 |
| `HUB_MAX_WEBHOOKS_PER_ARTIFACT` | `5` | How many webhook URLs one artifact may register |
| `HUB_MAX_INVITATIONS_PER_ARTIFACT` | `20` | How many live guest invitations one artifact may hold at once (revoked ones are reclaimed automatically to make room) |

## Deployment to Keboola

The app is deployed from the public GitHub repository
`padak/kbc_ai_artifact` as a Keboola Data App:

```bash
kbagent data-app create \
  --project artifacts \
  --git-repo https://github.com/padak/kbc_ai_artifact \
  --git-public
```

Secrets are set with `kbagent data-app secrets-set` and never committed to
the repository:

- `HUB_STORAGE_TOKEN` — a Storage token scoped to the host project, minted
  with `kbagent token create ... --can-read-all-file-uploads`
- `HUB_STACK_URL` — the host project's stack URL
- `HUB_SECRET_KEY` — a random secret for signing unlock cookies

The nginx-to-app contract (nginx listens on `8888` and proxies to uvicorn on
`:8050`) is defined in `keboola-config/` (`keboola-config/nginx/sites/default.conf`,
`keboola-config/supervisord/services/app.conf`); it does not need to be
changed for normal application development.

## Security model

Artifact URLs are unguessable capabilities: possession of the URL is
sufficient to read the artifact, and there is no public listing or index to
discover them. Every artifact response sets `X-Robots-Tag: noindex` to keep
search engines out. Passwords are hashed with PBKDF2-SHA256 before storage in
the serving envelope, and unlocking one sets a signed cookie (via
`itsdangerous`) scoped to that artifact's own path (`path=/a/<share_id>`) and
to the current password's own hash, so a cookie signed under a since-changed
password no longer verifies, and an unlock on one artifact does not unlock
another.

**Capability revocation (0.7.0).** `POST /api/artifacts/{id}/rotate-link`
mints a fresh public share id and the previous one — plus the bare internal
id, once it differs from the share id — stops resolving immediately, with no
grace period; every unlock cookie issued under the old link goes with it.
`DELETE /api/artifacts/{id}` (trash) is the softer revocation: the link stops
resolving too, but nothing is discarded and `POST .../restore` brings it back
on the same URL; `DELETE .../purge` is the separate, irreversible one. A guest
invitation is revoked the same way, per person, via `DELETE
/api/artifacts/{id}/invitations/{iid}` — comments the guest already made stay,
only the capability to make new ones is withdrawn.

Community versioning is deliberately **moderated**: a version submitted by any
project other than the owner is stored as a proposal, is never served as the
head, and its content is readable only by the artifact owner and the version's
own author (both authenticate with the usual two management headers). Proposal
*metadata* — project name, note, timestamps, size, and (0.7.0) `base_version`
and `outdated` — is listed to anyone holding the capability URL, so treat
notes as public. Submissions are capped per contributing project per artifact
per day (`HUB_MAX_VERSIONS_PER_DAY`); since 0.7.0 that counter — and every
other rate-limit and view-count counter — lives in a SQLite sidecar
snapshotted into Storage Files rather than in process memory, so it survives a
redeploy and is shared across replicas instead of resetting per instance.

Since 0.6.0, `/a/{id}` and `/a/{id}/v/{n}` render an artifact inside a
`srcdoc` iframe sandboxed without `allow-same-origin`, so a published
document's own scripts run in an opaque origin and cannot reach the hub's own
origin — where `/admin` and `/review` keep a signed-in visitor's Storage token
in `sessionStorage` — regardless of same-origin cookie scoping. `/raw` is
unaffected: it stays the exact bytes for machine clients.

A guest invitation's secret rides the URL *fragment* (`#invite=...`), which
browsers never send to a server, and only ever reaches the API in the
`X-Artifact-Guest` header — the same discipline as the artifact password,
never a query string, path segment, or cookie. Outbound webhook URLs are
treated as equally sensitive: they are only ever echoed back in the `PUT`
response that set them, never in `GET /api/artifacts` (which reports a count
instead), and every non-Slack delivery is HMAC-signed
(`X-Hub-Signature-256`) so a receiver can reject a forged POST.

## Contributing

See `CLAUDE.md` at the repo root for project-specific rules before changing
code: the 3.11 f-string-backslash gotcha, why absolute URLs must go through
`base_url()`/the `public_origin` middleware instead of the raw `Host` header,
the Storage-tag-driven index rebuild, and the secrets-scrubbing discipline
around client and git tokens. In short — `uv run pytest tests/ -q` and
`python3.11 -m py_compile src/*.py` must both be clean before any commit, and
tests must use `InMemoryFilesBackend` with `verify_token` patched, never a
live Keboola call.
