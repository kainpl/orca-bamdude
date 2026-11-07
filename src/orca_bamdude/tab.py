"""The tab's own logic (spec r2 §8), outside OrcaSlicer's API.

- :class:`Lease` -- "Live monitoring" (§8.2, RF-12): off at every load; switching it on opens ten minutes on the
  plugin's monotonic clock that each of the person's actions renews; the page's pulses and the poll's answers renew
  nothing; an action while it is off does not switch it on; a minimised window pauses the tab's demand without
  extending the lease; when it runs out it switches itself off and says so once.
- :func:`check_connection` -- the stepwise check (§8.1): permission, reach, protocol, key, printers, features, each
  refusal with its own answer, nothing uploaded or queued.
- :func:`fleet_rows` / :func:`history_rows` -- what the page is told, field by field: never a key, a hash, a body
  or an address.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from . import recovery
from .api import ConnectorClient, IncompatibleServer
from .journal import Journal, foreign_records
from .transport import HttpError, Transport, TransportError

LEASE_SECONDS = 600.0
FEATURES = ("monitor", "library_upload", "submissions", "camera_stream")


class Lease:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.RLock()  # the page's UI thread and the fleet's loop both ask
        self.on = False
        self._deadline = 0.0
        self._hidden = False
        self._expired = False

    def switch(self, on: bool) -> None:
        with self._lock:
            self.on = bool(on)
            self._expired = False
            if self.on:
                self._deadline = self._clock() + LEASE_SECONDS

    def activity(self) -> None:
        """A click or a key on the page: renews a lease that is on, and nothing else."""
        with self._lock:
            if self._check():
                self._deadline = self._clock() + LEASE_SECONDS

    def pulse(self) -> None:
        """The page's heartbeat: it renews nothing (§8.2 ⟨R23⟩)."""
        with self._lock:
            self._check()

    def visibility(self, *, hidden: bool) -> None:
        with self._lock:
            self._hidden = bool(hidden)
            self._check()

    def _check(self) -> bool:
        if self.on and self._clock() >= self._deadline:
            self.on, self._expired = False, True
        return self.on

    def wanted(self) -> bool:
        """Whether the tab's demand for the fleet is on now."""
        with self._lock:
            return self._check() and not self._hidden

    def expired(self) -> bool:
        """True once, after the lease ran out by itself: the page says why the switch went off."""
        with self._lock:
            self._check()
            expired, self._expired = self._expired, False
            return expired


def _step(name: str, ok: bool, code: str | None = None, **extra) -> dict:
    return {"step": name, "ok": ok, "code": code, **extra}


def check_connection(connection, *, blocked: str | None, timeout: float = 10.0) -> list[dict]:
    """The stepwise check of the current connection (no upload, no queue). Runs on a plugin thread."""
    if blocked is not None or connection is None:
        return [_step("permission", False, blocked or "missing")]
    steps = [_step("permission", True)]
    client = ConnectorClient(Transport(connection, timeout=timeout))
    try:
        try:
            capabilities = client.capabilities()
        except TransportError as exc:
            return [*steps, _step("reach", False, "html_answer" if exc.kind == "protocol" else exc.kind)]
        except HttpError as exc:
            if exc.status in (401, 403):
                return [
                    *steps,
                    _step("reach", True),
                    _step("key", False, "unauthorized" if exc.status == 401 else "forbidden"),
                ]
            return [*steps, _step("reach", False, exc.code or f"http_{exc.status}")]
        except IncompatibleServer as exc:
            return [*steps, _step("reach", True), _step("protocol", False, str(exc))]
        steps.append(_step("reach", True))
        if not capabilities.enabled:
            return [*steps, _step("protocol", False, "connector_disabled")]
        steps += [_step("protocol", True, server_version=capabilities.server_version), _step("key", True)]
        try:
            printers = client.printers()
        except (HttpError, TransportError) as exc:
            code = exc.code if isinstance(exc, HttpError) else exc.kind
            return [*steps, _step("printers", False, code)]
        known = sum(1 for p in printers if isinstance(p, dict) and p.get("model_code"))
        steps.append(_step("printers", True, count=len(printers), known=known))
        missing = [name for name in FEATURES if not capabilities.allows(name)]
        steps.append(_step("features", True, missing=missing, auto_target=capabilities.allows("auto_target")))
        return steps
    finally:
        client.close()


def fleet_rows(printers: list, statuses: dict) -> list[dict]:
    """The fleet as the tab shows it: names, models and state, nothing else."""
    rows = []
    for printer in printers:
        if not isinstance(printer, dict) or not isinstance(printer.get("id"), int):
            continue
        status = statuses.get(printer["id"]) if isinstance(statuses, dict) else None
        status = status if isinstance(status, dict) else {}
        rows.append(
            {
                "id": printer["id"],
                "name": str(printer.get("name") or ""),
                "model": printer.get("model_code") or None,
                "state": status.get("state") if isinstance(status.get("state"), str) else None,
                "progress": status.get("progress") if isinstance(status.get("progress"), (int, float)) else None,
                "remaining": status.get("remaining_time") if isinstance(status.get("remaining_time"), int) else None,
                "connected": status.get("connected") is True,
            }
        )
    return rows


def _row(record, journal: Journal, foreign: bool) -> dict:
    target = record.target if isinstance(record.target, dict) else {}
    return {
        "operation_id": record.operation_id,
        "file": record.filename,
        "created": record.created_at,
        "phase": record.phase,
        "error": record.error,
        "plates": list(record.plates),
        "target": target.get("kind"),
        "origin": record.origin,
        "foreign": foreign,
        "actions": [] if foreign else list(recovery.actions_for(record, journal)),
    }


def history_rows(journal: Journal, root: str, limit: int = 100) -> list[dict]:
    """This namespace's records and every other namespace's (shown as unknown, never sent again), newest first."""
    mine = [_row(record, journal, False) for record in journal.records()]
    theirs = [_row(record, journal, True) for _ns, record in foreign_records(root, journal.namespace)]
    return sorted(mine + theirs, key=lambda row: row["created"], reverse=True)[:limit]
