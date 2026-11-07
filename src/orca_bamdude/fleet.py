"""The farm as one connection generation sees it, and what the printer agent tells OrcaSlicer about it.

Host spec r2.3.2 §5.1-§5.3 and §8.2. One ``Fleet`` lives for a load; ``rebind`` gives it each new settings snapshot.
A new generation forgets what was read under the old one -- capabilities, the device map, a status -- instead of
carrying it over (§4.4 ⟨R23.1-M1⟩), and cancels the old generation's reads. A selected device keeps its id, and keeps
working only if the new binding yields that same id: a changed CA or proxy header is the same target (§4.4),
anything else ends it at its next read.

Reads go through the plugin's one ``Poller`` and only while something wants them (§8.2):

- ``printers`` -- capabilities and the printer list together, so the map belongs to the binding it was read under.
  Read once when OrcaSlicer starts discovery or connects a device the map does not know yet, again when the
  connected device seems gone, and every 60 s only while the tab asks for the fleet. Each read announces the devices.
- ``status`` -- capabilities and the one connected device's status, every 5 s while a device is selected, *stale or
  not*: freshness decides what is pushed, never whether the next read happens, so the device recovers without the
  tab and without a gesture (⟨M-R1⟩). A read is accepted only when the binding it derives still yields the device's
  id (a restore, another key or another address end it) and the status names the device's printer identity. A status
  that names *another* printer proves the id was given to someone else and revokes the device at once; a status that
  is merely missing only ages.

The transport is fresh while the last accepted status is younger than ``STALE_SECONDS``. While fresh the connected
device gets a full report every ``PUSH_SECONDS``; while stale nothing is pushed, and OrcaSlicer itself shows the
device offline about 30 s after the last report -- about a minute after the server last answered (RF-11). No "lost"
is ever sent. A read that is halted (401, 403, TLS, an incompatible server) stays halted until the settings change.

**Contexts** (H2 review R2). Every read carries the generation and the *era* it started in; the era moves with each
generation and whenever a read proves the namespace changed under the same settings (a restore at the same address),
so an older read can never bring the rejected namespace back. Every host message carries a stamp of what it was
computed under -- generation, era, the device and whether it must be connected or fresh, the device map -- and is
checked against the fleet as it is *at the moment it is handed over*, under a publication lock that ``rebind`` takes
too: a message computed before a rebind and handed over after it is dropped. The host's callbacks re-post through
``CallAfter`` and return at once (rc ``GUI_App::init_networking_callbacks``), so holding that lock across one is safe.

**The camera** (R1). The camera is admitted for the connected, fresh device whose key may watch, and the admission is
the fleet's: the relay asks for it at every request, every upstream open and every renewal (``camera_admits``), the
fleet's own ``open_camera`` asks again, and every loop step takes the relay's target down once it is no longer
admitted -- so a kept address, a stale device, a disconnect, another binding, a lost right or the fence opens nothing
and ends what plays. Issuing an address (``camera_url``: decide, set the relay's target, check) and every take-down
run under the publication lock, as every change that voids an admission does: a namespace change cannot come between
an intent's decision and its answer, and a take-down decided on an older state cannot take a newer admission of the
same printer with it -- the relay's target is a value, and only that order tells two equal values apart (fix2 R2).

**Shutdown** (R3). ``stop`` closes the current client first -- every read in flight ends and none starts -- then the
relay, the reads' threads and the loop, within one deadline.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from . import dialect
from .api import Capabilities, ConnectorClient
from .camera_relay import Target
from .devices import Binding, Device, DeviceMap, dev_id, device_map
from .poller import STALE_SECONDS, Poller
from .transport import Transport, TransportError

PUSH_SECONDS = 3.0
STATUS_SECONDS = 5.0
PRINTERS_SECONDS = 60.0
LOOP_SECONDS = 0.25
JOIN_SECONDS = 5.0
OK, FAILED = 0, 1  # rc ConnectStatus: Ok = 0, Failed = 1; Lost (2) is never sent
ERR_NOT_SUPPORTED = -7010
_REPLIED = {"pushall", "push_status", "get_version"}
_BLOCKED_MESSAGE = {"held": "connect.restart", "needs_restart": "connect.restart"}


class HostCallbacks:
    """What OrcaSlicer handed the agent through ``set_on_*_fn``: set on the UI thread, cleared with ``None`` at the
    host's shutdown, called from any thread.

    ``call`` holds this object's lock from the last admission check to the end of the host call, so what is handed over
    is exactly what was admitted at that moment (H2 fix review F3). Every function the plugin is handed re-posts and
    returns at once -- ``local_connect`` / ``local_message`` / ``printer_connected`` through ``CallAfter``
    (``GUI_App::init_networking_callbacks``), the SSDP one through ``queue_on_main`` (``NetworkAgent::
    apply_discovery_callback`` wraps it; the synchronous ``on_machine_alive`` runs later, from the UI queue) -- so the
    lock is held only briefly. It is re-entrant: a host call may come back into the agent on the same thread."""

    def __init__(self):
        self._lock = threading.RLock()
        self._fns: dict[str, Callable | None] = {}

    def set(self, name: str, fn) -> None:
        with self._lock:
            self._fns[name] = fn

    def call(self, name: str, *args, admit=None) -> bool:
        """Hand ``args`` to the host's ``name``; with ``admit`` (a context manager yielding a bool) only if it still
        admits them, checked under this lock."""
        with self._lock:
            fn = self._fns.get(name)
            if fn is None:
                return False
            if admit is None:
                return self._invoke(fn, args)
            with admit() as admitted:
                return self._invoke(fn, args) if admitted else False

    @staticmethod
    def _invoke(fn, args) -> bool:
        try:
            fn(*args)
        except Exception:  # noqa: BLE001 -- the host's wrapper failing never stops the fleet
            return False
        return True


@dataclass(frozen=True)
class _Read:
    """What a read started under: answers of another generation or era, or about another device, are dropped."""

    generation: int
    era: int
    base_url: str
    device: Device | None


@dataclass(frozen=True)
class _Stamp:
    """What a host message was computed under; it is handed over only while all of it still holds."""

    generation: int
    era: int
    device: str | None = None  # the dev_id it is about: still the selected device
    connected: bool = False  # ... and connected
    fresh: bool = False  # ... and fresh
    devices: DeviceMap | None = None  # the map it announces: still the current one


def default_client(snapshot) -> ConnectorClient | None:
    connection = getattr(snapshot, "connection", None)
    return ConnectorClient(Transport(connection)) if connection is not None else None


def _close(client) -> None:
    close = getattr(client, "close", None)
    if close is not None:
        try:
            close()
        except Exception:  # noqa: BLE001 -- a client that cannot close is left to its timeout
            pass


class Fleet:
    def __init__(
        self,
        callbacks: HostCallbacks,
        context,
        say: Callable[[str], str],
        *,
        poller: Poller | None = None,
        clock: Callable[[], float] = time.monotonic,
        client_factory: Callable = default_client,
        relay=None,
    ):
        self.callbacks = callbacks
        self.context = context
        self.say = say
        self.clock = clock
        self.poller = poller or Poller(clock=clock)
        self.relay = relay
        self._client_factory = client_factory
        self._lock = threading.Lock()
        # Held by every change that can void a stamp and by each hand-over from its check to its end; never while a
        # host function is called by a change (they hand over afterwards). Order: callbacks -> publication -> _lock.
        self._publication = threading.RLock()
        self._generation = 0
        self._era = 0
        self._binding: Binding | None = None  # the namespace accepted in this generation (H2 fix review F2)
        self._snapshot = None
        self._client: ConnectorClient | None = None
        self._capabilities: Capabilities | None = None
        self._map: DeviceMap | None = None
        self._fleet_wanted = False
        self._list_once = False
        self._waiting: str | None = None  # a connect waiting for the list: the dev_id it named
        self._target: Device | None = None  # the selected device of the current map
        self._connected = False  # the connect sequence was handed to the host for ``_target``
        self._reread_asked = False  # the list was re-read for this target once already (a missing status)
        self._status: dict | None = None
        self._status_at: float | None = None
        self._firmware: str | None = None
        self._camera: Target | None = None  # the camera target this fleet admitted, for ``_target``
        self._last_push = 0.0
        self._seq = 0
        self._access_code = ""
        self._loop: threading.Thread | None = None
        self._stop = threading.Event()
        self.poller.add("printers", PRINTERS_SECONDS, self._fetch_printers, self._deliver_printers)
        self.poller.add("status", STATUS_SECONDS, self._fetch_status, self._deliver_status)

    # -- settings ------------------------------------------------------------------------------------------------------

    def rebind(self, snapshot) -> None:
        """A newer settings snapshot; an older or repeated one changes nothing."""
        if snapshot is None:
            return
        refused = None
        with self._publication:
            with self._lock:
                if snapshot.generation <= self._generation:
                    return
                self._generation, self._snapshot = snapshot.generation, snapshot
                self._era += 1
                self._binding = None
                old, self._client = self._client, self._client_factory(snapshot)
                self._capabilities, self._map = None, None
                self._status, self._status_at, self._firmware = None, None, None
                self._reread_asked = False
                self._camera = None
                # A connect still waiting cannot be answered now: it hears what to do. A connected device keeps its
                # selection and goes quiet; it comes back when a connection does.
                pending = self._waiting is not None or (self._target is not None and not self._connected)
                if self._client is None and pending:
                    refused = (self._waiting or self._target.dev_id, self._blocked_message())
                    self._waiting, self._target = None, None
                self.poller.configure(generation=snapshot.generation, poll_min_seconds=0)
                stamp = self._stamp_locked()
            _close(old)  # the older generation's reads end now; their answers would be dropped anyway
            self._revoke_camera_address()
        if refused is not None:
            self._emit("local_connect", stamp, FAILED, *refused)
        self._update_wants()

    def _revoke_camera_address(self) -> None:
        """A new generation or a proven new namespace: the relay's secret goes, so no address issued before opens
        anything again, even for a printer that kept its number and identity (H2 fix review F2)."""
        if self.relay is not None:
            self.relay.rotate()
            self.relay.set_target(None)

    def _namespace_changed_locked(self, binding: Binding) -> bool:
        """Under the lock: ``binding`` is what a read accepted; a different one than this generation's proves the
        namespace changed under the same settings -- a new era, every older read void."""
        changed = self._binding is not None and binding != self._binding
        self._binding = binding
        if changed:
            self._era += 1
        return changed

    def _blocked_message(self) -> str:
        return self.say(_BLOCKED_MESSAGE.get(getattr(self._snapshot, "blocked", None), "connect.setup"))

    # -- stamps and the hand-over to the host --------------------------------------------------------------------------

    def _stamp_locked(self, device: Device | None = None, *, connected=False, fresh=False, devices=None) -> _Stamp:
        return _Stamp(
            self._generation,
            self._era,
            device.dev_id if device is not None else None,
            connected,
            fresh,
            devices,
        )

    def _holds_locked(self, stamp: _Stamp) -> bool:
        if stamp.generation != self._generation or stamp.era != self._era:
            return False
        if stamp.device is not None:
            if self._target is None or self._target.dev_id != stamp.device:
                return False
            if stamp.connected and not self._connected:
                return False
            if stamp.fresh and not self._fresh_locked():
                return False
        return stamp.devices is None or stamp.devices is self._map

    def _still(self, stamp: _Stamp) -> bool:
        with self._lock:
            return self._holds_locked(stamp) and not self.context.closed

    @contextlib.contextmanager
    def _admission(self, stamp: _Stamp):
        """The hand-over's own check, made under the callbacks' lock and the publication lock and held to the end of
        the host call: no change that voids the stamp can come between the check and the call (F3)."""
        with self._publication:
            yield self._still(stamp)

    def _emit(self, name: str, stamp: _Stamp, *args) -> bool:
        """Hand one message to the host if what it was computed under still holds. ``False`` when it was dropped."""
        if not self._still(stamp):
            return False  # early: no lock is waited for on behalf of a message already void
        return self.callbacks.call(name, *args, admit=lambda: self._admission(stamp))

    # -- what OrcaSlicer asks ------------------------------------------------------------------------------------------

    def discover(self) -> None:
        """``start_discovery(True)``: read the list once and announce it."""
        with self._lock:
            self._list_once = True
        self._update_wants()
        self.poller.wake("printers")

    def want_fleet(self, wanted: bool) -> None:
        """The tab's demand for the whole fleet (its live monitoring)."""
        with self._lock:
            self._fleet_wanted = bool(wanted)
        self._update_wants()

    def connect(self, device_id: str, access_code: str = "") -> int:
        """``connect_printer`` on the UI thread: decide what is known now and answer the rest when it is read.

        A device of the current map is connected once its first status is accepted. An id the current map does not
        hold is refused with a sentence OrcaSlicer shows as it is -- never ``"5"`` (Orca takes it for a wrong access
        code and erases it) or ``"-1"`` (it adds hints about the Bambu network plugin)."""
        refused, wake = None, None
        with self._publication:
            with self._lock:
                self._access_code = access_code or ""
                self._waiting, self._target, self._connected = None, None, False
                self._status, self._status_at, self._reread_asked = None, None, False
                self._camera = None
                if self._client is None:
                    refused = self._blocked_message()
                elif self._map is None:
                    self._waiting, self._list_once, wake = device_id, True, "printers"
                else:
                    self._target = self._map.get(device_id)
                    if self._target is None:
                        refused = self.say("connect.foreign")
                    else:
                        wake = "status"
                stamp = self._stamp_locked()
            self._take_camera_down()  # under the publication lock: never after a newer admission (fix2 R2)
        if refused is not None:
            self._emit("local_connect", stamp, FAILED, device_id, refused)
        self._update_wants()
        if wake is not None:
            self.poller.wake(wake)
        return OK

    def disconnect(self) -> int:
        with self._publication:
            with self._lock:
                self._waiting, self._target, self._connected = None, None, False
                self._status, self._status_at = None, None
                self._camera = None
            self._take_camera_down()  # under the publication lock: never after a newer admission (fix2 R2)
        self._update_wants()
        return OK

    def command(self, device_id: str, json_str: str) -> int:
        """``send_message[_to_printer]``: a report from the cache (only while fresh), the access code echoed back,
        anything else not supported -- the Device tab controls nothing on a farm printer."""
        try:
            body = json.loads(json_str)
        except (TypeError, ValueError):
            return ERR_NOT_SUPPORTED
        section = next(iter(body), "") if isinstance(body, dict) and body else ""
        inner = body.get(section) if isinstance(body, dict) else None
        command = inner.get("command", "") if isinstance(inner, dict) else ""
        with self._lock:
            mine = self._target is not None and self._target.dev_id == device_id and self._connected
            fresh = mine and self._fresh_locked()
            code, firmware = self._access_code, self._firmware
            stamp = self._stamp_locked(self._target, connected=True) if mine else None
            fresh_stamp = self._stamp_locked(self._target, connected=True, fresh=True) if fresh else None
        if section == "system" and command == "get_access_code":
            if stamp is not None:
                reply = json.dumps({"system": {"command": command, "access_code": code}})
                self._emit("local_message", stamp, device_id, reply)
            return OK
        if section == "pushing" or command in _REPLIED:
            if fresh_stamp is not None:
                if command == "get_version":
                    self._emit("local_message", fresh_stamp, device_id, dialect.version_reply(firmware))
                self._push(force=True)
            return OK
        return ERR_NOT_SUPPORTED

    # -- the camera (R1) -------------------------------------------------------------------------------------------------

    def _camera_device_locked(self) -> Device | None:
        """The device whose camera may be watched now: connected, fresh, and the key may watch."""
        if self._target is None or not self._connected or not self._fresh_locked():
            return None
        if self._capabilities is None or not self._capabilities.allows("camera_stream"):
            return None
        return self._target

    def camera_admits(self, target: Target) -> bool:
        """The relay's question at every request, open and renewal: is this still the admitted camera?"""
        with self._lock:
            device = self._camera_device_locked()
            return (
                device is not None
                and not self.context.closed
                and self._camera == target
                and target == Target(device.printer_id, device.identity)
            )

    def camera_url(self) -> str:
        """The relay's address for the admitted camera; otherwise empty, and the relay's target is taken down.

        Decision, relay and check are one step under the publication lock: no change that voids the admission can
        come between them, so an intent formed under one namespace never answers with another's (fix2 R2)."""
        with self._publication:
            with self._lock:
                device = self._camera_device_locked()
                target = Target(device.printer_id, device.identity) if device is not None else None
                self._camera = target
            if self.relay is None or target is None or self.context.closed:
                self._take_camera_down()
                return ""
            self.relay.set_target(target)
            if not self.camera_admits(target):  # the fence, or a loss the relay's own question sees
                self._take_camera_down(target)
                return ""
            return self.relay.url_for(target) or ""

    def _take_camera_down(self, only: Target | None = None) -> None:
        if self.relay is None:
            return
        current = self.relay.target
        if current is not None and (only is None or current == only):
            self.relay.set_target(None)

    def _revoke_camera(self) -> None:
        """Every loop step: a camera no longer admitted is ended, the playing viewer with it."""
        if self.relay is None:
            return
        with self._publication:  # decided and done in one step: never after a newer admission of the same value
            current = self.relay.target
            if current is not None and not self.camera_admits(current):
                with self._lock:
                    if self._camera == current:
                        self._camera = None
                self._take_camera_down(current)

    def open_camera(self, target: Target):
        """The relay's upstream, opened by the current generation's client -- only for the admitted camera."""
        with self._lock:
            client = self._client
        if client is None or not self.camera_admits(target):
            raise TransportError("not_admitted")
        return client.open_camera(target.printer_id, target.identity)

    # -- jobs ------------------------------------------------------------------------------------------------------------

    def admit_job(self, device_id: str):
        """Before a job reads anything: the device it names, or why not -- ``foreign`` or ``stale`` -- fail-closed
        whatever OrcaSlicer itself allowed (§5.2)."""
        with self._lock:
            if self._target is None or self._target.dev_id != device_id or not self._connected:
                return "foreign"
            if not self._fresh_locked():
                return "stale"
            return self._target

    def job_context(self, device_id: str):
        """``admit_job`` and what the job then sends with, read together: the device, this generation's client, its
        capabilities and the fresh status the slots Orca chose are confirmed against -- or ``foreign`` / ``stale``. A
        rebind afterwards closes that client, so a job never goes on under another connection."""
        with self._lock:
            if self._target is None or self._target.dev_id != device_id or not self._connected:
                return "foreign"
            if not self._fresh_locked():
                return "stale"
            if self._client is None or self._capabilities is None:
                return "foreign"
            return self._target, self._client, self._capabilities, dict(self._status or {})

    # -- state ---------------------------------------------------------------------------------------------------------

    @property
    def fresh(self) -> bool:
        with self._lock:
            return self._fresh_locked()

    def _fresh_locked(self) -> bool:
        return self._status_at is not None and self.clock() - self._status_at < STALE_SECONDS

    @property
    def devices(self) -> DeviceMap | None:
        with self._lock:
            return self._map

    @property
    def capabilities(self) -> Capabilities | None:
        with self._lock:
            return self._capabilities

    @property
    def target(self) -> Device | None:
        with self._lock:
            return self._target

    def _update_wants(self) -> None:
        with self._lock:
            online = self._client is not None
            printers = online and (self._fleet_wanted or self._list_once)
            status = online and self._target is not None
        self.poller.want("printers", printers)
        self.poller.want("status", status)

    def _learn(self, capabilities: Capabilities) -> None:
        """Under the lock: the newest capabilities, and the server's polling floor with them."""
        self._capabilities = capabilities
        floor = capabilities.limits.get("poll_min_seconds") if isinstance(capabilities.limits, dict) else None
        if isinstance(floor, (int, float)) and not isinstance(floor, bool) and floor > 0:
            self.poller.limit(poll_min_seconds=floor)

    # -- reads ---------------------------------------------------------------------------------------------------------

    def _started(self, device: Device | None) -> tuple[_Read, ConnectorClient]:
        with self._lock:
            client = self._client
            if client is None:
                raise RuntimeError("no connection")
            return _Read(self._generation, self._era, self._snapshot.connection.base_url, device), client

    def _stopping(self) -> bool:
        return self._stop.is_set() or self.context.closed

    def _fetch_printers(self):
        read, client = self._started(None)
        capabilities = client.capabilities()
        if self._stopping():
            raise RuntimeError("stopped")  # no next request of the chain after the fence
        return read, capabilities, client.printers()

    def _current(self, read: _Read) -> bool:
        return read.generation == self._generation and read.era == self._era

    def _deliver_printers(self, value) -> None:
        read, capabilities, printers = value
        refused = None
        with self._publication:
            with self._lock:
                if not self._current(read):
                    return  # an answer about a connection, or a namespace, that is no longer this one
                binding = Binding.of(read.base_url, capabilities)
                renamed = self._namespace_changed_locked(binding)  # a restore seen by the list first (F2)
                self._learn(capabilities)
                self._map = device_map(binding, printers)
                self._list_once = False
                announce = self._map.announced()
                if self._waiting is not None:
                    waiting, self._waiting = self._waiting, None
                    self._target = self._map.get(waiting)
                    if self._target is None:
                        refused = (waiting, self.say("connect.foreign"))
                elif self._target is not None:
                    found = self._map.get(self._target.dev_id)
                    if found is None:
                        # The connected device is gone: deleted, out of the key's scope, or of an earlier binding.
                        if not self._connected:
                            refused = (self._target.dev_id, self.say("connect.foreign"))
                        self._target, self._connected, self._status, self._status_at = None, False, None, None
                        self._camera = None
                    elif found != self._target:
                        self._target = found  # the same printer under the number the server gives it now
                wake = self._target is not None and not self._connected
                stamp = self._stamp_locked()
                announce_stamp = self._stamp_locked(devices=self._map)
            if refused is not None:
                self._take_camera_down()  # decided and done under the publication lock (fix3 R1)
            if renamed:
                self._revoke_camera_address()
        for device in announce:
            self._emit("ssdp", announce_stamp, dialect.ssdp(device, binding))
        if refused is not None:
            self._emit("local_connect", stamp, FAILED, *refused)
        self._update_wants()
        if wake:
            self.poller.wake("status")

    def _fetch_status(self):
        read, client = self._started(self.target)
        if read.device is None:
            raise RuntimeError("no target")
        capabilities = client.capabilities()
        if self._stopping():
            raise RuntimeError("stopped")
        return read, capabilities, client.status_batch([read.device.printer_id])

    def _deliver_status(self, value) -> None:
        read, capabilities, statuses = value
        device = read.device
        reread, sequence = False, None
        renamed = False
        with self._publication:
            with self._lock:
                if not self._current(read) or device is not self._target:
                    return
                status = statuses.get(device.printer_id) if isinstance(statuses, dict) else None
                named = status.get("connector_identity") if isinstance(status, dict) else None
                binding = Binding.of(read.base_url, capabilities)
                if dev_id(binding, device.identity) != device.dev_id:
                    # A restore, another key or another address: the device belongs to the previous binding, and so does
                    # every read still in flight -- a new era (R2) -- and every camera address issued under it (F2).
                    self._target, self._connected, self._status, self._status_at, self._map = (
                        None,
                        False,
                        None,
                        None,
                        None,
                    )
                    self._camera = None
                    self._binding = binding
                    self._era += 1
                    self._list_once, reread, renamed = True, True, True
                elif isinstance(status, dict) and named != device.identity:
                    # The number now names another printer: proven, so the device is revoked at once -- no report, job or
                    # camera on the strength of an older status (R4) -- and the list says what became of it.
                    self._status, self._status_at, self._camera = None, None, None
                    if not self._reread_asked:
                        self._reread_asked, self._list_once, reread = True, True, True
                elif not isinstance(status, dict):
                    # Missing (deleted, out of the key's scope): no proof either way; it ages, and the list is read once.
                    if not self._reread_asked:
                        self._reread_asked, self._list_once, reread = True, True, True
                else:
                    self._learn(capabilities)
                    self._binding = binding
                    self._status, self._status_at, self._reread_asked = status, self.clock(), False
                    firmware = status.get("firmware_version")
                    self._firmware = firmware if isinstance(firmware, str) else None
                    if not self._connected:
                        self._connected, self._last_push = True, self.clock()
                        self._seq += 1
                        report = dialect.push_status(status, seq=self._seq)
                        stamp = self._stamp_locked(device, connected=True)
                        sequence = (stamp, device.dev_id, dialect.version_reply(self._firmware), report)
                take_down = reread and (self._target is None or not self._fresh_locked())
            if take_down:
                self._take_camera_down()  # decided and done under the publication lock (fix3 R1)
            if renamed:
                self._revoke_camera_address()
        if reread:
            self._update_wants()
            self.poller.wake("printers")
            return
        if sequence is not None:
            stamp, connected, version, report = sequence
            sent = (
                self._emit("local_message", stamp, connected, version)
                and self._emit("local_message", stamp, connected, report)
                and self._emit("local_connect", stamp, OK, connected, "0")
                and self._emit("printer_connected", stamp, connected)
            )
            if not sent:
                # Not all of it reached the host: the next accepted status of this device sends it again.
                with self._publication, self._lock:
                    if self._target is not None and self._target.dev_id == connected:
                        self._connected = False

    # -- the loop ------------------------------------------------------------------------------------------------------

    def _push(self, force: bool = False) -> None:
        with self._lock:
            if self._target is None or not self._connected or not self._fresh_locked():
                return
            now = self.clock()
            if not force and now - self._last_push < PUSH_SECONDS:
                return
            self._last_push = now
            self._seq += 1
            target, report = self._target.dev_id, dialect.push_status(self._status, seq=self._seq)
            stamp = self._stamp_locked(self._target, connected=True, fresh=True)
        self._emit("local_message", stamp, target, report)

    def _answer_halted(self) -> None:
        """A connect still waiting while its read is halted (401, 403, TLS, ...) hears the refusal once."""
        with self._publication, self._lock:
            if self._waiting is not None and self.poller.halted("printers") is not None:
                waiting, self._waiting = self._waiting, None
            elif self._target is not None and not self._connected and self.poller.halted("status") is not None:
                waiting, self._target = self._target.dev_id, None
            else:
                return
            stamp = self._stamp_locked()
        self._emit("local_connect", stamp, FAILED, waiting, self.say("connect.refused"))
        self._update_wants()

    def tick(self) -> None:
        """One step of the fleet's loop: start due reads, answer a halted connect, push, end a camera gone."""
        if self.context.closed:
            self._revoke_camera()
            return
        self.poller.tick()
        self._answer_halted()
        self._push()
        self._revoke_camera()

    def start(self) -> None:
        """The fleet's thread and the relay, from the first UI entry on (§4.3). Idempotent."""
        with self._lock:
            if self._loop is not None or self.context.closed:
                return
            self._loop = threading.Thread(target=self._run, name="bamdude-fleet", daemon=True)
            self._loop.start()
        if self.relay is not None:
            self.relay.start()

    def _run(self) -> None:
        while not self._stop.is_set() and not self.context.closed:
            try:
                self.tick()
            except Exception:  # noqa: BLE001 -- one bad step never ends the loop
                pass
            self._stop.wait(LOOP_SECONDS)

    def stop(self, timeout: float = JOIN_SECONDS) -> bool:
        """Cancel the reads in flight (the client is closed first: nothing new starts), then stop the relay -- its
        port is free -- the reads' threads and the loop, all within one ``timeout``; ``True`` only if every thread
        ended in time."""
        deadline = time.monotonic() + timeout
        self._stop.set()
        with self._lock:
            loop, client = self._loop, self._client
        _close(client)
        ended = True
        if self.relay is not None:
            ended = self.relay.stop(max(0.0, deadline - time.monotonic())) and ended
        ended = self.poller.stop(max(0.0, deadline - time.monotonic())) and ended
        if loop is not None:
            loop.join(max(0.0, deadline - time.monotonic()))
            ended = ended and not loop.is_alive()
        return ended


__all__ = ["Fleet", "HostCallbacks", "PUSH_SECONDS", "STALE_SECONDS", "default_client"]
