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
  or replaced: old records are shown as unknown and are never resubmitted automatically. A record is changed only by
  a compare-and-set on its revision under the namespace folder's lock, which other OrcaSlicer processes honour (two
  overlap on every restart): a stale snapshot is refused, a newer format never overwritten. A new record is written
  only where no file is; a record once read only over a valid record of the same operation -- a deleted one is never
  brought back, a file `load` rejects never replaced. Note milestones through `Journal.update`, which compares the
  bytes it would write and refuses a `mutate` that changes the operation id, revision or schema.
- **Only the plugin's own copy is sent.** `staging.py` copies and hashes the file once; the inspector and the upload
  read only that copy.
- **One server, verified.** TLS verification has no off switch (a CA pasted as PEM text is the way to trust a
  self-signed server; never a file path), no redirect is followed, credentials go only to the configured authority,
  every answer is bounded, and an error shown to the user is the server's sentence at most — never a raw body, a
  header or the key.

## Layout

- `src/orca_bamdude/config.py` — connection settings: canonical server URL, API key, CA as PEM text, proxy headers.
- `settings.py` — the stored base, a preset override on top of it (address and CA only; the key and proxy headers
  come from base alone), the tab's masked view and keep/replace/clear edits, and the permission target each setting
  implies.
- `permits.py` — which user-set targets a load asks OrcaSlicer for: an unfinished load (a refusal or a stop before
  `on_load`) holds its targets, base included, until the tab asks again or a watched setting changes; a first
  sighting is never a change. A release carries the hold epoch it was made at and cannot undo a hold a later load
  made; past `MAX_HELD` everything is held and only what the tab names comes back. A state file that exists but
  cannot be read is never written over (`StateUnreadable`); only the import repairs it, and says so. `locking.py` —
  the file lock the journal and the state file share.
- `transport.py` — HTTP to the one configured server (JSON requests, streamed upload, camera stream).
- `api.py` — the connector contract on top of the transport.
- `inspector.py` — bounded reading of a sliced `.gcode.3mf` (plates by their `index` metadata child).
- `staging.py` — the plugin-owned hashed copy. `journal.py` — atomic per-operation records, written by revision
  under a folder lock.
- `camera_relay.py` — the loopback relay OrcaSlicer's FFmpeg plays the camera from (`/<secret>/<printer identity>` on
  127.0.0.1); the upstream and its credentials stay in the client.
- `poller.py` — the one poll scheduler: channels asked only while wanted, one request in flight each, backoff,
  halt on auth/configuration failures, answers of an older connection generation dropped, staleness.
- `service.py` — one `Runtime` per load (capability refcount, admission before the fence, shutdown steps with
  deadlines, settings snapshots by revision and generation), its `LoadContext` fence that every callback handed to
  OrcaSlicer checks, the `Notifier` — the only caller of the blocking `push_notification`, never waited for at
  shutdown — and the `StateWriter`, the one thread that writes `state.json`, closed in the same step as admission.
  Shutdown cleans up only after every owner is known to have finished; the UI thread never waits for the state
  file's lock. A held target gets a connection only once the state file no longer holds it: a release is judged by
  the writer under the lock and the snapshot is decided again after the write; a state file that cannot be read is
  no connection.
- `orca_adapter.py` — the only module that imports `orca`: install facts, the one permission request, the
  `BamDudePrinter` agent and the `BamDude` tab. `__init__.py` imports it only where `orca` exists. The live preset
  bundle is read on the UI thread only (`on_load` runs on OrcaSlicer's load thread); an override that cannot be
  read refuses the connection rather than falling back to base.
- `tests/` — pytest; `fakes.Server` is a scripted local HTTP server standing in for BamDude; `fake_orca.Host` stands
  in for OrcaSlicer 2.5.0-rc, modelling only what the rc source and the H-preflight runs proved. A host-facing change
  that needs behaviour the fake does not model is checked against the rc source first, then added to the fake.

## Commands

```bash
python -m pytest                 # unit tests
ruff check . && ruff format --check .
python tools/build_wheel.py      # dist/orca_bamdude_any.whl
```

## Versions and releases

`__version__` in `src/orca_bamdude/__init__.py` is the plugin version (`X.Y.Z`, pre-releases `X.Y.ZbN` /
`X.Y.ZrcN`), independent of BamDude's. The wheel's file name is fixed (`orca_bamdude_any.whl`): OrcaSlicer keys a
plugin's settings, storage and permissions by it. `tools/build_wheel.py` builds it reproducibly (sorted members, fixed
timestamps and modes; `.gitattributes` keeps every checkout LF so a local build equals CI's).

- Branches: `dev` takes the work (PRs target `dev`); `main` holds released code and only fast-forwards from `dev`.
  Neither is force-pushed or deleted (a repository ruleset enforces it), and `v*` tags are never moved or deleted.
- A release is a pushed `v<version>` tag: `.github/workflows/release.yml` runs the whole CI, `tools/release.py`
  checks tag = `v` + `__version__` and takes the notes from CHANGELOG.md's `## [<version>] - <date>` section, and the
  wheel is published with its `SHA256SUMS`. A stable tag must be on `main`; pre-releases are titled
  `OrcaBamDude v<version> (pre-release)`. Steps: `CONTRIBUTING.md`.

## Style

- Comments explain why, briefly. Name the OrcaSlicer source a host-facing decision relies on.
- Conventional commits (`feat`, `fix`, `docs`, `test`, `refactor`, `chore`). User-facing changes go into
  `CHANGELOG.md` as prose.
