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
  sighting is never a change. A release carries the state's incarnation and hold epoch from when it was made
  (`intent_basis`) and cannot undo a hold a later load made, in this state or in one started again since; past
  `MAX_HELD` everything is held and only what the tab names comes back. A state file that exists but cannot be read
  is never written over (`StateUnreadable`); only the import repairs it, as a new incarnation, and says so.
  `locking.py` — the file lock the journal and the state file share, and `read_text` / `replace`: the JSON files
  more than one thread or OrcaSlicer process reads and replaces — `state.json`, journal records and namespace,
  OrcaSlicer's `orca_plugins/config.json` — and the staged copy's replace go through them, because on Windows a
  lockless reader and a replace break each other (PermissionError) for as long as the other side holds the file. A
  new shared file goes through them too.
- `transport.py` — HTTP to the one configured server (JSON requests, streamed upload, camera stream). ⚠️ The connect
  is http.client's own (`HTTPConnection.connect`: every address of every family). OrcaSlicer's audit covers a
  `socket.connect` only inside a stdlib frame that raised a granted `http.client.connect`, with nothing but stdlib
  frames between; one made from plugin code is judged on its own target — a resolved address the user never granted —
  and a `sys.audit` raised from plugin code marks nothing. So the plugin never makes or connects a socket itself
  (`test_every_connect_runs_inside_http_clients_own_connect` applies that rule). `close()` is the owner's cancel: the
  transport holds each socket from the moment that connect returns it, through the TLS handshake (run by the plugin,
  on the held socket) to the end of the answer, so a close ends any of them, an answer read after it is not returned,
  a socket that connected after the close is refused unsent, and a request's end closes what was held; the name
  lookup (bounded only by the system resolver) and the TCP connect (the timeout per address, in turn) run to their own
  end. A stream already handed over stays its caller's.
- `api.py` — the connector contract on top of the transport.
- `inspector.py` — bounded reading of a sliced `.gcode.3mf` (plates by their `index` metadata child).
- `staging.py` — the plugin-owned hashed copy. `journal.py` — atomic per-operation records, written by revision
  under a folder lock.
- `camera_relay.py` — the loopback relay OrcaSlicer's FFmpeg plays the camera from (`/<secret>/<printer identity>` on
  127.0.0.1); the upstream and its credentials stay in the client. Admission is its owner's (`admits`), asked at every
  request, upstream open and renewal.
- `poller.py` — the one poll scheduler: channels asked only while wanted, one request in flight each, backoff,
  halt on auth/configuration failures, answers of an older connection generation dropped, staleness; the server's
  floor learnt later (`limit`) resets nothing.
- `devices.py` — `dev_id` = `bamdude-` + 32 hex of SHA-256 over the compact JSON array of the binding (canonical
  address, server id, storage epoch, actor id) and the printer's `connector_identity`; only a printer with a known
  `model_code` is announced or found.
- `dialect.py` — BamDude facts as the SSDP announcement, the `get_version` reply (`ota` only with a real firmware)
  and `push_status`, every key typed as the rc parser reads it; what is unknown is not sent, a printer BamDude cannot
  reach pushes only the keys true of the plugin, a second nozzle gets no nozzle temperature, no report carries a
  `job_id`. AMS per rc `DevFilaSystemParser`: type and extruder in `info`, AMS HT's own presence bits, a slot present
  by the firmware's bit (`exists`) and never by its filament profile; on two nozzles nothing goes on a side BamDude did
  not name (an unbound AMS and the external spools are left out).
