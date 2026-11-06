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
  a Runtime watches changes to it during its own load (``record_observation``; the baseline is that Runtime's, not
  this shared file's). Holds accumulate: completed loads, restarts and a later grant from another load do not
  release them (⟨R23.1⟩). More holds than ``MAX_HELD`` hold everything rather than forget the oldest; then only
  what the tab names comes back, and a later refusal holds again only what that load asked for.
- **The hold epoch** (H1 fix review F1). A load that holds something advances ``hold_epoch``. A release is an intent
  formed at some moment -- a click in the tab, a change a Runtime saw -- and it carries the epoch it saw then. Applied
  later, under the lock, it may undo only holds that existed when it was formed: a refusal detected after it stands.
  A retry formed before a newer hold is refused whole (``StaleIntent``); an observed change formed before it counts
  only as a first sighting. One number, not one per target: a release that races a restart is rare, and losing it
  costs one click in the tab.
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


class StaleIntent(Exception):
    """A release formed before a newer hold: the newer refusal stands, and nothing is written."""


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
    """The targets held by name. Under ``holds_everything`` every target is held except the ones released since."""
    return set(_targets(state.get("held_targets")))


def hold_epoch(state: dict) -> int:
    value = state.get("hold_epoch")
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def holds_everything(state: dict) -> bool:
    return state.get("held_all") is True


def is_held(state: dict, target: str) -> bool:
    if holds_everything(state):
        return target not in _targets(state.get("released"))
    return target in held(state)


def _with_held(state: dict, targets: set[str]) -> dict:
    """Hold exactly ``targets``. More than ``MAX_HELD`` never drops the oldest -- that would ask for an old refusal
    again by itself (H1 review R5); it holds everything instead, until the tab releases targets explicitly. Once
    everything is held, the names are kept for the tab only, and what the tab released stays released."""
    kept = [t for t in _targets(state.get("held_targets")) if t in targets]
    kept += sorted(targets - set(kept))
    if len(kept) <= MAX_HELD:
        return {**state, "held_targets": kept}
    if holds_everything(state):
        return {**state, "held_targets": kept[:MAX_HELD]}
    return {**state, "held_targets": kept[:MAX_HELD], "held_all": True, "released": []}


def _released(state: dict, targets) -> dict:
    """Under ``holds_everything``: these targets come back; the rest stays held."""
    if not holds_everything(state):
        return state
    released = [t for t in _targets(state.get("released")) if t not in targets] + list(targets)
    return {**state, "released": released[-MAX_PENDING:]}


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
            if holds_everything(state):
                state = {**state, "released": [t for t in _targets(state.get("released")) if t not in held_now]}
            state = _with_held(state, held(state) | set(held_now))
            if held_now:
                state = {**state, "hold_epoch": hold_epoch(state) + 1}
    wanted = set(current) | set(_targets(state.get("pending_targets")))
    requested = {t for t in wanted if not is_held(state, t)}
    state = {**state, "load_marker": {"id": load_id, "targets": sorted(requested), "completed": False}}
    return state, requested, {"previous_load": previous, "held_now": held_now}


def complete_load(state: dict, load_id: str) -> dict:
    """``on_load`` ran: this load's request was granted. Only its own marker is completed."""
    marker = state.get("load_marker")
    if not isinstance(marker, dict) or marker.get("id") != load_id:
        return state
    return {**state, "load_marker": {**marker, "completed": True}}


def record_observation(state: dict, changed, first, epoch: int) -> dict:
    """What one Runtime saw (H1 review R5): ``changed`` are targets of settings it saw change during its own load --
    released from any hold and queued; ``first`` are its first sightings -- queued unless held, never released.

    The baseline of what was seen lives in that Runtime's memory, not in this shared file: another process seeing
    another preset is not this load's change. A first sighting is not a change, so the first load ever -- possibly
    the refused one -- never asks for its refused address again by itself. A change seen at ``epoch``, before a
    newer hold, is only a first sighting (H1 fix review F1).
    """
    changed = [t for t in changed if isinstance(t, str)]
    if hold_epoch(state) > epoch:
        changed, first = [], changed + list(first)
    state = _released(state, changed)
    state = _with_held(state, held(state) - set(changed)) if changed else state
    queued = changed + [t for t in first if isinstance(t, str) and not is_held(state, t)]
    return _with_pending(state, queued) if queued else state


def ask_again(state: dict, targets, epoch: int) -> dict:
    """An explicit action in the tab, made at ``epoch``: release these targets and queue them for the next load.
    Raises ``StaleIntent`` when a load held something after it was made (H1 fix review F1)."""
    if hold_epoch(state) > epoch:
        raise StaleIntent
    targets = [t for t in targets if isinstance(t, str)]
    state = _released(state, targets)
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
