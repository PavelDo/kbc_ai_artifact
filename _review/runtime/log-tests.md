# Phase-3 runtime verification: tests

Status: PASS. No runtime-contract finding was produced. The findings file is intentionally empty at `_review/findings/runtime-tests.jsonl`.

Scope and containment

- Repository root: `/Users/padak/github/kbc_ai_artifact`.
- Read before execution: authoritative review brief, `_review/codemap.md`, `_review/capabilities.md`, `pyproject.toml`, `tests/conftest.py`, the top of `tests/test_api.py`, all test references to temporary files/environment/processes, `keboola-config/setup.sh`, `keboola-config/supervisord/services/app.conf`, `keboola-config/nginx/sites/default.conf`, `kbc_artifact_hub.egg-info/PKG-INFO`, and `.git/hooks/*`.
- Existing `.venv` only; no package installation or dependency lifecycle command was run.
- All executed Python commands used `env -i` with only a minimal `PATH`, deterministic `PYTHONHASHSEED`, and `PYTHONPYCACHEPREFIX` pointing below `_review/runtime/tests-tmp/`. Test runs also set `NO_PROXY=*`; test fixtures provide only in-process dummy `HUB_*` values. No production credentials or values were printed.
- Test caches and pytest basetemp were explicitly below `_review/runtime/tests-tmp/`. Builder subprocesses create local temporary Git fixtures below pytest basetemp. No external service or non-local network was needed; auth HTTP calls are intercepted by `respx`.
- Test/compile commands were wrapped by `/opt/homebrew/bin/timeout` (180 seconds), shell CPU limit `ulimit -t 170`, file-size limit `ulimit -f 1048576`, and descriptor limit `ulimit -n 256`.

Inspected targets and commands

1. Command: `sed -n '1,240p' /Users/padak/.codex/attachments/8f5a6a0c-996d-400e-b16f-8d05acda8554/pasted-text.txt && echo '---CODEMAP---' && sed -n '1,260p' _review/codemap.md && echo '---CAPABILITIES---' && sed -n '1,300p' _review/capabilities.md`
   - CWD: `/Users/padak/github/kbc_ai_artifact`.
   - Environment: inherited read-only inspection environment; no secrets copied.
   - Exit: `0` (tool display truncated output only).
   - Proves: review instructions, threat/capability context, and runtime schema were read before execution.
2. Command: `pwd; printf '%s\\n' '--- top files ---'; rg --files -g '!_review/**' | sed -n '1,220p'; ...` (the complete command inspected `pyproject.toml`, deployment files, and `pytest`/fixture/process references).
   - CWD: `/Users/padak/github/kbc_ai_artifact`.
   - Environment: inherited read-only inspection environment.
   - Exit: `0`.
   - Proves: pytest configuration is only `[tool.pytest.ini_options] testpaths = ["tests"]`; deployment setup runs `cd /app && uv sync`; supervisor starts Uvicorn; Nginx proxies locally; tests use temporary paths and mocked/local subprocesses.
3. Command: `sed -n '1,240p' tests/conftest.py; sed -n '1,180p' tests/test_api.py; rg -n --glob 'tests/**' ...; find . ...`.
   - CWD: `/Users/padak/github/kbc_ai_artifact`.
   - Environment: inherited read-only inspection environment.
   - Exit: `0`.
   - Proves: fixtures use in-memory Storage and `tmp_path`; no project pytest plugin, Make/just/tox config, or active custom hook was found; builder Git calls are local fixture operations in tests.
4. Command: `mkdir -p _review/runtime/tests-tmp _review/findings; : > _review/findings/runtime-tests.jsonl; find .git/hooks ...; sed -n ...; rg -n ...`.
   - CWD: `/Users/padak/github/kbc_ai_artifact`.
   - Environment: inherited; only review directories/files were created.
   - Exit: `0`.
   - Proves: setup/lifecycle metadata and required output locations were resolved. The setup script was not executed because it invokes `uv sync`, which is an installation/dependency lifecycle operation prohibited by the brief.
5. Command: `command -v timeout || true; command -v gtimeout || true; .venv/bin/python --version; .venv/bin/python -m pytest --version; ...` followed by an `env -i` import/version probe.
   - CWD: `/Users/padak/github/kbc_ai_artifact`.
   - Environment: version probe used empty environment except `PATH` and review-local `PYTHONPYCACHEPREFIX`; no application credentials.
   - Exit: `0`.
   - Output: Python `3.11.14`, pytest `9.1.1`, FastAPI `0.141.1`, httpx `0.28.1`, respx `0.23.1`; kbcstorage version was unavailable from its module metadata.
   - Proves: required test runtime is available in the existing `.venv` without installation.
6. Command: `find src tests -type d -name '__pycache__' ...; git check-ignore -v src/__pycache__ tests/__pycache__`.
   - CWD: `/Users/padak/github/kbc_ai_artifact`.
   - Environment: inherited read-only inspection environment.
   - Exit: `0`.
   - Output: ignored source/test `__pycache__` directories already existed; no unignored source changes were reported. New compilation output was redirected to review-local prefix below.

Executed checks

