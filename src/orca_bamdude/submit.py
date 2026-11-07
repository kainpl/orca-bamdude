"""Sending a sliced plate to the farm (spec r2 §6): one operation, from the plugin's own copy to the server's answer.

Runs on a plugin thread (§4.3). The host's job thread only orchestrates: it hands a :class:`Request` over, polls its
own cancel and returns; everything here -- the copy, the inspection, the journal, the network -- happens on the
plugin's side, and a cancel is an event, never a host callback.

The order is the spec's: the writer gate before any I/O, the plugin's hashed copy (never the host's path, which the
slicer rewrites), the plates, the person's choice, the journal record before the first request, the upload, the
submit. A refusal before the upload leaves no record; from the upload on, the record says how far it got. A submit
whose answer is lost is ``unknown`` -- checked later against the server, never called a failure, never sent again
under a new id. A cancel before the submit queues nothing (a file already uploaded stays in the library); after it,
the server is asked to stop what it has not yet published.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from . import inspector, staging
from .api import Capabilities
from .inspector import Inspection, InspectionError
from .journal import Journal, JournalError
from .staging import Staged, StagingError
from .transport import HttpError, TransportError

ACTIONS = ("library_only", "queue_manual", "queue_asap")
QUEUE_ACTIONS = ("queue_manual", "queue_asap")
FILAMENT_MODES = ("auto", "pinned", "external_only")
TERMINAL = ("complete", "partial", "cancelled", "rejected")
UPLOAD_WAIT_SECONDS = 600.0

# Orca's print dialog: PrintOption::getValueInt (rc SelectMachine.cpp) -- off 0, on 1, auto 2 -- handed over as the
# PrintParams auto_* fields; the task_* booleans are only "== on" of the same choice.
_CODES = {0: "off", 1: "on", 2: "auto"}
_CALIBRATIONS = (
    ("bed_levelling", "auto_bed_leveling"),
    ("flow_cali", "auto_flow_cali"),
    ("nozzle_offset_cali", "auto_offset_cali"),
)


class OptionError(ValueError):
    """An option code this plugin does not know: refused by name, never guessed (§6.3)."""

    def __init__(self, option: str):
        super().__init__(option)
        self.option = option


def options_from_params(params) -> tuple[dict, tuple[str, ...]]:
    """The print options Orca's dialog chose, as the connector names them, and the warnings they carry."""
    options: dict = {}
    for name, attribute in _CALIBRATIONS:
        code = getattr(params, attribute, 2)
        if isinstance(code, bool) or code not in _CODES:
            raise OptionError(name)
        options[name] = _CODES[code]
    options["timelapse"] = bool(getattr(params, "task_record_timelapse", False))
    options["layer_inspect"] = bool(getattr(params, "task_layer_inspect", False))
    warnings = ("vibration_cali_unsupported",) if getattr(params, "task_vibration_cali", False) else ()
    return options, warnings


class FilamentError(ValueError):
    """A slot Orca named that the printer's fresh state cannot confirm: no pin is offered, nothing is guessed."""


_UNUSED = (255, 255)  # Orca's ams_mapping2 entry for a filament the plate does not use
_EXTERNAL = (255, 0)  # Orca's main external holder: the one vt_tray the dialect shows it (dialect.push_status)
_PLAIN_UNITS, _HT_FIRST, _HT_UNITS = 4, 128, 8


