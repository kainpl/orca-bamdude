"""The plugin's runtime outside OrcaSlicer's API: the load's fence, the notifier and the Runtime.

Host spec r2.3.2 §4.1, §4.3, §4.4 and §4.5. Nothing here imports ``orca``; the adapter hands in the host's functions
and the settings it read on the UI thread.
"""

from __future__ import annotations

import collections
import threading
from dataclasses import dataclass, field

from . import permits
from .config import Connection
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
    ``problem`` (a stored or override value does not validate), ``missing`` (no address or no key), ``held`` (an
    unfinished load held the target, §4.1 ⟨R23-B2⟩) or ``needs_restart`` (this load did not ask for it).
    """

    generation: int
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


class Runtime:
    """One per load, shared by the agent and the page capability (§4.5 ⟨A6⟩).

    It stops when its last capability detaches. Shutdown runs once, in a fixed order:
    1. close admission;
    2. raise the load's fence (callbacks and the notifier go quiet);
    3. wake every waiter;
    4. run the ``cancel``, then ``stop``, then ``cleanup`` steps, each with a deadline.

    The UI thread never waits without a deadline, and never for the notifier.
    """

    def __init__(self, push, storage: str | None, load_id: str, requested: set[str], decision: dict):
        self.context = LoadContext()
        self.notifier = Notifier(push, self.context)
        self.storage = storage
        self.load_id = load_id
        self.requested = frozenset(requested)
        self.decision = dict(decision)
        self.snapshot: Snapshot | None = None
        self._lock = threading.Lock()
        self._attached: set[str] = set()
        self._stopping = False
        self._waiters: set = set()
        self._admitted: dict[int, str] = {}
        self._next_token = 0
        self._steps: list[tuple[int, str, object, float]] = []
        self._generation = 0
        self._generation_key = None

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

    def shutdown(self, reason: str) -> dict:
        with self._lock:
            repeated = self._stopping
            self._stopping = True
            waiters = list(self._waiters)
            admitted = len(self._admitted)
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
        for _order, stage, fn, deadline in steps:
            name = stage if stage not in results else f"{stage}:{sum(k.startswith(stage) for k in results)}"
            results[name] = _run_step(fn, deadline)
        return {"reason": reason, "repeated": False, "waiters": len(waiters), "admitted": admitted, "steps": results}

    # -- settings, generation and permission targets ---------------------------------------------------------------

    def apply_settings(self, base_raw, override_raw, preset_name: str | None) -> Snapshot:
        """Freeze what the UI thread read; a change of anything that reaches the server is a new generation."""
        base = parse_base(base_raw)
        current = effective(base, override_raw, preset_name)
        slots = slot_targets(base, current)
        self._observe(slots)
        blocked = self._blocked(current)
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
        with self._lock:
            if key != self._generation_key:
                self._generation += 1
                self._generation_key = key
            snapshot = Snapshot(self._generation, base, current, connection, blocked, slots)
            self.snapshot = snapshot
        return snapshot

    def _blocked(self, current: Effective) -> str | None:
        if current.problems:
            return "problem"
        if current.connection is None:
            return "missing"
        target = f"network:{origin(current.server_url)}"
        if target in self.held():
            return "held"
        if target not in self.requested:
            return "needs_restart"
        return None

    def _observe(self, slots: dict) -> None:
        if self.storage is None:
            return
        state = permits.read_state(self.storage)
        if permits.observe(state, slots)[0] != state:
            self.update_state(lambda s: permits.observe(s, slots)[0])

    def held(self) -> set[str]:
        return permits.held(permits.read_state(self.storage)) if self.storage else set()

    def ask_again(self, targets) -> None:
        """The tab's explicit action: the targets are released and asked for at the next start (§4.1)."""
        self.update_state(lambda s: permits.ask_again(s, targets))

    def loaded(self) -> None:
        """``on_load`` ran: this load's request was granted."""
        self.update_state(lambda s: permits.complete_load(s, self.load_id))

    def update_state(self, change) -> dict:
        if self.storage is None:
            return {}
        return permits.update_state(self.storage, change)

    def view(self) -> dict:
        """What the tab is told about the settings and the permission state; nothing secret."""
        snapshot = self.snapshot
        out = {
            "previous_load": self.decision.get("previous_load"),
            "held": sorted(self.held()),
            "generation": snapshot.generation if snapshot else 0,
            "blocked": snapshot.blocked if snapshot else None,
        }
        if snapshot is not None:
            out["settings"] = masked_view(snapshot.base, snapshot.effective)
        return out


def _run_step(fn, deadline: float) -> str:
    """One shutdown step in a daemon thread, waited for at most ``deadline``: the UI thread never waits longer."""
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
