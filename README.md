# OrcaBamDude

[![CI](https://github.com/kainpl/orca-bamdude/actions/workflows/ci.yml/badge.svg?branch=dev)](https://github.com/kainpl/orca-bamdude/actions/workflows/ci.yml)

An OrcaSlicer plugin that sends sliced plates to a [BamDude](https://github.com/kainpl/bamdude) print farm over
its HTTP API.

[Українською](README.uk.md)

> **Status: not released yet.** The plugin is written and tested against a model of OrcaSlicer 2.5.0-rc; the
> acceptance run in the real OrcaSlicer is still to come. It needs a BamDude version with the slicer connector, which
> is not released yet either.

## What it does

- **Your farm's printers in OrcaSlicer's device list.** With the BamDude printer agent chosen, the printers your key
  may see appear under their BamDude names, with their state, temperatures, progress, AMS and camera on the Device
  tab.
- **Print and Send go to the farm.** OrcaSlicer's own Print and Send buttons open a window over the plugin's copy of
  the sliced file: print as soon as the queue allows, queue it and start it by hand, or only put the file in BamDude's
  library; how many copies; filament chosen by BamDude, from the external spool, or from the AMS slots you chose in
  OrcaSlicer.
- **What you sent is followed.** For a day, while OrcaSlicer is open: in the queue, printer chosen, printing, first
  layer started, finished, failed or cancelled — each said once.
- **The BamDude tab.** Connection settings with a step-by-step check, the farm with live monitoring and a camera
  picture, sending a file from an export folder, the history of what was sent, and Open BamDude.

### How it differs from BamDude's Virtual Printer

BamDude also takes prints through its Virtual Printer, which pretends to be a Bambu printer on the network. This
plugin is the other way in: it talks to BamDude's HTTP API with an API key, so it works across a reverse proxy or
from outside the farm's LAN, and a plate lands in a printer's queue or the AutoQueue exactly as if it had been queued
in BamDude's web interface.

## Requirements

- **OrcaSlicer 2.5.0-rc**, with *Preferences → Developer → Use printer agents instead of print hosts* enabled.
  Sending is enabled for this version only; on another one the plugin still monitors and shows the camera, and the
  tab says why it does not send. Earlier OrcaSlicer releases have no plugins at all; use the Virtual Printer there.
  The plugin has been tried in OrcaSlicer on Windows 11; on Linux and macOS only its tests have run, not OrcaSlicer.
- **A BamDude server with the slicer connector** (`/api/v1/connector/`) — the first BamDude release that has it.
- **A BamDude API key** for the plugin. What the plugin can do follows the key's rights: `printers:read` for the
  device list, `camera:view` for the camera, `library:upload` + `library:read_all` to put files in the library,
  `queue:create` + `library:read_all` to queue them. The tab's connection check names any right that is missing. A
  key can read every library file its owner can.

## Installing and updating

Download `orca_bamdude_any.whl` from [Releases](https://github.com/kainpl/orca-bamdude/releases), check it against
the release's `SHA256SUMS`, and install it in OrcaSlicer: *Plugins → Install local plugin*. An update is the same
file name: install it over the old one, then activate the plugin again and agree to its permissions — OrcaSlicer
asks again for every new version. The settings stay.

### Permissions

OrcaSlicer asks once, when the plugin starts, for what it needs:

- **its own threads** — to talk to the farm without holding up OrcaSlicer's window;
- **reading OrcaSlicer's temporary model files** — the sliced file Print and Send hand over;
- **a local camera relay on 127.0.0.1** — OrcaSlicer's video player cannot send an API key, so the plugin passes the
  farm's stream to it from your own computer;
- **the farm address you configured**, and nothing else;
- **reading the export folder**, if you set one;
- **opening the BamDude dashboard** in your browser — your server's address only.

If you answer **No**, OrcaSlicer turns the plugin off; turn it on again with *Plugins → Activate*. The addresses that
refusal was about are then not asked for again until you ask for them in the tab ("Ask at the next start") or change
them, so one refusal does not come back at every start. A new address saved in the tab is asked for at the next start
of OrcaSlicer.

## Setting up

1. Open the **BamDude** tab, enter the server address and the API key, and save. For a server with a self-signed
   certificate, paste its CA certificate as **PEM text** — never a file path. Certificate checks are never switched
   off.
2. **Check the connection** in the tab: it goes step by step and says what is missing.
3. In your printer preset choose *Printer Agent → BamDude*, then pick a farm printer on the Device tab. The first time,
   OrcaSlicer asks for an access code: any code will do — the farm's API key is what counts.

**The API key is stored in plain text** in OrcaSlicer's plugin configuration file (`orca_plugins/config.json` in
OrcaSlicer's data folder), as OrcaSlicer stores every plugin's settings. Once saved it is never shown in the tab, and
it is sent nowhere but to your server.

### Another address for one printer preset

A printer preset can point the plugin at another address of the farm — say, the LAN address at the farm and a public
one elsewhere — with its own CA. Edit it under *Printer settings → Printer Agent → choose BamDude again →
Capabilities: Configure… → BamDudePrinter*; "Restore to global" removes it. Only the address and the CA come from a
preset: the API key and the proxy headers always come from the tab, because preset files are exported and synced.

### After a server restore, a new address or a new key

The plugin's printers belong to one server, its data and one key. After you restore BamDude from a backup, change its
address or change the key, OrcaSlicer shows "Connect … failed" at every start until you choose the printer again on
the Device tab; the old entries stay under *My Device*, which is OrcaSlicer's own list. To move a farm to another
machine, use BamDude's backup and restore: a byte-for-byte copy at the same address cannot be told apart from the
original.

## Using it

- **Print** offers the queue first; **Send** offers the library first and queues nothing unless you choose to. A
  notification says where the file went. After Send, OrcaSlicer's own message speaks of "the printer's storage" —
  that is OrcaSlicer's text; the file is in BamDude's library.
- **A printer BamDude cannot reach** (switched off, say). OrcaSlicer's Print waits for the printer's firmware version,
  which the plugin passes on only when BamDude has read it from the printer; without it, Print's Send button stays
  grey after "Synchronizing device information timed out". Use Send: after the same wait of about half a minute it
  opens the plugin's window, where the plate can still go to the queue.
- **The AMS slots you chose in OrcaSlicer** can be kept for a single plate when the printer's current state still
  shows them; otherwise the window says so and BamDude chooses.
- **Cancel** in OrcaSlicer's dialog returns at once; if the farm already had the request, the plugin asks it to stop
  what has not started. Cancel in the plugin's window sends nothing; OrcaSlicer's dialog then shows its general
  "Failed to send the print job", and the plugin's notification says the send was cancelled.
- **An answer the farm did not give in time** is shown as *not confirmed yet*, never as a failure, and is checked
  again when OrcaSlicer starts. Check it in the tab's history rather than sending the same plate twice.
- **The export folder** (optional): the tab lists the sliced 3MF files in it and sends one to a printer or the
  AutoQueue.
- **Live monitoring** in the tab reads the whole farm only while it is on. It starts off and switches itself off ten
  minutes after your last action in the tab; each action starts the ten minutes again. "Refresh" reads once.
- **Notifications** appear on OrcaSlicer's Prepare and Preview tabs; the BamDude tab's history shows each send's last
  step.

## Development

Python 3.12 (the version OrcaSlicer embeds) and the standard library only; the tests need `pytest` (and Node.js for
the pages' own script — those tests are skipped without it).

```bash
pip install -r requirements-dev.txt
python -m pytest
ruff check . && ruff format --check .
python tools/build_wheel.py      # dist/orca_bamdude_any.whl
```

The tests never reach a network beyond `127.0.0.1` and never need OrcaSlicer or a BamDude server. Work lands on `dev`;
`main` holds released code. How to contribute and how releases are cut: [CONTRIBUTING.md](CONTRIBUTING.md).

## Licence

[GNU Affero General Public License v3.0](LICENSE), the same licence as BamDude.