def pinned_mapping(params, status) -> tuple[int, ...] | None:
    """The slots Orca's dialog chose (``ams_mapping2``, one entry per filament), as BamDude's feed source ids, or
    ``None`` when it named none.

    The ids are BamDude's own (``printer_feed_snapshot``): a plain AMS slot ``unit * 4 + slot``, an AMS HT its unit
    id, an external holder its tray id. Orca saw the farm's units under their BamDude ids, and an external spool only
    when the printer has one nozzle and one holder -- so a slot is confirmed only against the same fresh status, and
    anything else raises :class:`FilamentError`. The legacy ``ams_mapping`` is not read: it says -1 for an external
    spool (P0) and cannot tell one from an unused filament."""
    raw = getattr(params, "ams_mapping2", "")
    if not raw:
        return None
    try:
        entries = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise FilamentError("mapping") from exc
    if not isinstance(entries, list) or not isinstance(status, dict):
        raise FilamentError("mapping")
    units = {}
    for unit in status.get("ams") or []:
        if isinstance(unit, dict) and isinstance(unit.get("id"), int):
            units[unit["id"]] = {t.get("id") for t in unit.get("tray") or [] if isinstance(t, dict)}
    holders = [h for h in status.get("vt_tray") or [] if isinstance(h, dict)]
    nozzles = status.get("nozzles") if isinstance(status.get("nozzles"), list) else []
    pins = []
    for entry in entries:
        ams_id = entry.get("ams_id") if isinstance(entry, dict) else None
        slot = entry.get("slot_id") if isinstance(entry, dict) else None
        if any(isinstance(v, bool) or not isinstance(v, int) for v in (ams_id, slot)):
            raise FilamentError("mapping")
        if (ams_id, slot) == _UNUSED or ams_id < 0 or slot < 0:
            pins.append(-1)
        elif 0 <= ams_id < _PLAIN_UNITS and slot in units.get(ams_id, ()):
            pins.append(ams_id * 4 + slot)
        elif _HT_FIRST <= ams_id < _HT_FIRST + _HT_UNITS and slot == 0 and 0 in units.get(ams_id, ()):
            pins.append(ams_id)
        elif (ams_id, slot) == _EXTERNAL and len(nozzles) <= 1 and len(holders) == 1:
            holder = holders[0].get("id")
            if isinstance(holder, bool) or not isinstance(holder, int) or not holders[0].get("tray_type"):
                raise FilamentError("external")
            pins.append(holder)
        else:
            raise FilamentError("topology")
    return tuple(pins) if any(pin >= 0 for pin in pins) else None


@dataclass(frozen=True)
class Target:
    kind: str  # "printer" or "auto"
    printer_id: int | None = None
    identity: str | None = None
    model: str | None = None
    location_id: int | None = None

    @classmethod
    def printer(cls, printer_id: int, identity: str) -> Target:
        return cls("printer", printer_id=int(printer_id), identity=identity)

    @classmethod
    def auto(cls, model: str, location_id: int | None = None) -> Target:
        return cls("auto", model=model, location_id=location_id)

    def wire(self) -> dict:
        if self.kind == "printer":
            return {"kind": "printer", "printer_id": self.printer_id, "printer_identity": self.identity}
        wire = {"kind": "auto", "target_model": self.model}
        if self.location_id is not None:
            wire["target_location_id"] = self.location_id
        return wire


@dataclass(frozen=True)
class Request:
    """What to send. ``kind`` is Orca's Print, Orca's Send or the tab's manual send."""

    kind: str
    source: str
    name: str
    target: Target
    plate_index: int | None = None
    options: dict = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    pins: tuple[int, ...] | None = None  # the slots Orca's dialog chose, as BamDude ids (``pinned_mapping``)

    @classmethod
    def native_print(cls, source, name, target, *, plate_index, options=None, warnings=(), pins=None) -> Request:
        pins = tuple(pins) if pins is not None else None
        return cls("print", source, name, target, int(plate_index), dict(options or {}), tuple(warnings), pins)

    @classmethod
    def native_send(cls, source, name, target, *, plate_index) -> Request:
        return cls("send", source, name, target, int(plate_index))

    @classmethod
    def manual(cls, source, name, target) -> Request:
        return cls("manual", source, name, target)

    @property
    def origin(self) -> str:
        return "manual" if self.kind == "manual" else "native"

    @property
    def filename(self) -> str:
        """The library's name for it: Orca's job name, a sliced 3MF."""
        return self.name if self.name.lower().endswith(".3mf") else f"{self.name}.gcode.3mf"


@dataclass(frozen=True)
class Offer:
    """What the person chooses over: the staged copy and its plates (§6.3)."""

    request: Request
    staged: Staged
    inspection: Inspection
    plates: tuple[int, ...]
    proposed: tuple[int, ...]
    default_action: str


