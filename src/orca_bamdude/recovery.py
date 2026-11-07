"""What the history does with a recorded send (spec r2 §7).

A record whose answer was lost is asked about by its own operation id (``reconcile``: a GET, nothing else), and after
a restart that is all that happens by itself. Everything else is the person's explicit action: ``resume`` (the same
id -- the body sent again when the server never received it, else the server runs what it has not published),
``cancel`` (the server fences what it has not published; published jobs stay) and ``send_as_new`` (a new operation
over the same library file, only when confirmed). 409 ``context_changed`` and 410 ``receipt_compacted`` are final.
A record of another namespace -- a restored or replaced server, another key -- is shown as unknown and never sent
again: every action refuses it before any request.
"""

from __future__ import annotations

import copy
import os
import time
import uuid

from .journal import Journal, JournalError, Record
from .submit import Outcome
from .transport import HttpError, TransportError

FINAL_CODES = ("context_changed", "receipt_compacted")
UNPUBLISHED = ("pending", "processing", "retryable_failure", "preparing")
TRACKED_SECONDS = 24 * 3600  # spec r2 §9.3: an accepted operation is followed for a day


def _foreign(record: Record, journal: Journal) -> bool:
    folder = record.folder or ""
    return os.path.normcase(os.path.abspath(folder)) != os.path.normcase(os.path.abspath(journal.folder))


def _unpublished(result) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("state") == "processing":
        return True
    return any(isinstance(e, dict) and e.get("state") in UNPUBLISHED for e in result.get("entries") or [])


def actions_for(record: Record, journal: Journal) -> tuple[str, ...]:
    """The history's buttons for this record."""
    if _foreign(record, journal):
        return ()
    if record.phase in ("submitting", "unknown"):
        return ("reconcile",)
    if record.phase == "uploaded" and record.body:
        return ("resume", "send_as_new")
    if record.phase == "accepted":
        return ("reconcile", "resume", "cancel") if _unpublished(record.result) else ("reconcile",)
    if record.phase == "finished" and record.body and record.error not in FINAL_CODES:
        return ("send_as_new",)
    return ()


def _update(journal: Journal, operation_id: str, **changes) -> None:
    def mutate(stored):
        for name, value in changes.items():
            setattr(stored, name, value)

    try:
        journal.update(operation_id, mutate)
    except JournalError:
        pass  # the record stays as it was; the next look at it asks the server again


def _settle(journal: Journal, record: Record, result, *, file_id=None) -> Outcome:
    state = result.get("state", "processing") if isinstance(result, dict) else "processing"
    kwargs = {"operation_id": record.operation_id, "library_file_id": file_id or record.library_file_id}
    if state in ("complete", "partial", "processing"):
        _update(journal, record.operation_id, phase="accepted", result=result, error=None)
        if state == "processing":
            return Outcome("unknown", "still_processing", answer=result, **kwargs)
        return Outcome("accepted", "queued" if state == "complete" else "partial", answer=result, **kwargs)
    _update(journal, record.operation_id, phase="finished", result=result, error=state)
    return Outcome("cancelled" if state == "cancelled" else "refused", state, answer=result, **kwargs)


def _refusal(journal: Journal, record: Record, exc: HttpError) -> Outcome:
    if exc.code in FINAL_CODES:
        _update(journal, record.operation_id, phase="finished", error=exc.code)
    return Outcome("refused", exc.code, exc.message, operation_id=record.operation_id)


def reconcile(record: Record, client, capabilities, journal: Journal) -> Outcome:
    """Ask the server what became of the operation. A GET only."""
    if _foreign(record, journal):
        return Outcome("refused", "foreign_namespace", operation_id=record.operation_id)
    try:
        _status, result = client.get(record.operation_id, capabilities.server_id, capabilities.storage_epoch)
    except HttpError as exc:
        if exc.code == "operation_not_found" and record.phase in ("submitting", "unknown") and record.body:
            # The submit never reached the server: nothing was queued, and the same id may be sent again.
            _update(journal, record.operation_id, phase="uploaded", error="not_received")
            return Outcome("refused", "not_received", operation_id=record.operation_id)
        if exc.code == "operation_not_found":
            _update(journal, record.operation_id, phase="finished", error=exc.code)
        return _refusal(journal, record, exc)
    except TransportError as exc:
        return Outcome("unknown", exc.kind, operation_id=record.operation_id)
    return _settle(journal, record, result)


