# Changelog

## [Unreleased]

- The plugin's host-independent core: connection settings that accept one server URL and refuse anything that could
  send the key elsewhere, an HTTP client that always verifies TLS -- against a private CA pasted as PEM text, never a
  file path -- and never follows a redirect, a bounded reader for sliced 3MF files, the plugin's own hashed copy of
  the file being sent, and a journal that records every operation before its first request so a lost answer never
  queues the work twice. Its records are versioned and older ones stay readable, and the history shows what was
  sent to another server, or before a restore, as unknown -- it is never sent again from there.
- A local relay for the printer camera. OrcaSlicer's video player cannot send an API key or proxy headers, so it plays
  a fixed address on 127.0.0.1 behind a long secret, and the plugin opens the farm's stream for it with its own
  credentials. One viewer at a time; a stream the server ends after its time limit is reopened under the same viewer,
  which is where the server checks the key again; a camera that sends no picture is reported as such, never as
  working.
- One scheduler for everything the plugin reads from the farm. It asks only while something is on screen or tracked,
  never more often than the server allows, backs off when the server is slow or unreachable, stops asking when the
  key or the address is wrong instead of retrying forever, and shows a printer it has not heard about for 30 seconds
  as unknown rather than idle.
- A reproducible wheel build (`tools/build_wheel.py`) and releases from `v<version>` tags: the wheel and its
  checksums are attached to each GitHub release.
