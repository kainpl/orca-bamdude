"""Which user-set permission targets a load asks OrcaSlicer for, and which it holds back (host spec r2.3.2 §4.1).

OrcaSlicer asks for permissions once, while the plugin registers, and a refusal discards the whole load: the plugin
is turned off until the user ticks Activate (run 2, RF-2). A target that stays in the request would therefore be
refused again on every start. So:

- **The marker.** Every import writes a load marker naming the user-set targets it asks for; ``on_load`` completes
  it. A marker still unfinished at the next import means that load never got going -- a refusal, or OrcaSlicer
  stopping or crashing before ``on_load``. The plugin cannot tell which (``request_permissions`` returns before the
  prompt, and OrcaSlicer's install state is off limits), so the state it reports is neutral.
- **Holding** (⟨R23-B2⟩). The targets of an unfinished load become held, the base address included. A held target is
  not asked for, and no client is made for it, until the user asks again from the tab (``ask_again``) or a setting
  the runtime watches changes to it (``observe``). Holds accumulate: completed loads, restarts and a later grant from
  another load do not release them (⟨R23.1⟩).
- **Pending.** The last few targets seen or asked for again, so a preset switch within a session finds its origin
  already granted.

Targets are typed strings, ``<bucket>:<target>`` (see ``settings.slot_targets``): origins, a folder, a URL, never a
secret. The fixed targets (threads, the temp model tree, the relay) are the adapter's and are always asked for.

The state file is shared by OrcaSlicer's overlapping processes, so each change is one read-change-write under the
storage folder's lock.
"""

from __future__ import annotations

import json
import os

from .locking import FileLock

STATE_FILE = "state.json"
STATE_LOCK = "state.lock"
LOCK_TIMEOUT = 5.0
MAX_PENDING = 8
MAX_HELD = 32


def _targets(value) -> list[str]:
    return [t for t in value if isinstance(t, str)] if isinstance(value, list) else []


def _with_pending(state: dict, targets) -> dict:
    pending = _targets(state.get("pending_targets"))
    for target in targets:
        if target in pending:
            pending.remove(target)
        pending.append(target)
    return {**state, "pending_targets": pending[-MAX_PENDING:]}


def held(state: dict) -> set[str]:
    return set(_targets(state.get("held_targets")))


def _with_held(state: dict, targets: set[str]) -> dict:
    kept = [t for t in _targets(state.get("held_targets")) if t in targets]
    kept += sorted(targets - set(kept))
    return {**state, "held_targets": kept[-MAX_HELD:]}


def begin_load(state: dict, load_id: str, current: set[str]) -> tuple[dict, set[str], dict]:
    """At import: hold what an unfinished previous load asked for, then decide this load's user targets.

    Returns the new state (with this load's marker), the user targets to ask for, and what was decided -- for the
    tab's explanation.
    """
    marker = state.get("load_marker")
    previous = None
    held_now: list[str] = []
    if isinstance(marker, dict):
        previous = "completed" if marker.get("completed") is True else "unfinished"
        if previous == "unfinished":
            held_now = sorted(set(_targets(marker.get("targets"))))
            state = _with_held(state, held(state) | set(held_now))
    holds = held(state)
    requested = (set(current) | set(_targets(state.get("pending_targets")))) - holds
    state = {**state, "load_marker": {"id": load_id, "targets": sorted(requested), "completed": False}}
    return state, requested, {"previous_load": previous, "held_now": held_now}


def complete_load(state: dict, load_id: str) -> dict:
    """``on_load`` ran: this load's request was granted. Only its own marker is completed."""
    marker = state.get("load_marker")
    if not isinstance(marker, dict) or marker.get("id") != load_id:
        return state
    return {**state, "load_marker": {**marker, "completed": True}}


def observe(state: dict, slots: dict[str, str]) -> tuple[dict, list[str]]:
    """The runtime saw these settings. A slot whose target changed since it was last seen releases that target from
    any hold and queues it; an unchanged one does nothing. Returns the new state and the targets that changed.

    A slot seen for the first time is only remembered (and queued unless held): a first sighting is not a change. The
    first load ever can be the refused one, so its address is held before the runtime has seen anything -- counting
    that first sighting as a change would ask for the refused address again on every start.
    """
    last = state.get("last_seen")
    last = dict(last) if isinstance(last, dict) else {}
    holds = held(state)
    changed, first = [], []
    for slot, target in sorted(slots.items()):
        if slot not in last:
            first.append(target)
        elif last[slot] != target:
            changed.append(target)
        last[slot] = target
    state = {**state, "last_seen": last}
    if changed:
        state = _with_held(state, holds - set(changed))
    queued = changed + [t for t in first if t not in holds]
    if queued:
        state = _with_pending(state, queued)
    return state, changed


def ask_again(state: dict, targets) -> dict:
    """An explicit action in the tab: release these targets and queue them for the next load."""
    targets = [t for t in targets if isinstance(t, str)]
    return _with_pending(_with_held(state, held(state) - set(targets)), targets)


def read_state(folder: str) -> dict:
    """The state as stored; missing or broken is an empty one."""
    try:
        with open(os.path.join(folder, STATE_FILE), encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def update_state(folder: str, change, timeout: float = LOCK_TIMEOUT) -> dict:
    """Read, ``change`` and replace the state atomically under the folder's lock; returns what was written."""
    os.makedirs(folder, exist_ok=True)
    with FileLock(os.path.join(folder, STATE_LOCK), timeout):
        state = change(read_state(folder))
        path = os.path.join(folder, STATE_FILE)
        temp = f"{path}.{os.getpid()}.tmp"
        try:
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump(state, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        except BaseException:
            try:
                os.remove(temp)
            except OSError:
                pass
            raise
    return state
