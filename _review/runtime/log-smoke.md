# Phase-3 runtime smoke log

Scope: local, read-only ASGI smoke checks only. No application source was modified. All request state was in-memory; cache/temp/bytecode were directed under `_review/runtime/smoke-tmp/`; no external network or production backend was used.

## Inspection before execution

- Command: `sed -n '1,240p' /Users/padak/.codex/attachments/8f5a6a0c-996d-400e-b16f-8d05acda8554/pasted-text.txt && sed -n '1,240p' _review/codemap.md && sed -n '1,260p' _review/capabilities.md`
  - CWD: `/Users/padak/github/kbc_ai_artifact`
  - Environment: inherited read-only shell; no secrets printed or used.
  - Exit: `0`
  - Expected/proof: read the authoritative brief and required review artifacts before runtime work; confirmed FastAPI/ASGI, HTTP-first surface, and absence of CLI/MCP registrations in the canonical inventory.

- Command: `sed -n '150,280p' src/main.py; sed -n '220,340p' src/main.py; sed -n '540,620p' src/main.py; sed -n '1010,1425p' src/main.py; sed -n '1,180p' tests/conftest.py; sed -n '1,150p' tests/test_api.py`
  - CWD: `/Users/padak/github/kbc_ai_artifact`
  - Environment: inherited read-only shell; no secrets printed or used.
  - Exit: `0`
  - Expected/proof: inspected lifespan, route handlers, test fixtures, and `InMemoryFilesBackend`; demonstrated a safe seam for a local TestClient.

- Command: `rg -n -i "\\[project\\.scripts\\]|entry[-_ ]?points|console_scripts|argparse|click|typer|mcp|fastmcp|ModelContextProtocol|tool\\(|resource\\(|prompt\\(" pyproject.toml src tests README.md skills keboola-config -g '!_review/**' || true; printf '\\nroute decorators:\\n'; rg -n '^@app\\.(get|post|put|delete|patch)\\(' src/main.py; printf '\\npackage metadata:\\n'; rg -n '^Version:|^version\\s*=' pyproject.toml kbc_artifact_hub.egg-info/PKG-INFO`
  - CWD: `/Users/padak/github/kbc_ai_artifact`
  - Environment: inherited read-only shell; no secrets printed or used.
  - Exit: `0`
  - Sanitized output: no CLI declaration/console script or MCP framework/registration matched; only a docstring mention of “entry points” matched. FastAPI decorators enumerate the HTTP routes. Package metadata shows `pyproject.toml:3 version = "0.2.1"` and `kbc_artifact_hub.egg-info/PKG-INFO:3 Version: 0.2.0`.
  - Expected/proof: establish technology presence before runtime checks. CLI and MCP runtime discovery were skipped because package/runtime inspection found no such registrations; no invented checks were run.

- Command: `command -v python3; python3 --version; python3 - <<'PY' ... import fastapi, httpx, pydantic ... PY`
  - CWD: `/Users/padak/github/kbc_ai_artifact`
  - Environment: inherited read-only shell; no secrets printed or used.
  - Exit: `0`
  - Sanitized output: Python `/Users/padak/github/kbc_ai_artifact/.venv/bin/python3`, version `3.11.14`; FastAPI `0.141.1`, httpx `0.28.1`, Pydantic `2.13.5` import successfully.
  - Expected/proof: verify existing local runtime dependencies without installation; dependencies were available.

## Contained TestClient smoke

- Command (exact): `timeout 30 env -i PATH=/Users/padak/github/kbc_ai_artifact/.venv/bin:/opt/homebrew/bin:/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=0 TMPDIR=/Users/padak/github/kbc_ai_artifact/_review/runtime/smoke-tmp HUB_STORAGE_TOKEN=smoke-storage-token HUB_STACK_URL=https://smoke.invalid HUB_SECRET_KEY=smoke-secret-key HUB_CACHE_DIR=/Users/padak/github/kbc_ai_artifact/_review/runtime/smoke-tmp/cache /Users/padak/github/kbc_ai_artifact/.venv/bin/python3 _review/runtime/smoke-tmp/smoke.py`
  - CWD: `/Users/padak/github/kbc_ai_artifact`
  - Environment: `env -i`; only executable `PATH`, `PYTHONDONTWRITEBYTECODE=1`, deterministic `PYTHONHASHSEED=0`, `TMPDIR` and dummy `HUB_*` values. No credentials, network, subprocess, real Storage client, or production service. TestClient lifespan was patched to `InMemoryFilesBackend`; all cache/temp state stayed under `_review/runtime/smoke-tmp/`.
  - Exit: `0` within the 30-second timeout.
  - Sanitized output: one Starlette deprecation warning about httpx compatibility; startup hydration completed with `0 artifact(s)`. Results: `GET /` 200 (`text/html`), `GET /health` 200 (`{"artifacts":0,"hydrated":true,"status":"ok","version":"0.2.0"}`), `GET /context` 200 (`service=kbc-artifact-hub`, `23` advertised endpoint entries), `GET /skill` 200 (`text/markdown`), `GET /docs` 200 (`text/html`), `GET /openapi.json` 200 (`openapi=3.1.0`, `19` schema paths), and `GET /a/runtime-smoke-not-found` 404 (`{"error":"artifact not found","id":"runtime-smoke-not-found"}`).
  - Expected/proof: each advertised unauthenticated operation returns its documented status and content type; missing artifact returns a non-sensitive 404. All seven checks matched expected outcomes. The stale `0.2.0` version is recorded as `runtime-smoke-1`.

## Skipped checks

- CLI help/command discovery: skipped after package inspection found no `[project.scripts]`, console-script entry point, or CLI framework. There is no CLI registration to enumerate.
- MCP tool/resource/prompt discovery: skipped after package/source inspection found no MCP dependency, server, or registration. `/context` and `/skill` are ordinary HTTP routes, as documented in `_review/capabilities.md`.
- Full production Uvicorn launch: skipped because it would instantiate the real `KbcFilesBackend` and may contact external Storage; not necessary for the contained in-memory contract probe.
- Full pytest suite/build/package generation: skipped for this smoke role; repository test/build lifecycle and generated artifacts were not needed to establish the requested unauthenticated route behavior, and no packages were installed.
