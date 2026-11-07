# Changelog

## [Unreleased]

- The plugin's host-independent core: connection settings that accept one server URL and refuse anything that could
  send the key elsewhere, an HTTP client that always verifies TLS (against a private CA pasted as PEM text, never a
  file path), never follows a redirect and reaches a server name at whichever of its addresses answers, a bounded
  reader for sliced 3MF files, the plugin's own hashed copy of
  the file being sent, and a journal that records every operation before its first request so a lost answer never
  queues the work twice. Its records are versioned and older ones stay readable, and the history shows what was
  sent to another server, or before a restore, as unknown -- it is never sent again from there. A record is written only
  over the revision it was read at, under a lock other OrcaSlicer processes honour, so an older view of it -- or a
  second OrcaSlicer still closing down -- can never undo a newer write, bring back a deleted record, replace a damaged
  one or forget a notification already shown. On Windows, a file read while another thread or OrcaSlicer process
  saves it is waited for, never taken for a damaged one.
- A local relay for the printer camera. OrcaSlicer's video player cannot send an API key or proxy headers, so it plays
  a fixed address on 127.0.0.1 behind a long secret, and the plugin opens the farm's stream for it with its own
  credentials. One viewer at a time; a stream the server ends after its time limit is reopened under the same viewer,
  which is where the server checks the key again; a camera that sends no picture is reported as such, never as
  working.
- One scheduler for everything the plugin reads from the farm. It asks only while something is on screen or tracked,
  never more often than the server allows, backs off when the server is slow or unreachable, asks again when an
  answer was cut off on the way, stops asking when the key or the address is wrong instead of retrying forever, and
  shows a printer it has not heard about for 30 seconds as unknown rather than idle.
- The plugin registers with OrcaSlicer 2.5 as a printer agent named "BamDude" and a "BamDude" tab, and asks for its
  permissions once, at start-up: its own threads, OrcaSlicer's temporary model files, a local camera relay, and the
  farm address you configured. If you answer No, OrcaSlicer turns the plugin off; when you turn it back on, the
  addresses that prompt was about are not asked for again until you ask for them in the tab or change them, so one
  refusal does not repeat on every start; a request in the tab, or a change, made before that refusal cannot bring
  the address back, and the tab says when a request was not recorded. A printer preset can point the plugin at another address of the farm; the
  API key and proxy headers always come from the tab, never from the preset, because presets are exported and
  shared. OrcaSlicer's own configuration editor never shows the key.
- Farm printers in OrcaSlicer's device list. Choosing the BamDude agent lists the printers your key may see whose
  model OrcaSlicer knows, under their BamDude names; the selected one shows its state, temperatures, progress, AMS
  and external spool on the Device tab, and its live camera through the local relay. Nothing BamDude does not know is
  shown as known: a printer BamDude cannot reach stays selectable -- the farm can still queue work for it through
  Send, while OrcaSlicer's Print waits for a firmware version BamDude has not read -- but its state and temperatures
  are left out, and a printer with two nozzles shows no nozzle temperature rather than the
  wrong one -- nor an external spool or an AMS on a side BamDude did not name. An AMS HT and an AMS 2 Pro show as
  what they are, and a slot shows a spool when the printer's own sensor says so, not because a filament was once set
  for it. When the farm stops answering, the plugin stops reporting and OrcaSlicer shows the printer offline about
  a minute after the last answer; it comes back by itself when the farm answers again. The camera plays only while
  the printer is connected and up to date and your key may watch it: it stops when the printer goes stale, is
  deselected, the connection or the key's rights change, or the plugin is turned off. After a restore, another key
  or another address, the earlier devices are not reused: OrcaSlicer says the printer belongs to an earlier BamDude
  connection, in English or Ukrainian as OrcaSlicer is set, and you choose it again. Without the tab open the plugin
  reads only the printer you selected, and turning the plugin off ends every request it had in flight.
- Print and Send from OrcaSlicer to the farm. Pressing Print or Send for a BamDude printer opens a window over the
  plugin's own copy of the sliced file -- its name, size and hash, its plates, the printer -- where you choose what
  BamDude does: print when the queue allows, add it to the queue and start it by hand, or only put it in the library;
  how many copies; whether BamDude chooses the filament or the external spool feeds it. Print offers the queue first;
  Send offers the library first and queues nothing you did not tick. Bed levelling, flow and nozzle offset
  calibration, timelapse and first layer inspection go as OrcaSlicer's dialog set them; vibration calibration is not
  supported by BamDude and you are told so. A notification says where the file went -- the printer's queue or only
  the library -- because OrcaSlicer's own message after Send speaks of the printer's storage. Cancel in OrcaSlicer's
  dialog returns at once; if the farm already had the request, the plugin asks it to stop what it has not yet
  started. Cancel in the plugin's window sends nothing and leaves OrcaSlicer's dialog free, with its general "Failed
  to send" while the notification says the send was cancelled. An answer the farm did not give in time is reported as not yet confirmed, never as failed, so the same
  plate is not sent twice; when OrcaSlicer starts again the plugin asks the farm what became of it and says so. The
  AMS slots you chose in OrcaSlicer can be kept for a single plate when the printer's current state shows them;
  otherwise the window says they cannot be kept and BamDude chooses. Sending is enabled for OrcaSlicer 2.5.0-rc.
- The BamDude tab. It shows where the connection comes from -- the tab or a printer preset -- and edits it: the
  address, the API key (kept, replaced or removed; never shown), proxy headers whose values are never shown, a
  certificate authority pasted as PEM text and the export folder. "Check the connection" goes step by step --
  OrcaSlicer's permission, reaching the server, the BamDude connector, the key, the printers, the rights -- names
  what is missing and says in a sentence why a step failed. The farm view reads every printer only while "Live monitoring" is on: it starts off and switches
  itself off, saying so, ten minutes after your last action in the tab; "Refresh" reads once. A camera picture of a
  printer, sending a file from the export folder to a printer or to the AutoQueue, the history of what was sent with
  its actions (check, go on, cancel what has not started, send as new after a confirmation) and Open BamDude are
  there too. In a printer preset's Capabilities the agent's own page edits that preset's address and CA only, with
  "Restore to global"; the key and the proxy headers always come from the tab. The tab carries BamDude's icon.
- What you sent is followed for a day while OrcaSlicer is open: a notification when it is in the queue, when BamDude
  chooses a printer for an AutoQueue job, when it starts, when the first layer starts, and when it finishes, fails
  or is cancelled -- each once, also after a restart, and a print run again counts as a new run. When BamDude can no
  longer follow a job (moved to another queue, its printer outside the key's printers, the server restored), you are
  told once; when the key loses its rights or the connector is switched off, following pauses with an explanation
  and the tab's "Follow again". The history shows each send's last milestone.
- A reproducible wheel build (`tools/build_wheel.py`) and releases from `v<version>` tags: the wheel and its
  checksums are attached to each GitHub release.