- `fleet.py` — one connection generation's farm: capabilities + printers read together (on discovery, and every
  minute only under the tab's demand), the connected device's status every 5 s stale or not, accepted only while the
  binding still yields its `dev_id` and the status names its identity; reports every 3 s while fresh, none while
  stale; `connect_printer` answers at once and the result follows through `local_connect`; a refusal is a sentence,
  never `"5"`/`"-1"`; jobs and the camera only for the connected fresh device. Every read carries its generation and
  era; a namespace change proven by a status or by the list is a new era and rotates the relay's secret, as a rebind
  does. Every host message carries a stamp checked once more inside `HostCallbacks.call`, under its lock and the
  publication lock, to the end of the host call; every change that can void a stamp takes the publication lock and
  hands over only after releasing it. One lock order: callbacks → publication → state; the relay's own lock is taken under publication, never with
  the state lock held, and the relay asks `camera_admits` outside its lock. A status
  naming another printer revokes at once. The camera's admission is the fleet's (`camera_admits`), and every loop step
  takes a camera no longer admitted down. Issuing an address (`camera_url`: decide, set the relay's target, check)
  and every take-down are one step under the publication lock, like a hand-over: the relay's target is a value, and
  only that order keeps an older intent or take-down from touching a newer admission of the same printer. `rebind` and `stop` close the
  client: nothing of an older generation runs on. Host callbacks are the fleet's to call from its threads, never after
  the fence.
- `submit.py` — one send (spec r2 §6.2), host-independent, on a plugin thread: the writer gate before any I/O, the
  staged copy, the plates Orca's gesture means, the person's `Choice`, the journal record before the first request,
  one upload at a time (`UploadGate`), the server's `file_hash` checked against the copy, the submit written down
  first, 202 asked about until it ends (unknown at its deadline, never "failed"), a cancel after the submit asking the
  server to stop. `options_from_params` reads Orca's dialog codes (rc `PrintOption::getValueInt`: off 0, on 1,
  auto 2); an unknown code is refused by name.
- `choice_window.py` — the modal window over the staged copy (§6.3) and its `Answer`: callbacks only put and signal,
  the first answer wins, nothing after the fence; the waiting thread closes the window on a cancel without waiting
  for its `on_close`.
- `recovery.py` — the history's actions (§7): `reconcile` (a GET by the record's own id), `resume` (the same id:
  the body again when the server never received it, else the server's resume), `cancel`, `send_as_new` (only
  confirmed); 409/410 are final; another namespace's record refuses every action before any request. After a restart
  the Runtime reconciles a namespace's open records once (`Fleet.on_namespace`), GETs only.
- `exports.py` — the export folder (§8.4): a bounded listing (plain `.3mf`, no link, junction or reparse point) and
  one-time tokens bound to generation, folder, path, size and time; a path from the page is never taken. The tab's
  entry points are `orca_adapter.list_exports` / `manual_send`.
- `jobs.py` — the host's job thread for every `start_*` (§4.3, RF-6): it polls `cancel_fn` and returns -18 at once,
  calls `update_fn` itself (Upload stage, never Finished), and hands the host's callbacks to nothing that outlives
  it. 0 for accepted or unconfirmed (a negative code invites a duplicate send), -1 for a refusal.
- `i18n.py` + `locales/{en,uk}.json` — the plugin's sentences, chosen by OrcaSlicer's language code (`uk*` →
  Ukrainian).
- `service.py` — one `Runtime` per load (capability refcount, admission before the fence, shutdown steps with
  deadlines, settings snapshots by revision and generation), its `LoadContext` fence that every callback handed to
  OrcaSlicer checks, the `Notifier` — the only caller of the blocking `push_notification`, never waited for at
  shutdown — and the `StateWriter`, the one thread that writes `state.json`, closed in the same step as admission.
  Shutdown cleans up only after every owner is known to have finished; the UI thread never waits for the state
  file's lock. A held target gets a connection only once the state file no longer holds it: a release is judged by
  the writer under the lock and the snapshot is decided again after the write; a state file that cannot be read is
  no connection. Every published snapshot goes to the `Fleet`, outside the lock; the fleet's thread and the camera
  relay start at the first UI entry, and their stop is the Runtime's own shutdown step.
- `orca_adapter.py` — the only module that imports `orca`: install facts, the one permission request, the
  `BamDudePrinter` agent and the `BamDude` tab. `__init__.py` imports it only where `orca` exists. The live preset
  bundle is read on the UI thread only (`on_load` runs on OrcaSlicer's load thread); an override that cannot be
  read refuses the connection rather than falling back to base.
- `tests/` — pytest; `fakes.Server` is a scripted local HTTP server standing in for BamDude; `fake_orca.Host` stands
  in for OrcaSlicer 2.5.0-rc, modelling only what the rc source and the H-preflight runs proved. A host-facing change
  that needs behaviour the fake does not model is checked against the rc source first, then added to the fake.
  An autouse guard (`conftest.loopback_only`) refuses every name but loopback: the agent's threads read a configured
  server as soon as OrcaSlicer asks, and no test may reach past 127.0.0.1, not even with a DNS query.

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
