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
  never more often than the server allows, backs off when the server is slow or unreachable, stops asking when the
  key or the address is wrong instead of retrying forever, and shows a printer it has not heard about for 30 seconds
  as unknown rather than idle.
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
  shown as known: a printer BamDude cannot reach stays selectable -- the farm can still queue work for it -- but its
  state and temperatures are left out, and a printer with two nozzles shows no nozzle temperature rather than the
  wrong one -- nor an external spool or an AMS on a side BamDude did not name. An AMS HT and an AMS 2 Pro show as
  what they are, and a slot shows a spool when the printer's own sensor says so, not because a filament was once set
  for it. When the farm stops answering, the plugin stops reporting and OrcaSlicer shows the printer offline about
  a minute after the last answer; it comes back by itself when the farm answers again. The camera plays only while
  the printer is connected and up to date and your key may watch it: it stops when the printer goes stale, is
  deselected, the connection or the key's rights change, or the plugin is turned off. After a restore, another key
  or another address, the earlier devices are not reused: OrcaSlicer says the printer belongs to an earlier BamDude
  connection, in English or Ukrainian as OrcaSlicer is set, and you choose it again. Without the tab open the plugin
  reads only the printer you selected, and turning the plugin off ends every request it had in flight.
- A reproducible wheel build (`tools/build_wheel.py`) and releases from `v<version>` tags: the wheel and its
  checksums are attached to each GitHub release.
