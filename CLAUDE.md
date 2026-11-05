# CLAUDE.md

Guidance for coding agents working in this repository. `AGENTS.md` points other tools here.

OrcaBamDude is an OrcaSlicer plugin that sends sliced plates to a BamDude server over BamDude's slicer connector
API. User docs are `README.md` (en) and `README.uk.md` (uk); keep both in step with behaviour.

## Rules

- **Never write a real server hostname, URL, API key, access code or proxy credential** anywhere: code, tests, docs,
  commit messages, logs or examples. Use `farm.example` or `127.0.0.1`.
- **The runtime is the standard library only.** OrcaSlicer loads the wheel into its embedded CPython 3.12; there is
  no dependency installation. Tests may use `pytest`.
- **Only the host adapter imports `orca`.** Every other module runs and is tested outside the slicer. Keep plugin-API
  calls in that one module, and verify them against OrcaSlicer's source at the release tag the plugin supports
  (`v2.5.0-rc`), not against the wiki.
- **BamDude owns the protocol.** The server side is `/api/v1/connector/` in the BamDude repository
  (`backend/app/api/routes/connector.py`, `backend/app/services/connector_submissions.py`). The plugin checks
  `protocol_version` from `GET /api/v1/connector/capabilities` and branches only on a refusal's stable
  `detail.error` code, never on its `detail.message` text, which the server translates.
- **An operation id is written down before its first request.** `journal.py` records it inside the namespace
  `connector_server_id` + `storage_epoch` + the key's `actor_id`; a lost answer is resolved by sending the same id
  again or asking the server, never by inventing a new operation. A changed namespace means the server was restored
  or replaced: old records are shown as unknown and are never resubmitted automatically.
- **Only the plugin's own copy is sent.** `staging.py` copies and hashes the file once; the inspector and the upload
  read only that copy.
- **One server, verified.** TLS verification has no off switch (a CA file is the way to trust a self-signed
  server), no redirect is followed, credentials go only to the configured authority, every answer is bounded, and an
  error shown to the user is the server's sentence at most — never a raw body, a header or the key.

## Layout

- `src/orca_bamdude/config.py` — connection settings: canonical server URL, API key, CA file, proxy headers.
- `transport.py` — HTTP to the one configured server (JSON requests, streamed upload, camera stream).
- `api.py` — the connector contract on top of the transport.
- `inspector.py` — bounded reading of a sliced `.gcode.3mf` (plates by their `index` metadata child).
- `staging.py` — the plugin-owned hashed copy. `journal.py` — atomic per-operation records.
- `camera_relay.py` — the loopback relay OrcaSlicer's FFmpeg plays the camera from (`/<secret>/<printer identity>` on
  127.0.0.1); the upstream and its credentials stay in the client.
- `tests/` — pytest; `fakes.Server` is a scripted local HTTP server standing in for BamDude.

## Commands

```bash
python -m pytest            # unit tests
ruff check . && ruff format --check .
```

## Versions and releases

`__version__` in `src/orca_bamdude/__init__.py` is the plugin version, independent of BamDude's. The wheel's file
name is fixed (`orca_bamdude_any.whl`): OrcaSlicer keys a plugin's settings, storage and permissions by it. Releases
are `v<semver>` tags of this repository.

## Style

- Comments explain why, briefly. Name the OrcaSlicer source a host-facing decision relies on.
- Conventional commits (`feat`, `fix`, `docs`, `test`, `refactor`, `chore`). User-facing changes go into
  `CHANGELOG.md` as prose.
