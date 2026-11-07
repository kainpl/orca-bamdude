"""Following what was sent (spec r2 §9.2-§9.4).

Milestones come only from the server's observation projection (§11 S3), which proves the chain receipt -> AutoQueue
row -> the queue row its promotion made -> that row's executions by immutable identities; numeric ids are no proof
and nothing else is read. Each milestone is said once, deduplicated in the operation's own journal record under its
lock by the three identities of S3 -- queued by the job, assigned by the assignment, every execution milestone by the
execution -- so a re-read, a second load or a restart says nothing again.

One poll asks a bounded number of pages, operations in turn, each operation's cursor walking all its pages. Errors are
classified here, before the poll scheduler sees them:

- addressed to this operation only (403 ``printer_scope`` / ``auto_requires_unrestricted_key``, 404, 409
  ``context_changed``, 410 ``receipt_compacted``): that operation is no longer followed, said once; the rest go on;
- global (401, any other 403 -- the route gate's refusal arrives as text without a code --, 503
  ``connector_disabled``, TLS, a redirect, an HTML answer, another protocol): tracking pauses, said once, and asks
  nothing until the settings change or the person asks again;
- temporary (a timeout, the connection, a 5xx without the connector's code, 429): that operation backs off.

An operation is followed for a day; one whose every job has ended is not asked again.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from .api import IncompatibleServer
from .journal import Journal, JournalError
from .transport import HttpError, TransportError

TRACK_SECONDS = 24 * 3600
PAGES_PER_POLL = 3
BACKOFF_SECONDS = (10.0, 20.0, 40.0, 80.0, 160.0, 300.0)
ADDRESSED = {
    (403, "printer_scope"),
    (403, "auto_requires_unrestricted_key"),
    (404, "operation_not_found"),
    (409, "context_changed"),
    (410, "receipt_compacted"),
}
# A job whose run failed or was cancelled may run again (the operator retries the same row: a new execution, §9.2),
# so only a completed or an unavailable job ends its following; the rest are followed for the day.
ENDED = ("completed", "unavailable")
WARNING = ("failed", "cancelled", "unavailable")
DONE_IDENTITY, DONE = "*", "tracking_done"
# The last milestone said, kept in the same record by the same locked write: the journal sorts the keys it
# writes, so their order is no timeline (H5 review R1). A record without it has no known last milestone.
LATEST = "latest:*"
UNPUBLISHED = ("pending", "preparing", "processing", "retryable_failure")


@dataclass(frozen=True)
class Event:
    kind: str  # the identity space: job, assignment or execution (§11 S3)
    identity: str
    milestone: str
    printer_id: int | None = None
    reason: str | None = None


def _printer(value) -> int | None:
    pid = value.get("id") if isinstance(value, dict) else None
    return pid if isinstance(pid, int) and not isinstance(pid, bool) else None


def candidates(jobs: list) -> list[Event]:
    """Every milestone the page shows, not yet deduplicated."""
    events = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        entry = str(job.get("entry_id") or "")
        identity = (job.get("job") or {}).get("identity") if isinstance(job.get("job"), dict) else None
        printer = _printer(job.get("printer"))
        if job.get("state") == "unavailable":
            events.append(Event("job", f"{entry}|{identity or '-'}", "unavailable", None, str(job.get("reason") or "")))
            continue
        if not identity:
            continue
        events.append(Event("job", f"{entry}|{identity}", "queued", printer))
        assignment = job.get("assignment")
        if isinstance(assignment, dict) and assignment.get("identity"):
            key = f"{entry}|{identity}|{assignment['identity']}"
            events.append(Event("assignment", key, "assigned", _printer(assignment.get("printer"))))
        for execution in job.get("executions") or []:
            if not isinstance(execution, dict) or not execution.get("identity"):
                continue
            key, state = f"{entry}|{identity}|{execution['identity']}", execution.get("state")
            if state == "printing":  # "dispatching" is not started: the printer has not taken it yet
                events.append(Event("execution", key, "started", printer))
            if execution.get("first_layer") is True:
                events.append(Event("execution", key, "first_layer", printer))
            if state in ("completed", "failed", "cancelled"):
                events.append(Event("execution", key, state, printer))
    return events


def _ended(job) -> bool:
    return isinstance(job, dict) and job.get("state") in ENDED


@dataclass
class _Follow:
    cursor: str | None = None
    settled: bool = True  # every job of this pass's pages so far has ended
    backoff_until: float = 0.0
    failures: int = 0


class Tracker:
    def __init__(
        self,
        context: Callable[[], tuple | None],
        storage: Callable[[], str | None],
        notify: Callable[[str, str], None],
        *,
        names: Callable[[int], str | None],
        say: Callable[[str], str],
        clock: Callable[[], float] = time.time,
    ):
        self._context, self._storage, self._notify = context, storage, notify
        self._names, self._say, self._clock = names, say, clock
        self._lock = threading.Lock()
        self._follows: dict[str, _Follow] = {}
        self._turn = 0
        self.paused: str | None = None
        self._changed = False  # something the open history should show: a milestone, an ending, a pause

    def take_changed(self) -> bool:
        """True once after a round that said something (H5 review R2): the open tab reads its history again."""
        with self._lock:
            changed, self._changed = self._changed, False
            return changed

    def _mark_changed(self) -> None:
        with self._lock:
            self._changed = True

    def resume(self) -> None:
        """New settings, or the person asked to follow again: a paused tracker asks again."""
        with self._lock:
            self.paused = None

    def _followed(self, record, now: float) -> bool:
        if record.phase != "accepted" or now - record.created_at > TRACK_SECONDS:
            return False
        return DONE not in record.milestones.get(f"job:{DONE_IDENTITY}", [])

    def poll(self) -> bool:
        """One round; ``True`` while something is followed (the channel stays wanted)."""
        if self.paused:
            return False
        context, storage = self._context(), self._storage()
        if context is None or storage is None:
            return True  # no connection now: nothing can be told, the next round tries
        client, capabilities = context[0], context[1]
        journal = Journal(storage, capabilities.namespace)
        now = self._clock()
        followed = [record for record in journal.records() if self._followed(record, now)]
        if not followed:
            return False
        with self._lock:
            start = self._turn % len(followed)
            self._turn += 1
        budget = PAGES_PER_POLL
        for record in followed[start:] + followed[:start]:
            follow = self._follows.setdefault(record.operation_id, _Follow())
            while budget > 0 and follow.backoff_until <= now and not self.paused:
                budget -= 1
                if not self._ask(record, follow, client, capabilities, journal, now):
                    break
            if budget <= 0 or self.paused:
                break
        return not self.paused

    def _ask(self, record, follow: _Follow, client, capabilities, journal: Journal, now: float) -> bool:
        """One page; ``True`` when the next page of this operation may be asked in the same round."""
        try:
            _status, body = client.observe(
                record.operation_id, capabilities.server_id, capabilities.storage_epoch, follow.cursor
            )
        except HttpError as exc:
            if (exc.status, exc.code) in ADDRESSED:
                self._end(journal, record, exc.code)
            elif exc.status == 400:
                follow.cursor, follow.settled = None, True  # a cursor the server no longer takes: start over
                self._back_off(follow, now)
            elif exc.status in (401, 403) or exc.code == "connector_disabled":
                self._pause(exc.code if exc.code not in ("", "http_error") else f"http_{exc.status}")
            else:
                self._back_off(follow, now)
            return False
        except TransportError as exc:
            if exc.kind in ("tls", "redirect", "protocol"):
                self._pause(exc.kind)  # a certificate, a redirect or a proxy's HTML page: asking again fixes nothing
            else:
                self._back_off(follow, now)
            return False
        except IncompatibleServer:
            self._pause("incompatible")
            return False
        observations = body.get("observations") if isinstance(body, dict) else None
        observations = observations if isinstance(observations, dict) else {}
        jobs = observations.get("jobs") if isinstance(observations.get("jobs"), list) else []
        self._announce(record, self._note(journal, record, candidates(jobs)))
        follow.failures = 0
        # The projection holds only published entries: while the operation still has unpublished ones (a partial
        # answer, a retryable failure, work in progress), the jobs seen so far are not all of it (H5 review R3).
        entries = body.get("entries") if isinstance(body.get("entries"), list) else []
        unpublished = body.get("state") == "processing" or any(
            isinstance(entry, dict) and entry.get("state") in UNPUBLISHED for entry in entries
        )
        follow.settled = follow.settled and not unpublished and all(_ended(job) for job in jobs)
        next_cursor = observations.get("next_cursor")
        if isinstance(next_cursor, str) and next_cursor:
            follow.cursor = next_cursor
            return True
        if follow.settled and jobs:
            self._note(journal, record, [Event("job", DONE_IDENTITY, DONE)])
        follow.cursor, follow.settled = None, True
        return False

    def _note(self, journal: Journal, record, events: list[Event]) -> list[Event]:
        """The events not noted before, noted now under the record's lock: each is said once, ever."""
        new: list[Event] = []

        def mutate(stored):
            new.clear()
            for event in events:
                if stored.note_milestone(event.kind, event.identity, event.milestone):
                    new.append(event)
            said = [event.milestone for event in new if event.milestone != DONE]
            if said:
                stored.milestones[LATEST] = [said[-1]]

        try:
            journal.update(record.operation_id, mutate)
        except (JournalError, OSError):
            return []  # nothing noted, nothing said: the next round tries again
        return list(new)

    def _announce(self, record, events: list[Event]) -> None:
        groups: dict[str, list[Event]] = {}
        for event in events:
            if event.milestone != DONE:
                groups.setdefault(event.milestone, []).append(event)
        for milestone, group in groups.items():
            first = group[0]
            name = self._names(first.printer_id) if first.printer_id is not None else None
            reason = self._say(f"reason.{first.reason}") if first.reason else ""
            text = (
                self._say(f"track.{milestone}")
                .replace("{file}", record.filename)
                .replace("{printer}", name or "")
                .replace("{count}", str(len(group)))
                .replace("{reason}", reason)
            )
            self._notify("Warning" if milestone in WARNING else "Regular", text)
            self._mark_changed()

    def _end(self, journal: Journal, record, code: str) -> None:
        if self._note(journal, record, [Event("job", DONE_IDENTITY, DONE)]):  # said once, by whoever noted it
            text = self._say("track.ended").replace("{file}", record.filename)
            self._notify("Warning", text.replace("{reason}", self._say(f"reason.{code}")))
            self._mark_changed()

    def _pause(self, code: str) -> None:
        with self._lock:
            if self.paused:
                return
            self.paused, self._changed = code, True
        self._notify("Warning", self._say("track.paused").replace("{reason}", self._say(f"reason.{code}")))

    @staticmethod
    def _back_off(follow: _Follow, now: float) -> None:
        delay = BACKOFF_SECONDS[min(follow.failures, len(BACKOFF_SECONDS) - 1)]
        follow.failures += 1
        follow.backoff_until = now + delay
