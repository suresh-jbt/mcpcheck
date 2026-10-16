<!-- Thanks for contributing. Keep the summary short; the checklist is the important part. -->

## Summary

<!-- What changes and why. Link the issue (`Fixes #123`) and, for a check or finding change, the spec clause. -->

## Test plan

<!-- How you verified it: which tests you added or changed, and the commands you ran (e.g. `uv run pytest -q`, a run against the fixture servers). -->

## Checklist

- [ ] `uv run pytest -q`, `uv run ruff check . && uv run ruff format --check .` and `uv run mypy` are green locally.
- [ ] The read-only guarantee holds: no new request outside the allow-list in README *Safety: what mcpcheck sends* (`tools/call` only with proven-invalid arguments to annotated read-only tools; never `resources/read`, `prompts/get`, `DELETE`, notifications, cross-origin redirects, network `$ref`).
- [ ] Every new finding code is registered in `src/mcpcheck/checks/codes.py` at the right severity with a verified spec URL; no existing code was renamed or removed (or the CHANGELOG lists it under *Changed* / *Removed*).
- [ ] New FAIL/WARN paths have a `Toggles` field in `tests/fixtures/misbehaving_server.py` and a test; the reference server still passes every check.
- [ ] README updated where it applies: *What it checks* table, options table (`uv run python scripts/gen_options_table.py README.md`), real-run blocks if the output changed; `uv run python scripts/check_readme.py README.md CHANGELOG.md CONTRIBUTING.md` passes.
- [ ] `CHANGELOG.md` has a line under `[Unreleased]`.
- [ ] Python 3.10 compatible syntax only; no new runtime dependency without discussion.