@dataclass(frozen=True)
class Choice:
    action: str
    plates: tuple[int, ...]
    copies: int = 1
    filament: dict = field(default_factory=lambda: {"mode": "auto"})
    options: dict | None = None


@dataclass(frozen=True)
class Outcome:
    """``accepted``, ``cancelled``, ``refused`` or ``unknown`` (a request went out and its answer did not come)."""

    result: str
    code: str
    message: str = ""
    operation_id: str | None = None
    library_file_id: int | None = None
    answer: dict | None = None


class Cancelled(Exception):
    pass


class UploadGate:
    """One upload at a time for the whole plugin, Orca's Print and Send and the tab alike (§6.2 step 3)."""

    def __init__(self):
        self._lock = threading.Lock()

    @contextlib.contextmanager
    def slot(self, cancel: threading.Event, deadline: float):
        end = time.monotonic() + deadline
        while not self._lock.acquire(timeout=0.05):
            if cancel.is_set():
                raise Cancelled
            if time.monotonic() >= end:
                raise TimeoutError
        try:
            yield True
        finally:
            self._lock.release()


def _refused(code: str, message: str = "", **kwargs) -> Outcome:
    return Outcome("refused", code, message, **kwargs)


def _proposal(request: Request, plates: tuple[int, ...]) -> tuple[tuple[int, ...], str] | Outcome:
    """Which plates Orca's gesture means (§6.2 step 6), and the action offered first."""
    if request.kind == "print":
        if len(plates) == 1 and plates[0] != request.plate_index:
            return _refused("plate_ambiguous")
        return plates, "queue_asap"
    if request.kind == "send" and request.plate_index:
        if request.plate_index not in plates:
            return _refused("plate_ambiguous")
        return (request.plate_index,), "library_only"
    return (), "library_only"  # all plates of a Send, or the tab: the person picks them for a queue


def _check(choice: Choice, options: dict, request: Request, plates, capabilities: Capabilities) -> Outcome | None:
    """What the choice asks against what the key and the server allow -- before anything is sent."""
    if choice.action not in ACTIONS:
        return _refused("action")
    if choice.action == "library_only":
        return None
    if not capabilities.allows("submissions"):
        return _refused("not_permitted")
    if request.target.kind == "auto":
        if not capabilities.allows("auto_target"):
            return _refused("not_permitted")
        if options.get("nozzle_offset_cali", "auto") != "auto":
            return _refused("option_not_for_autoqueue")
    chosen = tuple(choice.plates)
    if not chosen or any(plate not in plates for plate in chosen) or len(set(chosen)) != len(chosen):
        return _refused("no_plates")
    limits = capabilities.limits
    copies = int(choice.copies)
    if copies < 1 or copies > int(limits.get("max_copies_per_entry", 999)):
        return _refused("copies")
    if len(chosen) > int(limits.get("max_entries", 64)) or copies * len(chosen) > int(
        limits.get("max_total_jobs", 999)
    ):
        return _refused("too_many_jobs")
    filament = choice.filament or {}
    mode = filament.get("mode")
    if mode not in FILAMENT_MODES:
        return _refused("filament")
    if mode == "pinned":
        # Only exactly what Orca named, for the one plate its dialog mapped, on a printer (never the AutoQueue).
        named = list(request.pins) if request.pins is not None else None
        if (
            request.target.kind != "printer"
            or named is None
            or filament.get("ams_mapping") != named
            or len(chosen) != 1
        ):
            return _refused("filament")
    elif "ams_mapping" in filament:
        return _refused("filament")
    return None


def _body(record_id: str, capabilities: Capabilities, file_id: int, staged: Staged, request, choice, options) -> dict:
    entries = [
        {
            "entry_id": str(uuid.uuid4()),
            "plate_id": plate,
            "quantity": int(choice.copies),
            "target": request.target.wire(),
            "action": choice.action,
            "filament": dict(choice.filament),
            "options": dict(options),
        }
        for plate in choice.plates
    ]
    return {
        "protocol_version": 1,
        "operation_id": record_id,
        "server_id": capabilities.server_id,
        "storage_epoch": capabilities.storage_epoch,
        "library_file_id": file_id,
        "source_sha256": staged.sha256,
        "entries": entries,
    }


