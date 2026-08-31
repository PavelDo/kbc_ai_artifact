# KBC Artifact Hub

A Keboola App (FastAPI) that hosts self-contained HTML/Markdown artifacts
under public, unguessable URLs. Anyone holding **any** Keboola Storage API
token, on **any** Keboola stack, can publish a document; the service returns
a public URL that humans can open in a browser and machines can fetch as raw
content, source, or metadata over a small JSON API.

## Features

- Publish HTML, Markdown, or a public git repository as a hosted artifact
- Unguessable capability URLs (`token_urlsafe`, 24 chars) — no public listing,
  `X-Robots-Tag: noindex` on every artifact response
- Optional password protection, with a web unlock form and a machine header
- Machine-readable API: `/context` manifest and a `/skill` SKILL.md an AI
  agent can read to learn how to publish, unassisted
- Markdown rendering with GFM tables, task lists, mermaid diagrams, and
  syntax-highlighted code
- Survives restarts: the only durable state is Keboola Storage Files; local
  disk is a cache, not a source of truth

## Architecture

**Publish flow.** A client sends content (`html`, `markdown`, or `git_url`)
plus a Storage token and stack to `POST /api/artifacts`. The service verifies
the token against the caller's own stack to establish project identity, then
builds the final HTML (rendering Markdown or cloning+resolving a git repo as
needed). Two copies are written:

- a **canonical copy** of the built HTML as a Storage File in the **author's
  own project**, uploaded with the author's token, tagged `kbc-artifact` and
  `artifact-id-<id>`;
- a **serving envelope** (HTML + metadata + password hash, as JSON) as a
  Storage File in the **host project**, tagged `artifact-hub`,
  `artifact-id-<id>`, and `artifact-owner-<key>`.

The service never persists client tokens; they are used only for the
duration of the request.

**Read flow.** `GET /a/{id}` (and `/raw`, `/source`, `/meta`) is served from
an in-memory index that maps artifact ids to their envelopes, backed by a
disk LRU cache. On startup, since the app container has no permanent disk,
the service rebuilds this index from scratch by listing every Storage File
tagged `artifact-hub` in the host project — the Storage Files of the host
project are the single source of truth, and local disk holds only a cache of
recently-served envelopes.

## API reference

Public (no auth):

| Method | Path | Description |
|---|---|---|
| GET | `/` | Landing page (docs) |
| POST | `/` | Returns 200 (platform health check) |
| GET | `/context` | Machine-readable manifest |
| GET | `/skill` | SKILL.md (`text/markdown`) teaching agents how to publish |
| GET | `/a/{id}` | Rendered artifact, or the password unlock form |
| POST | `/a/{id}/unlock` | Password form target; sets a signed unlock cookie |
| GET | `/a/{id}/raw` | Raw built HTML (password via `X-Artifact-Password` if protected) |
| GET | `/a/{id}/source` | Original submitted source (markdown or html) |
| GET | `/a/{id}/meta` | Public metadata JSON (no owner details) |
| GET | `/health` | Liveness check + index stats |

Authenticated (`X-StorageApi-Token` + `X-Kbc-Stack` headers):

| Method | Path | Description |
|---|---|---|
| POST | `/api/artifacts` | Publish `{html \| markdown \| git_url[, git_ref, git_path], title?, password?}` → `{id, url, raw_url, meta_url, ...}` |
| PUT | `/api/artifacts/{id}` | Update content and/or password (owner project only) |
| GET | `/api/artifacts` | List the caller's project's own artifacts |
| DELETE | `/api/artifacts/{id}` | Delete the serving copy (owner project only) |

## Quick start (curl)

Set `$HUB` to the deployed base URL, `your-token` to a real Keboola Storage
API token, and pick the `X-Kbc-Stack` alias for your stack (`us`, `gcp-us`,
`eu`, `azure-eu`, `gcp-eu`, or any full `https://*.keboola.com` URL).

```bash
# Publish HTML
curl -s -X POST "$HUB/api/artifacts" \
  -H "X-StorageApi-Token: your-token" \
  -H "X-Kbc-Stack: eu" \
  -H "Content-Type: application/json" \
  -d '{"html": "<!doctype html><html><body><h1>Hello</h1></body></html>", "title": "My report"}'

# Publish Markdown
curl -s -X POST "$HUB/api/artifacts" \
  -H "X-StorageApi-Token: your-token" \
  -H "X-Kbc-Stack: eu" \
  -H "Content-Type: application/json" \
  -d '{"markdown": "# Title\n\nSome content."}'

# Publish from a public git repo
curl -s -X POST "$HUB/api/artifacts" \
  -H "X-StorageApi-Token: your-token" \
  -H "X-Kbc-Stack: eu" \
  -H "Content-Type: application/json" \
  -d '{"git_url": "https://github.com/org/repo", "git_ref": "main", "git_path": "docs/report.md"}'

# Read (public, no token)
curl -s "$HUB/a/<id>/raw"

# Update (owner project only)
curl -s -X PUT "$HUB/api/artifacts/<id>" \
  -H "X-StorageApi-Token: your-token" \
  -H "X-Kbc-Stack: eu" \
  -H "Content-Type: application/json" \
  -d '{"markdown": "# Updated"}'

# Delete (owner project only)
curl -s -X DELETE "$HUB/api/artifacts/<id>" \
  -H "X-StorageApi-Token: your-token" \
  -H "X-Kbc-Stack: eu"
```

Add `"password": "secret"` to any publish/update body to protect the
artifact; readers then need `X-Artifact-Password: secret` (machines) or the
web unlock form (browsers).

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
| `HUB_CACHE_MAX_ENTRIES` | `200` | Max number of envelopes kept in the disk LRU cache |
| `HUB_UNLOCK_COOKIE_MAX_AGE_S` | `43200` (12 h) | Lifetime of a signed password-unlock cookie |
| `HUB_TOKEN_VERIFY_TIMEOUT_S` | `15` | Timeout for the `token verify` call to a caller's stack |
| `HUB_EXTRA_STACKS` | empty | Comma-separated extra stack URLs allowed beyond the `*.keboola.com` rule |

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
`itsdangerous`) scoped to that artifact's own path (`path=/a/<id>`), so an
unlock on one artifact does not unlock another. As a v1 caveat, all artifacts
are served from the same application origin, which makes the path-scoped
cookie a soft boundary rather than a hard one; this is an accepted risk for
v1 and may be revisited (e.g. per-artifact subdomains) later.
