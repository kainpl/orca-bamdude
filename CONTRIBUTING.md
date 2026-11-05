# Contributing

Thank you for helping. This page is how the repository works; `CLAUDE.md` holds the code's rules (read it — they
apply to people too).

## Branches

- `dev` — where work lands. **Branch from `dev` and open pull requests against `dev`.**
- `main` — released code only; it moves forward from `dev` when a stable version is released.
- Neither branch is ever force-pushed or deleted.

## Before a pull request

```bash
pip install -r requirements-dev.txt
python -m pytest
ruff check . && ruff format --check .
python tools/build_wheel.py      # dist/orca_bamdude_any.whl
```

- The runtime is the standard library only, and only the host adapter imports `orca`.
- A behaviour change comes with a test that fails without it, a line in `CHANGELOG.md` under `[Unreleased]`, and
  both READMEs updated.
- Conventional commits: `feat`, `fix`, `docs`, `test`, `refactor`, `chore`.

## Releases

Versions follow `X.Y.Z` for stable releases and `X.Y.ZbN` / `X.Y.ZrcN` for pre-releases, independent of BamDude's
versions. To release:

1. Set `__version__` in `src/orca_bamdude/__init__.py`.
2. Rename `## [Unreleased]` in `CHANGELOG.md` to `## [<version>] - <YYYY-MM-DD>` and start a new empty
   `[Unreleased]` above it.
3. A pre-release is tagged on `dev`; a stable release is tagged on `main` after `dev` is fast-forwarded into it.
   Push the tag `v<version>`.

The `Release` workflow then runs the whole CI, checks that the tag, the version and the changelog agree, and
publishes the wheel with its `SHA256SUMS`. Tags are never moved or deleted: a broken release is followed by the next
version.
