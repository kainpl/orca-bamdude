"""The plugin's runtime outside OrcaSlicer's API: the load's fence, the notifier and the Runtime.

Host spec r2.3.2 §4.1, §4.3, §4.4 and §4.5. Nothing here imports ``orca``; the adapter hands in the host's functions
and the settings it read on the UI thread.
"""

from __future__ import annotations

import collections
import threading
from dataclasses import dataclass, field

from . import i18n, permits
from .camera_relay import CameraRelay
from .config import Connection
from .fleet import Fleet, HostCallbacks
from .settings import Effective, Settings, effective, masked_view, origin, parse_base, slot_targets

NOTIFY_CAPACITY = 32
SHUTDOWN_STAGES = ("cancel", "stop", "cleanup")
STEP_DEADLINE = 2.0


class LoadContext:
    """One import of the plugin. Its fence closes once, at shutdown, and is never reopened.

    Every callback the plugin hands OrcaSlicer -- a notification's ``on_click``, a window's ``on_submit`` or
    ``on_close`` -- is wrapped by ``guard`` and so checks the fence of the load that made it (⟨R23.1-A1⟩). OrcaSlicer
    clears a plugin's callbacks before Python ``on_unload`` runs and keeps accepting new ones after, so a callback
    registered in between stays active in the host: only this check makes it harmless. A re-import makes a new
    context and never touches an old one.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._closed = False

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def close(self) -> None:
        with self._lock:
            self._closed = True

    def guard(self, fn, closed_result=None):
        """``fn`` while this load is open; after the fence, nothing but ``closed_result``."""

        def guarded(*args, **kwargs):
            if self.closed:
                return closed_result
            return fn(*args, **kwargs)

        return guarded


class Notifier:
    """The only caller of the host's ``push_notification`` (⟨R23-B1⟩).

    ``push_notification`` waits for OrcaSlicer's UI thread, and after a Cancel that thread waits for our job. So a
    notification is queued by anyone and pushed by this one daemon thread, which owns nothing else: no socket, no
    staged file, no journal, no right to start work.

    - ``notify`` never waits: a full queue refuses the notification (the event stays in the history).
    - ``close`` drops what is queued and returns at once. It never waits for the call already handed to the host:
      ``on_unload`` runs on the UI thread that call waits for. That one call may still show its notification after
      shutdown; its ``on_click`` is guarded by the load's fence and does nothing.
    - When the host reports it is closing (RuntimeError from a call it never took), the thread ends.
    """

    def __init__(self, push, context: LoadContext, capacity: int = NOTIFY_CAPACITY, name: str = "bamdude-notifier"):
        self._push = push
        self._context = context
        self._capacity = capacity
        self._name = name
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._queue: collections.deque = collections.deque()
        self._closed = False
        self.dropped = 0
        self.thread: threading.Thread | None = None

    def notify(self, level, text: str, on_click=None, hyper_text: str = "") -> bool:
        with self._lock:
            if self._closed or len(self._queue) >= self._capacity:
                return False
            self._queue.append((level, text, hyper_text, on_click))
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, name=self._name, daemon=True)
                self.thread.start()
            self._wake.notify()
            return True

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self.dropped += len(self._queue)
            self._queue.clear()
            self._wake.notify()

    def _run(self) -> None:
        while True:
            with self._lock:
                while not self._queue and not self._closed:
                    self._wake.wait()
                if self._closed:
                    return
                level, text, hyper_text, on_click = self._queue.popleft()
            # The call is admitted under the lock and made outside it: holding the lock through a blocking host call
            # would bring back the wait it exists to avoid.
            callback = self._context.guard(on_click, closed_result=True) if on_click is not None else None
            try:
                self._push(level, text, hyper_text, callback)
            except RuntimeError:
                with self._lock:
                    self._closed = True
                    self.dropped += len(self._queue)
                    self._queue.clear()
                return
            except Exception:  # noqa: BLE001 -- a notification that fails is still in the history
                continue


@dataclass(frozen=True)
class Snapshot:
    """The settings as the UI thread read them, frozen, with the generation they belong to (§4.4).

    ``connection`` is ``None`` whenever the plugin may not talk to that server now; ``blocked`` says why:
    ``problem`` (a stored or override value does not validate, or the override could not be read), ``missing`` (no
    address or no key), ``state_unreadable`` (the state file exists but cannot be read, so what is held is unknown),
    ``held`` (an unfinished load held the target, §4.1 ⟨R23-B2⟩) or ``needs_restart`` (this load did not ask for it).
    A held target stays ``held`` until the state file says otherwise: a change this load saw, or a retry in the tab,
    gives it a client only once the writer has written the release (H1 fix2 review F1-R).
    """

    generation: int
    revision: int
    base: Settings = field(repr=False)
    effective: Effective = field(repr=False)
    connection: Connection | None = field(repr=False)
    blocked: str | None
    slots: dict

    @property
    def source(self) -> str:
        return self.effective.source

    @property
    def preset_name(self) -> str | None:
        return self.effective.preset_name


class StateWriter:
    """The one thread that writes ``state.json`` for this load, in order (H1 review R4).

    The state file is shared by OrcaSlicer's overlapping processes, so a write waits for the file lock and syncs to
    disk. Neither may happen on the UI thread: ``submit`` queues the change and returns at once, and ``done`` is told
    whether it was recorded -- an intent is never shown as saved before it is, nor when the change refused itself
    (``permits.StaleIntent``). The Runtime closes it together with its own admission; what was accepted before is
    written by ``drain``. A write that outlives the drain's deadline is judged under the file lock by the epoch its
    intent carries, so it cannot undo a hold a later load made (H1 fix review F1).
    """

    def __init__(self, storage: str | None, name: str = "bamdude-state"):
        self._storage = storage
        self._name = name
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._queue: collections.deque = collections.deque()
        self._closed = False
        self._busy = False
        self.thread: threading.Thread | None = None

    def submit(self, change, done=None) -> bool:
        with self._lock:
            if self._closed:
                return False
            self._queue.append((change, done))
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, name=self._name, daemon=True)
                self.thread.start()
            self._wake.notify_all()
            return True

    def close(self) -> None:
        """Accept nothing more; what was accepted is still written."""
        with self._lock:
            self._closed = True
            self._wake.notify_all()

    def adopt(self, storage: str) -> None:
        with self._lock:
            if self._storage is None:
                self._storage = storage

    def flush(self, timeout: float) -> bool:
        """Wait until everything accepted so far is written. Never call it on the UI thread."""
        with self._lock:
            return self._wake.wait_for(lambda: not self._queue and not self._busy, timeout)

    def drain(self, timeout: float) -> bool:
        """Accept nothing more and write what was accepted, within ``timeout``."""
        with self._lock:
            self._closed = True
            self._wake.notify_all()
            return self._wake.wait_for(lambda: not self._queue and not self._busy, timeout)

    def _run(self) -> None:
        while True:
            with self._lock:
                while not self._queue and not self._closed:
                    self._wake.wait()
                if not self._queue:
                    return
                change, done = self._queue.popleft()
                self._busy = True
            recorded = False
            try:
                if self._storage is not None:
                    permits.update_state(self._storage, change)
                    recorded = True
            except Exception:  # noqa: BLE001 -- a busy lock or a full disk: reported to ``done``, never raised
                recorded = False
            if done is not None:
                try:
                    done(recorded)
                except Exception:  # noqa: BLE001
                    pass
            with self._lock:
                self._busy = False
                self._wake.notify_all()


class Runtime:
    """One per load, shared by the agent and the page capability (§4.5 ⟨A6⟩).

    It stops when its last capability detaches. Shutdown runs once, in a fixed order:
    1. close admission;
    2. raise the load's fence (callbacks and the notifier go quiet);
    3. wake every waiter;
    4. run the ``cancel`` steps, then the ``stop`` steps and the state writer's drain, each with a deadline;
    5. run the ``cleanup`` steps only when every cancel and stop step finished and no admitted operation is still
       running (H1 review R2). A step past its deadline keeps running and keeps its resources; it is reported as
       unfinished and nothing it owns is cleaned up.

    The UI thread never waits without a deadline, never for the notifier and never for the state file's lock.
    """

    def __init__(self, push, storage: str | None, load_id: str, requested: set[str], decision: dict):
        self.context = LoadContext()
        self.notifier = Notifier(push, self.context)
        self.storage = storage
        self.load_id = load_id
        self.requested = frozenset(requested)
        self.decision = dict(decision)
        self.snapshot: Snapshot | None = None
        self.state_writer = StateWriter(storage)
        self._lock = threading.Lock()
        self._attached: set[str] = set()
        self._stopping = False
        self._waiters: set = set()
        self._admitted: dict[int, str] = {}
        self._next_token = 0
        self._steps: list[tuple[int, str, object, float]] = []
        self._generation = 0
        self._generation_key = None
        self._revision = 0
        self._published = 0
        self._seen: dict[str, str] = {}  # this load's baseline of observed settings (H1 review R5)
        self.language = "en"
        self.callbacks = HostCallbacks()
        self.relay = CameraRelay(
            lambda target: self.fleet.open_camera(target),
            admits=lambda target: self.fleet.camera_admits(target),  # asked at every request, open and renewal
        )
        self.fleet = Fleet(self.callbacks, self.context, self.say, relay=self.relay)
        self._fleet_started = False

    # -- capabilities and shutdown ---------------------------------------------------------------------------------

    @property
    def stopping(self) -> bool:
        with self._lock:
            return self._stopping

    def attach(self, name: str) -> None:
        with self._lock:
            self._attached.add(name)

    def detach(self, name: str) -> dict | None:
        """A capability unloaded; the last one stops the runtime and returns the shutdown report."""
        with self._lock:
            self._attached.discard(name)
            last = not self._attached
        return self.shutdown("unload") if last else None

    def admit(self, kind: str) -> int | None:
        """A new operation may start only before the fence; ``None`` after it."""
        with self._lock:
            if self._stopping:
                return None
            self._next_token += 1
            self._admitted[self._next_token] = kind
            return self._next_token

    def release(self, token: int | None) -> None:
        with self._lock:
            self._admitted.pop(token, None)

    def add_waiter(self, waiter) -> bool:
        """Registered before its window exists, so the fence can wake it; refused once the fence is up."""
        with self._lock:
            if self._stopping:
                return False
            self._waiters.add(waiter)
            return True

    def remove_waiter(self, waiter) -> None:
        with self._lock:
            self._waiters.discard(waiter)

    def on_shutdown(self, stage: str, fn, deadline: float = STEP_DEADLINE) -> None:
        if stage not in SHUTDOWN_STAGES:
            raise ValueError(stage)
        with self._lock:
            self._steps.append((SHUTDOWN_STAGES.index(stage), stage, fn, deadline))

    def notify(self, level, text: str, on_click=None, hyper_text: str = "") -> bool:
        return self.notifier.notify(level, text, on_click=on_click, hyper_text=hyper_text)

    # -- language and the fleet --------------------------------------------------------------------------------------

    def set_language(self, code) -> None:
        """OrcaSlicer's UI language, read on the UI thread (``app_info``) and kept for every later sentence."""
        self.language = i18n.language(code)

    def say(self, key: str) -> str:
        return i18n.say(self.language, key)

    def start_fleet(self) -> bool:
        """The fleet's thread and the camera relay, from the first UI entry on (§4.3); their stop is a shutdown step
        of their own owner. ``False`` once the fence is up."""
        with self._lock:
            if self._stopping:
                return False
            first = not self._fleet_started
            self._fleet_started = True
        if first:
            self.on_shutdown("stop", self._stop_fleet)
            self.fleet.start()
        return True

    def _stop_fleet(self) -> None:
        if not self.fleet.stop(STEP_DEADLINE):
            raise RuntimeError("fleet_threads_alive")  # reported as unfinished: nothing it owns is cleaned up

    def shutdown(self, reason: str) -> dict:
        with self._lock:
            repeated = self._stopping
            self._stopping = True
            self.state_writer.close()  # in the same step as admission: no window for a late state request (F1)
            waiters = list(self._waiters)
            steps = sorted(self._steps, key=lambda step: step[0])
        if repeated:
            return {"reason": reason, "repeated": True}
        self.context.close()
        self.notifier.close()  # never joined: it may be inside a host call that waits for this very thread
        for waiter in waiters:
            try:
                waiter.wake("unload_fence")
            except Exception:  # noqa: BLE001 -- one waiter never stops the others
                pass
        results: dict[str, str] = {}
        unfinished: list[str] = []
        for _order, stage, fn, deadline in [s for s in steps if s[1] != "cleanup"]:
            name = _step_name(results, stage)
            results[name] = _run_step(fn, deadline)
            if results[name] == "deadline":
                unfinished.append(name)
        if not self.state_writer.drain(STEP_DEADLINE):
            unfinished.append("state")
        owners_done = not unfinished and all(r == "ok" for r in results.values()) and not self._running()
        for _order, stage, fn, deadline in [s for s in steps if s[1] == "cleanup"]:
            name = _step_name(results, stage)
            results[name] = _run_step(fn, deadline) if owners_done else "skipped"
        return {
            "reason": reason,
            "repeated": False,
            "waiters": len(waiters),
            "admitted": self._running(),
            "steps": results,
            "unfinished": unfinished,
        }

    def _running(self) -> int:
        with self._lock:
            return len(self._admitted)

    # -- settings, generation and permission targets ---------------------------------------------------------------

    def apply_settings(self, base_raw, override_raw, preset_name: str | None, override_error: str | None = None):
        """Freeze what the UI thread read; a change of anything that reaches the server is a new generation.

        Each call takes a revision as it starts; a read that finishes after a newer one was published, or after the
        fence, publishes nothing and returns ``None`` (H1 review R3). Observations are compared with this load's own
        baseline and handed to the state writer -- no file lock on the caller's thread (R4, R5). What is held is
        read from the state file as it is now; a release this observation asks for is published by ``_reconsider``
        once it is written, never assumed here (H1 fix2 review F1-R).
        """
        with self._lock:
            if self._stopping:
                return None
            self._revision += 1
            revision = self._revision
        base = parse_base(base_raw)
        current = effective(base, override_raw, preset_name, override_error)
        slots = slot_targets(base, current)
        state = permits.load_state(self.storage) if self.storage else {}
        basis = permits.intent_basis(state or {})  # what was held when this was seen (H1 fix review F1, F1-R3)
        with self._lock:
            if self._stopping or revision < self._published:
                return None
            changed = [t for s, t in sorted(slots.items()) if s in self._seen and self._seen[s] != t]
            first = [t for s, t in sorted(slots.items()) if s not in self._seen]
            self._seen.update(slots)
            snapshot = self._publish(base, current, slots, _blocked(current, state, self.requested), revision)
            if changed or first:
                self.state_writer.submit(
                    lambda s: permits.record_observation(s, changed, first, basis), self._reconsider
                )
        self.fleet.rebind(snapshot)  # outside the lock; an older snapshot arriving later is ignored there
        return snapshot

    def _publish(self, base: Settings, current: Effective, slots: dict, blocked, revision: int) -> Snapshot:
        """Under ``self._lock``: the new snapshot. A change of anything that reaches the server, or of the decision,
        is a new generation."""
        connection = current.connection if blocked is None else None
        key = (
            current.source,
            current.preset_name,
            current.server_url,
            current.ca_pem,
            base.api_key,
            base.headers,
            blocked,
        )
        if key != self._generation_key:
            self._generation += 1
            self._generation_key = key
        snapshot = Snapshot(self._generation, revision, base, current, connection, blocked, slots)
        self.snapshot = snapshot
        self._published = revision
        return snapshot

    def _reconsider(self, recorded: bool) -> None:
        """On the state writer's thread, after this load's own write: decide the published snapshot again from the
        state file as it is now (H1 fix2 review F1-R). A release the writer refused as stale leaves the target held;
        a written one gives the same settings a client. Only the snapshot that was judged is replaced -- never a
        newer one, never after the fence -- and the UI thread waits for none of it."""
        if not recorded:
            return
        with self._lock:
            snapshot = None if self._stopping else self.snapshot
        if snapshot is None:
            return
        state = permits.load_state(self.storage) if self.storage else {}
        with self._lock:
            if self._stopping or self.snapshot is not snapshot:
                return
            blocked = _blocked(snapshot.effective, state, self.requested)
            if blocked == snapshot.blocked:
                return
            decided = self._publish(snapshot.base, snapshot.effective, snapshot.slots, blocked, snapshot.revision)
        self.fleet.rebind(decided)

    def adopt_storage(self, storage: str) -> None:
        """The host's storage folder, taken in ``on_load``, when import could not derive it."""
        if self.storage is None:
            self.storage = storage
            self.state_writer.adopt(storage)

    def held(self) -> set[str]:
        """The targets held by name (for the tab); see ``holds_everything`` for the overflow case."""
        return permits.held(permits.read_state(self.storage)) if self.storage else set()

    def holds_everything(self) -> bool:
        return permits.holds_everything(permits.read_state(self.storage)) if self.storage else False

    def ask_again(self, targets, done=None) -> bool:
        """The tab's explicit action: the targets are released and asked for at the next start (§4.1). Returns at
        once -- ``False`` after the fence; ``done(recorded)`` follows from the state writer.

        The intent carries the hold epoch it was made at; a load that held something since makes the writer refuse
        it, and ``done`` hears ``False`` (H1 fix review F1). The read takes no lock.
        """
        targets = list(targets)
        basis = permits.intent_basis(permits.read_state(self.storage) if self.storage else {})

        def finished(recorded: bool) -> None:
            self._reconsider(recorded)  # first, so that what ``done`` shows is already decided
            if done is not None:
                done(recorded)

        with self._lock:
            if self._stopping:
                return False
            return self.state_writer.submit(lambda s: permits.ask_again(s, targets, basis), finished)

    def flush(self, timeout: float) -> bool:
        return self.state_writer.flush(timeout)

    def loaded(self) -> bool:
        """``on_load`` ran (on OrcaSlicer's load thread, not the UI thread): this load's request was granted.

        A state that became unreadable since the import is left as it is: its marker stays unfinished as far as
        anyone can tell, and the next import repairs it (H1 fix3 review F1-R2)."""
        try:
            self.update_state(lambda s: permits.complete_load(s, self.load_id))
        except permits.StateUnreadable:
            return False
        return True

    def update_state(self, change) -> dict:
        """A synchronous state change, for import and ``on_load`` only -- never on the UI thread."""
        if self.storage is None:
            return {}
        return permits.update_state(self.storage, change)

    def view(self) -> dict:
        """What the tab is told about the settings and the permission state; nothing secret."""
        snapshot = self.snapshot
        out = {
            "previous_load": self.decision.get("previous_load"),
            "state_repaired": self.decision.get("state_repaired") is True,
            "held": sorted(self.held()),
            "held_all": self.holds_everything(),
            "generation": snapshot.generation if snapshot else 0,
            "blocked": snapshot.blocked if snapshot else None,
        }
        if snapshot is not None:
            out["settings"] = masked_view(snapshot.base, snapshot.effective)
        return out


def _blocked(current: Effective, state: dict | None, requested) -> str | None:
    if current.problems:
        return "problem"
    if current.connection is None:
        return "missing"
    if state is None:
        return "state_unreadable"
    target = f"network:{origin(current.server_url)}"
    if permits.is_held(state, target):
        return "held"
    if target not in requested:
        return "needs_restart"
    return None


def _step_name(results: dict, stage: str) -> str:
    return stage if stage not in results else f"{stage}:{sum(k.startswith(stage) for k in results)}"


def _run_step(fn, deadline: float) -> str:
    """One shutdown step in a daemon thread, waited for at most ``deadline``: the UI thread never waits longer.

    A step past its deadline is left running and reported as ``deadline``; the caller then cleans up nothing.
    """
    outcome: dict[str, str] = {}

    def run():
        try:
            fn()
            outcome["result"] = "ok"
        except Exception as exc:  # noqa: BLE001 -- reported, the next step still runs
            outcome["result"] = type(exc).__name__

    worker = threading.Thread(target=run, name="bamdude-shutdown-step", daemon=True)
    worker.start()
    worker.join(deadline)
    return outcome.get("result", "deadline")