def _submit(record: Record, body: dict, client, journal: Journal) -> Outcome:
    try:
        _status, result = client.submit(body)
    except HttpError as exc:
        _update(journal, record.operation_id, phase="finished", error=exc.code)
        return Outcome("refused", exc.code, exc.message, operation_id=record.operation_id)
    except TransportError:
        _update(journal, record.operation_id, phase="unknown")
        return Outcome("unknown", "submit_unknown", operation_id=record.operation_id)
    return _settle(journal, record, result)


def resume(record: Record, client, capabilities, journal: Journal) -> Outcome:
    """Go on under the same operation id: send the body again if the server never received it, else let it run
    what it has not published."""
    if _foreign(record, journal):
        return Outcome("refused", "foreign_namespace", operation_id=record.operation_id)
    if not record.body:
        return Outcome("refused", "nothing_to_resume", operation_id=record.operation_id)
    if record.phase == "uploaded":
        try:
            journal.update(record.operation_id, lambda stored: setattr(stored, "phase", "submitting"))
        except JournalError as exc:
            return Outcome("refused", "journal_busy" if str(exc) == "busy" else "journal")
        return _submit(record, record.body, client, journal)
    try:
        _status, result = client.resume(record.operation_id, capabilities.server_id, capabilities.storage_epoch)
    except HttpError as exc:
        return _refusal(journal, record, exc)
    except TransportError as exc:
        return Outcome("unknown", exc.kind, operation_id=record.operation_id)
    return _settle(journal, record, result)


def cancel(record: Record, client, capabilities, journal: Journal) -> Outcome:
    """The server fences what it has not published; a job already on a printer's queue stays."""
    if _foreign(record, journal):
        return Outcome("refused", "foreign_namespace", operation_id=record.operation_id)
    try:
        _status, result = client.cancel(record.operation_id, capabilities.server_id, capabilities.storage_epoch)
    except HttpError as exc:
        return _refusal(journal, record, exc)
    except TransportError as exc:
        return Outcome("unknown", exc.kind, operation_id=record.operation_id)
    return _settle(journal, record, result)


def send_as_new(record: Record, client, capabilities, journal: Journal, *, confirmed: bool) -> Outcome:
    """A new operation over the same library file -- only on the person's explicit confirmation."""
    if _foreign(record, journal):
        return Outcome("refused", "foreign_namespace", operation_id=record.operation_id)
    if not confirmed:
        return Outcome("refused", "confirm", operation_id=record.operation_id)
    if not record.body or not record.library_file_id:
        return Outcome("refused", "nothing_to_send", operation_id=record.operation_id)
    try:
        new = journal.create(
            record.filename, record.source_sha256, origin=record.origin, plates=record.plates, target=record.target
        )
        body = copy.deepcopy(record.body)
        body.update(
            operation_id=new.operation_id,
            server_id=capabilities.server_id,
            storage_epoch=capabilities.storage_epoch,
        )
        for entry in body.get("entries") or []:
            entry["entry_id"] = str(uuid.uuid4())

        def mutate(stored):
            stored.phase, stored.body, stored.library_file_id = "submitting", body, record.library_file_id

        journal.update(new.operation_id, mutate)
    except JournalError as exc:
        return Outcome("refused", "journal_busy" if str(exc) == "busy" else "journal")
    return _submit(new, body, client, journal)


def reconcile_pending(journal: Journal, client, capabilities, *, now: float | None = None) -> list:
    """After a restart: ask about every record whose answer is not final -- GETs only, nothing sent."""
    now = time.time() if now is None else now
    outcomes = []
    for record in journal.pending():
        if not record.body:
            continue  # an upload whose answer was lost: there is no operation to ask about
        if record.phase == "accepted" and (
            now - record.created_at > TRACKED_SECONDS or not _unpublished(record.result)
        ):
            continue
        outcomes.append((record, reconcile(record, client, capabilities, journal)))
    return outcomes
