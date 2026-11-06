"""The farm as one connection generation sees it, and what the printer agent tells OrcaSlicer about it.

Host spec r2.3.2 §5.1-§5.3 and §8.2. One ``Fleet`` lives for a load; ``rebind`` gives it each new settings snapshot.
A new generation forgets what was read under the old one -- capabilities, the device map, a status -- instead of
carrying it over (§4.4 ⟨R23.1-M1⟩). A selected device keeps its id, and keeps working only if the new binding yields
that same id: a changed CA or proxy header is the same target (§4.4), anything else ends it at its next read.

Reads go through the plugin's one ``Poller`` and only while something wants them (§8.2):

- ``printers`` -- capabilities and the printer list together, so the map belongs to the binding it was read under.
  Read once when OrcaSlicer starts discovery or connects a device the map does not know yet, again when the
  connected device seems gone, and every 60 s only while the tab asks for the fleet. Each read announces the devices.
- ``status`` -- capabilities and the one connected device's status, every 5 s while a device is selected, *stale or
  not*: freshness decides what is pushed, never whether the next read happens, so the device recovers without the
  tab and without a gesture (⟨M-R1⟩). A read is accepted only when the binding it derives still yields the device's
  id (a restore, another key or another address end it) and the status names the device's printer identity (an id
  the database gave to a new printer is not that printer).

The transport is fresh while the last accepted status is younger than ``STALE_SECONDS``. While fresh the connected
device gets a full report every ``PUSH_SECONDS``; while stale nothing is pushed, and OrcaSlicer itself shows the
device offline about 30 s after the last report -- about a minute after the server last answered (RF-11). No "lost"
is ever sent. A read that is halted (401, 403, TLS, an incompatible server) stays halted until the settings change.

Host callbacks are called from the fleet's threads -- the host re-posts each through ``CallAfter`` (rc
``GUI_App::init_networking_callbacks``) -- and never after the load's fence.
"""

from __future__ import annotations

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
from .transport import Transport

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
    host's shutdown, called from any thread."""

    def __init__(self):
        self._lock = threading.Lock()
        self._fns: dict[str, Callable | None] = {}

    def set(self, name: str, fn) -> None:
        with self._lock:
            self._fns[name] = fn

    def call(self, name: str, *args) -> bool:
        with self._lock:
            fn = self._fns.get(name)
        if fn is None:
            return False
        try:
            fn(*args)
        except Exception:  # noqa: BLE001 -- the host's wrapper failing never stops the fleet
            return False
        return True


@dataclass(frozen=True)
class _Read:
    """What a read started under: answers of an older generation, or about another device, are dropped."""

    generation: int
    base_url: str
    device: Device | None


