## What and why

<!-- What changes for someone printing from OrcaSlicer, and why. Link the issue if there is one. -->

Fixes #

## How I tested it

<!-- Commands run; for anything that touches OrcaSlicer, the Orca version and OS you clicked through. -->

- [ ] `python -m pytest` passes, and a test that fails without this change is included
- [ ] Checked in OrcaSlicer: <!-- version + OS, or "not applicable" -->

## Checklist

- [ ] Branch is based on `dev` and the PR targets `dev`
- [ ] `ruff check . && ruff format --check .` is clean
- [ ] The runtime still imports nothing beyond the standard library, and only the host adapter imports `orca`
- [ ] No real server address, API key or proxy secret anywhere in the change
- [ ] `CHANGELOG.md` (user-facing changes) and both READMEs (behaviour changes) are updated
