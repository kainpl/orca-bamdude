# Changelog

## [Unreleased]

- The plugin's host-independent core: connection settings that accept one server URL and refuse anything that could
  send the key elsewhere, an HTTP client that always verifies TLS and never follows a redirect, a bounded reader for
  sliced 3MF files, the plugin's own hashed copy of the file being sent, and a journal that records every operation
  before its first request so a lost answer never queues the work twice.