def default_client(snapshot) -> ConnectorClient | None:
    connection = getattr(snapshot, "connection", None)
    return ConnectorClient(Transport(connection)) if connection is not None else None


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
        self._generation = 0
        self._snapshot = None
        self._client: ConnectorClient | None = None
        self._capabilities: Capabilities | None = None
        self._map: DeviceMap | None = None
        self._fleet_wanted = False
        self._list_once = False
        self._waiting: str | None = None  # a connect waiting for the list: the dev_id it named
        self._target: Device | None = None  # the selected device of the current map
        self._connected = False  # the connect sequence was sent for ``_target``
        self._reread_asked = False  # the list was re-read for this target once already (a missing status)
        self._status: dict | None = None
        self._status_at: float | None = None
        self._firmware: str | None = None
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
        with self._lock:
            if snapshot.generation <= self._generation:
                return
            self._generation, self._snapshot = snapshot.generation, snapshot
            self._client = self._client_factory(snapshot)
            self._capabilities, self._map = None, None
            self._status, self._status_at, self._firmware = None, None, None
            self._reread_asked = False
            # A connect still waiting cannot be answered now: it hears what to do. A connected device keeps its
            # selection and goes quiet; it comes back when a connection does.
            pending = self._waiting is not None or (self._target is not None and not self._connected)
            if self._client is None and pending:
                refused = (self._waiting or self._target.dev_id, self._blocked_message())
                self._waiting, self._target = None, None
            self.poller.configure(generation=snapshot.generation, poll_min_seconds=0)
        if self.relay is not None:
            self.relay.rotate()
        if refused is not None:
            self._emit("local_connect", FAILED, *refused)
        self._update_wants()

    def _blocked_message(self) -> str:
        return self.say(_BLOCKED_MESSAGE.get(getattr(self._snapshot, "blocked", None), "connect.setup"))

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
        with self._lock:
            self._access_code = access_code or ""
            self._waiting, self._target, self._connected = None, None, False
            self._status, self._status_at, self._reread_asked = None, None, False
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
        if self.relay is not None:
            self.relay.set_target(None)
        if refused is not None:
            self._emit("local_connect", FAILED, device_id, refused)
        self._update_wants()
        if wake is not None:
            self.poller.wake(wake)
        return OK

    def disconnect(self) -> int:
        with self._lock:
            self._waiting, self._target, self._connected = None, None, False
            self._status, self._status_at = None, None
        if self.relay is not None:
            self.relay.set_target(None)
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
        if section == "system" and command == "get_access_code":
            if mine:
                reply = json.dumps({"system": {"command": command, "access_code": code}})
                self._emit("local_message", device_id, reply)
            return OK
        if section == "pushing" or command in _REPLIED:
            if fresh:
                if command == "get_version":
                    self._emit("local_message", device_id, dialect.version_reply(firmware))
                self._push(force=True)
            return OK
        return ERR_NOT_SUPPORTED

    def camera_url(self) -> str:
        """The relay's address for the connected, fresh device whose key may watch; otherwise empty."""
        with self._lock:
            device = self._target if self._connected and self._fresh_locked() else None
            allowed = self._capabilities is not None and self._capabilities.allows("camera_stream")
        if self.relay is None or device is None or not allowed:
            return ""
        target = Target(device.printer_id, device.identity)
        self.relay.set_target(target)
        return self.relay.url_for(target) or ""

    def admit_job(self, device_id: str):
        """Before a job reads anything: the device it names, or why not -- ``foreign`` or ``stale`` -- fail-closed
        whatever OrcaSlicer itself allowed (§5.2)."""
        with self._lock:
            if self._target is None or self._target.dev_id != device_id or not self._connected:
                return "foreign"
            if not self._fresh_locked():
                return "stale"
            return self._target

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
            return _Read(self._generation, self._snapshot.connection.base_url, device), client

    def _fetch_printers(self):
        read, client = self._started(None)
        return read, client.capabilities(), client.printers()

    def _deliver_printers(self, value) -> None:
        read, capabilities, printers = value
        refused = None
        with self._lock:
            if read.generation != self._generation:
                return  # an answer about a connection that is no longer this one
            binding = Binding.of(read.base_url, capabilities)
            self._learn(capabilities)
            self._map = device_map(binding, printers)
            self._list_once = False
            announce = self._map.announced()
            if self._waiting is not None:
                waiting, self._waiting = self._waiting, None
                self._target = self._map.get(waiting)
                if self._target is None:
                    refused = (waiting, self.say("connect.foreign"))
            elif self._target is not None and self._map.get(self._target.dev_id) is None:
                # The connected device is gone: deleted, out of the key's scope, or of an earlier binding.
                if not self._connected:
                    refused = (self._target.dev_id, self.say("connect.foreign"))
                self._target, self._connected, self._status, self._status_at = None, False, None, None
            wake = self._target is not None and not self._connected
        for device in announce:
            self._emit("ssdp", dialect.ssdp(device, binding))
        if refused is not None:
            if self.relay is not None:
                self.relay.set_target(None)
            self._emit("local_connect", FAILED, *refused)
        self._update_wants()
        if wake:
            self.poller.wake("status")

    def _fetch_status(self):
        read, client = self._started(self.target)
        if read.device is None:
            raise RuntimeError("no target")
        return read, client.capabilities(), client.status_batch([read.device.printer_id])

    def _deliver_status(self, value) -> None:
        read, capabilities, statuses = value
        device = read.device
        reread, sequence = False, None
        with self._lock:
            if read.generation != self._generation or device is not self._target:
                return
            status = statuses.get(device.printer_id) if isinstance(statuses, dict) else None
            if dev_id(Binding.of(read.base_url, capabilities), device.identity) != device.dev_id:
                # A restore, another key or another address: the device belongs to the previous binding.
                self._target, self._connected, self._status, self._status_at, self._map = None, False, None, None, None
                self._list_once, reread = True, True
            elif not isinstance(status, dict) or status.get("connector_identity") != device.identity:
                # Deleted, out of the key's scope, or its id given to another printer: once, see what the list says.
                if not self._reread_asked:
                    self._reread_asked, self._list_once, reread = True, True, True
            else:
                self._learn(capabilities)
                self._status, self._status_at, self._reread_asked = status, self.clock(), False
                firmware = status.get("firmware_version")
                self._firmware = firmware if isinstance(firmware, str) else None
                if not self._connected:
                    self._connected, self._last_push = True, self.clock()
                    self._seq += 1
                    report = dialect.push_status(status, seq=self._seq)
                    sequence = (device.dev_id, dialect.version_reply(self._firmware), report)
        if reread:
            if self.relay is not None and self.target is None:
                self.relay.set_target(None)
            self._update_wants()
            self.poller.wake("printers")
            return
        if sequence is not None:
            connected, version, report = sequence
            self._emit("local_message", connected, version)
            self._emit("local_message", connected, report)
            self._emit("local_connect", OK, connected, "0")
            self._emit("printer_connected", connected)

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
        self._emit("local_message", target, report)

    def _answer_halted(self) -> None:
        """A connect still waiting while its read is halted (401, 403, TLS, ...) hears the refusal once."""
        with self._lock:
            if self._waiting is not None and self.poller.halted("printers") is not None:
                waiting, self._waiting = self._waiting, None
            elif self._target is not None and not self._connected and self.poller.halted("status") is not None:
                waiting, self._target = self._target.dev_id, None
            else:
                return
        self._emit("local_connect", FAILED, waiting, self.say("connect.refused"))
        self._update_wants()

    def tick(self) -> None:
        """One step of the fleet's loop: start due reads, answer a halted connect, push."""
        if self.context.closed:
            return
        self.poller.tick()
        self._answer_halted()
        self._push()

    def _emit(self, name: str, *args) -> None:
        if not self.context.closed:
            self.callbacks.call(name, *args)

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
        """Stop the relay (its port is free first), the reads and the loop, all within one ``timeout``; ``True`` only
        if every thread ended in time. A read still out is bounded by the transport's timeout and its answer is
        dropped."""
        deadline = time.monotonic() + timeout
        self._stop.set()
        with self._lock:
            loop = self._loop
        ended = True
        if self.relay is not None:
            ended = self.relay.stop(max(0.0, deadline - time.monotonic())) and ended
        ended = self.poller.stop(max(0.0, deadline - time.monotonic())) and ended
        if loop is not None:
            loop.join(max(0.0, deadline - time.monotonic()))
            ended = ended and not loop.is_alive()
        return ended

    def open_camera(self, target: Target):
        """The relay's upstream: the current generation's client opens the farm's stream for the chosen printer."""
        with self._lock:
            client = self._client
        if client is None:
            raise RuntimeError("no connection")
        return client.open_camera(target.printer_id, target.identity)


__all__ = ["Fleet", "HostCallbacks", "PUSH_SECONDS", "STALE_SECONDS", "default_client"]