def send(
    request: Request,
    *,
    client,
    capabilities: Capabilities,
    journal: Journal,
    staging_dir: str,
    gate: UploadGate,
    cancel: threading.Event,
    choose: Callable[[Offer], Choice | None],
    host_writable: bool,
    progress: Callable[[str, int], None] | None = None,
    answer_deadline: float = 60.0,
    poll_seconds: float = 2.0,
    upload_wait: float = UPLOAD_WAIT_SECONDS,
) -> Outcome:
    """Run one operation to its outcome. Never raises for what the server, the file or the person decided."""
    if not host_writable:
        return _refused("host_version")
    if not capabilities.enabled:
        return _refused("connector_disabled")
    if not capabilities.allows("library_upload"):
        return _refused("not_permitted")
    report = progress or (lambda *_: None)
    report("staging", 0)
    try:
        staged = staging.stage(
            request.source,
            staging_dir,
            int(capabilities.limits.get("max_upload_bytes") or 0) or 1 << 31,
            filename=request.filename,
            cancelled=cancel.is_set,
        )
    except StagingError as exc:
        return Outcome("cancelled", "cancelled") if exc.code == "cancelled" else _refused(exc.code)
    try:
        return _staged(request, staged, client, capabilities, journal, gate, cancel, choose, report,
                       answer_deadline, poll_seconds, upload_wait)  # fmt: skip
    finally:
        staging.discard(staged)


def _staged(request, staged, client, capabilities, journal, gate, cancel, choose, report, deadline, poll, wait):
    try:
        inspection = inspector.inspect(staged.path)
    except InspectionError as exc:
        return _refused(exc.code)
    plates = tuple(plate.index for plate in inspection.plates)
    if not plates:
        return _refused("not_sliced")
    proposal = _proposal(request, plates)
    if isinstance(proposal, Outcome):
        return proposal
    proposed, default_action = proposal
    if cancel.is_set():
        return Outcome("cancelled", "cancelled")
    choice = choose(Offer(request, staged, inspection, plates, proposed, default_action))
    if choice is None or cancel.is_set():
        return Outcome("cancelled", "cancelled")
    options = {**request.options, **(choice.options or {})}
    refusal = _check(choice, options, request, plates, capabilities)
    if refusal is not None:
        return refusal
    chosen = tuple(choice.plates) if choice.action != "library_only" else ()
    try:
        record = journal.create(
            staged.filename, staged.sha256, origin=request.origin, plates=chosen, target=request.target.wire()
        )
    except (JournalError, OSError) as exc:
        return _refused(_journal_code(exc))  # not written down: nothing is sent
    operation_id = record.operation_id

    def strict(**changes):
        """A milestone a request must not go out without: JournalError is the caller's to answer."""

        def mutate(stored):
            for name, value in changes.items():
                setattr(stored, name, value)

        return journal.update(operation_id, mutate)

    def note(**changes):
        """A milestone after the fact: a journal busy now leaves the record behind, and the history reconciles it
        with the server -- the answer already given is not taken back."""
        try:
            strict(**changes)
        except (JournalError, OSError):
            pass  # a busy lock or a full disk: the server's answer stands (H3 review R3)

    def finished(result: str, code: str, message: str = "", **kwargs) -> Outcome:
        note(phase="finished", error=code)
        return Outcome(result, code, message, operation_id=operation_id, **kwargs)

    # -- the upload: the operation is on disk before its first request ----------------------------------------------
    try:
        strict(phase="uploading")
    except (JournalError, OSError) as exc:
        return _refused(_journal_code(exc), operation_id=operation_id)
    report("uploading", 0)
    try:
        with gate.slot(cancel, wait):
            answer = client.upload(
                staged.path,
                staged.filename,
                staged.size,
                progress=lambda sent, total: report("uploading", int(sent * 100 / total) if total else 100),
                cancelled=cancel.is_set,
            )
    except Cancelled:
        return finished("cancelled", "cancelled")
    except TimeoutError:
        return finished("refused", "upload_busy")
    except HttpError as exc:
        return finished("refused", exc.code, exc.message)
    except TransportError as exc:
        if cancel.is_set() or exc.kind == "cancelled":
            return finished("cancelled", "cancelled")
        return finished("refused", exc.kind)
    file_id = answer.get("id") if isinstance(answer, dict) else None
    if not isinstance(file_id, int) or answer.get("file_hash") != staged.sha256:
        return finished("refused", "hash_mismatch")
    if choice.action == "library_only":
        note(phase="upload_only", library_file_id=file_id)
        return Outcome("accepted", "library", operation_id=operation_id, library_file_id=file_id)
    note(phase="uploaded", library_file_id=file_id)
    if cancel.is_set():
        return finished("cancelled", "cancelled", library_file_id=file_id)

    # -- the submit: written down first, so a lost answer is asked about under the same id --------------------------
    body = _body(operation_id, capabilities, file_id, staged, request, choice, options)
    try:
        strict(phase="submitting", body=body)
    except (JournalError, OSError) as exc:
        return _refused(_journal_code(exc), operation_id=operation_id, library_file_id=file_id)
    report("submitting", 100)
    try:
        status, result = client.submit(body)
    except (HttpError, TransportError) as exc:
        if isinstance(exc, HttpError) and not uncertain(exc):
            return finished("refused", exc.code, exc.message, library_file_id=file_id)
        # The request went out and its answer proves nothing: the server may have it (H3 review R1). A cancel the
        # person already asked for is still sent, under the same id (R2).
        if cancel.is_set():
            return _cancel_published(client, capabilities, operation_id, file_id, note)
        note(phase="unknown")
        return Outcome("unknown", "submit_unknown", operation_id=operation_id, library_file_id=file_id)
    end = time.monotonic() + deadline
    while _state(result) not in TERMINAL:
        if cancel.is_set():
            return _cancel_published(client, capabilities, operation_id, file_id, note)
        if time.monotonic() >= end:
            note(phase="unknown", result=result)
            return Outcome("unknown", "still_processing", operation_id=operation_id, library_file_id=file_id)
        time.sleep(poll)
        try:
            status, result = client.get(operation_id, capabilities.server_id, capabilities.storage_epoch)
        except (HttpError, TransportError):
            continue  # asked again until the deadline; the record stays the source of truth
    return _settled(result, operation_id, file_id, note)