1. Initial contained test attempt.
   - Exact command: `mkdir -p _review/runtime/tests-tmp/pytest-cache _review/runtime/tests-tmp/pytest-base _review/runtime/tests-tmp/pyc; ulimit -t 170; ulimit -v 1048576; exec /opt/homebrew/bin/timeout --signal=TERM --kill-after=5s 180s env -i PATH=/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin PYTHONHASHSEED=0 PYTHONDONTWRITEBYTECODE=0 PYTHONPYCACHEPREFIX="$PWD/_review/runtime/tests-tmp/pyc" NO_PROXY='*' .venv/bin/python -m pytest -q --disable-warnings --maxfail=0 -o cache_dir="$PWD/_review/runtime/tests-tmp/pytest-cache" --basetemp="$PWD/_review/runtime/tests-tmp/pytest-base"`.
   - CWD: `/Users/padak/github/kbc_ai_artifact`.
   - Sanitized environment: empty environment plus listed `PATH`, `PYTHONHASHSEED`, `PYTHONDONTWRITEBYTECODE`, `PYTHONPYCACHEPREFIX`, and `NO_PROXY`; conftest sets dummy non-production `HUB_*` values in-process.
   - Resource setup: wall timeout 180s and CPU limit 170s applied; zsh reported `ulimit: setrlimit failed: invalid argument` for the attempted virtual-memory limit, so this attempt was superseded by the valid-limit rerun below.
   - Exit: `0`.
   - Output: `219 passed, 1 warning in 5.58s`.
   - Expected: all existing tests pass without outside writes/network.
   - Proves: first test execution completed successfully; rerun below provides the authoritative resource-contained result.
2. Authoritative full test rerun.
   - Exact command: `ulimit -t 170; ulimit -f 1048576; ulimit -n 256; exec /opt/homebrew/bin/timeout --signal=TERM --kill-after=5s 180s env -i PATH=/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin PYTHONHASHSEED=0 PYTHONDONTWRITEBYTECODE=0 PYTHONPYCACHEPREFIX="$PWD/_review/runtime/tests-tmp/pyc-rerun" NO_PROXY='*' .venv/bin/python -m pytest -q --disable-warnings --maxfail=0 -o cache_dir="$PWD/_review/runtime/tests-tmp/pytest-cache-rerun" --basetemp="$PWD/_review/runtime/tests-tmp/pytest-base-rerun"`.
   - CWD: `/Users/padak/github/kbc_ai_artifact`.
   - Sanitized environment: empty environment plus listed `PATH`, `PYTHONHASHSEED`, `PYTHONDONTWRITEBYTECODE`, `PYTHONPYCACHEPREFIX`, and `NO_PROXY`; conftest sets dummy non-production `HUB_*` values in-process.
   - Resource limits: wall 180s, CPU 170s, file size 1,048,576 blocks, descriptors 256; all commands completed within limits.
   - Exit: `0`.
   - Output: `219 passed, 1 warning in 3.02s`.
   - Expected: all configured tests pass.
   - Proves: the complete advertised pytest suite executes successfully under the existing environment with test state contained under `_review/runtime/tests-tmp/`.
3. Python syntax/bytecode compilation.
   - Exact command: `mkdir -p _review/runtime/tests-tmp/compile-cache; ulimit -t 170; ulimit -f 1048576; ulimit -n 256; exec /opt/homebrew/bin/timeout --signal=TERM --kill-after=5s 180s env -i PATH=/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin PYTHONHASHSEED=0 PYTHONDONTWRITEBYTECODE=0 PYTHONPYCACHEPREFIX="$PWD/_review/runtime/tests-tmp/compile-cache" .venv/bin/python -m compileall -q -f src tests`.
   - CWD: `/Users/padak/github/kbc_ai_artifact`.
   - Sanitized environment: empty environment plus listed `PATH`, `PYTHONHASHSEED`, `PYTHONDONTWRITEBYTECODE`, and review-local `PYTHONPYCACHEPREFIX`.
   - Resource limits: wall 180s, CPU 170s, file size 1,048,576 blocks, descriptors 256.
   - Exit: `0`; output: empty.
   - Expected: all Python files under `src/` and `tests/` compile with no syntax errors, with bytecode only under review-local prefix.
   - Proves: Python syntax and bytecode compilation succeeds for the application and test tree.

Containment verification

- Command: `printf 'source_pycache_count='; find src tests -type d -name '__pycache__' ...; git status --short --untracked-files=all | awk '$2 !~ /^_review\\// ...'; printf 'runtime_file_count='; find _review/runtime/tests-tmp -type f | wc -l; ...`
- CWD: `/Users/padak/github/kbc_ai_artifact`.
- Environment: inherited read-only inspection environment.
- Exit: `0`.
- Output: no modified/untracked path outside `_review/`; `runtime_file_count=3786`; generated caches/bytecode were under `_review/runtime/tests-tmp/`. Ignored source/test `__pycache__` directories predated or are ignored; no source file was changed by this phase.
- Expected/proves: runtime artifacts stayed within the allowed review runtime area and application worktree remained unchanged.

Skipped checks

- `keboola-config/setup.sh`: skipped because it executes `uv sync` (package installation/dependency lifecycle and potentially writes outside the review runtime area).
- No build/type-check command was advertised in `pyproject.toml` or other inspected config, so none was invented or run.
- No route/CLI/MCP enumeration was run by this tests-focused agent; the inspected canonical capability matrix records no CLI or MCP implementation, and any route/runtime enumeration is left to the separately assigned runtime scope.
