# OrcaBamDude

An OrcaSlicer plugin that sends sliced plates to a [BamDude](https://github.com/kainpl/bamdude) print farm over
its HTTP API.

[Українською](README.uk.md)

> **Status: in development, not released.** The host-independent core (connection settings, the HTTP client, the
> sliced-3MF inspector, the local copy of the file being sent and the operation journal) is written and tested.
> The OrcaSlicer side — the printer agent behind Orca's own Print/Send buttons, the BamDude tab and the camera —
> is not written yet. There is no wheel to install.

## What it is for

BamDude already accepts prints from a slicer through its Virtual Printer. This plugin is the other way in: it talks
to BamDude's HTTP API with an API key instead of pretending to be a printer on the network, so it also works across
a reverse proxy or from outside the farm's LAN. A plate it sends lands in one printer's queue or in BamDude's
AutoQueue, exactly as if it had been queued from BamDude's own web interface.

## Requirements

- **OrcaSlicer 2.5.0-rc** or later, with *Preferences → Developer → Use printer agents instead of print hosts*
  enabled. Earlier OrcaSlicer releases have no plugin support; use BamDude's Virtual Printer there.
- **A BamDude server with the slicer connector** (`/api/v1/connector/`). The connector is not in a released BamDude
  version yet.
- **A BamDude API key** for the plugin. It needs queue access to send prints and library access to upload the file.
  BamDude API keys cannot yet be limited to part of the library, so the key can read every library file its owner
  can.

## How it behaves

- **One server.** The plugin talks only to the URL you enter. TLS is always verified; a self-signed server is
  trusted through a CA file you point it at, never by switching verification off. Redirects are never followed.
- **No duplicates.** Every send is an operation with an id the plugin writes down before its first request. If the
  answer is lost, sending again returns what the server did the first time instead of queueing the work twice.
- **The bytes that were sliced.** The plugin copies the sliced file once into its own folder, hashes it, checks it
  and uploads that copy, so a file the slicer rewrites in the meantime cannot reach the printer.
- **Errors in your language.** BamDude refusals arrive translated by the server; the plugin never shows a raw server
  page.

## Development

Python 3.12 (the version OrcaSlicer embeds) and the standard library only; the tests need `pytest`.

```bash
pip install -r requirements-dev.txt
python -m pytest
ruff check . && ruff format --check .
```

The tests never reach a network beyond `127.0.0.1` and never need OrcaSlicer or a BamDude server.

## Licence

[GNU Affero General Public License v3.0](LICENSE), the same licence as BamDude.