def _journal_code(exc: Exception) -> str:
    return "journal_busy" if isinstance(exc, JournalError) and str(exc) == "busy" else "journal"


def uncertain(exc: HttpError) -> bool:
    """A refusal after a request went out that proves nothing about the server: a gateway's or the server's own 5xx
    without the connector's refusal code (a proxy's 502/504, a crash mid-answer). The server may have the work, so it
    is ``unknown`` -- asked about, never called refused (H3 review R1)."""
    return (exc.status >= 500 and exc.code in ("http_error", "")) or exc.status == 408


def _state(result) -> str:
    return result.get("state", "processing") if isinstance(result, dict) else "processing"


def _settled(result: dict, operation_id: str, file_id: int, note) -> Outcome:
    state = _state(result)
    kwargs = {"operation_id": operation_id, "library_file_id": file_id, "answer": result}
    if state in ("complete", "partial"):
        note(phase="accepted", result=result)
        return Outcome("accepted", state if state == "partial" else "queued", **kwargs)
    note(phase="finished", result=result, error=state)
    return Outcome("cancelled" if state == "cancelled" else "refused", state, **kwargs)


def _cancel_published(client, capabilities, operation_id, file_id, note) -> Outcome:
    """The person cancelled after the submit went out: the server stops what it has not yet published."""
    try:
        _status, result = client.cancel(operation_id, capabilities.server_id, capabilities.storage_epoch)
    except (HttpError, TransportError):
        note(phase="unknown")
        return Outcome("unknown", "cancel_unknown", operation_id=operation_id, library_file_id=file_id)
    return _settled(result, operation_id, file_id, note)
