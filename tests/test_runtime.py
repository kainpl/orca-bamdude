"""Host spec r2.3.2 §4.1, §4.4, §4.5: one Runtime per load -- capability refcount, admission, the fence, the
snapshot and its generation, the permission targets it may use, and shutdown in its fixed order."""

import sys
import threading
import time

import pytest

from orca_bamdude import permits, service
from orca_bamdude.locking import FileLock
from orca_bamdude.permits import (
    ask_again,
    begin_load,
    complete_load,
    hold_epoch,
    intent_basis,
    is_held,
    load_state,
    read_state,
)
from orca_bamdude.service import Runtime

KEY = "bd_secret_key_SENTINEL"
BASE = {"server_url": "https://farm.example", "api_key": KEY}
A = "network:https://farm.example"
B = "network:http://192.168.1.5"


class Host:
    def __init__(self):
        self.pushed = []

    def push(self, level, text, hyper_text, on_click):
        self.pushed.append((level, text))


def make(tmp_path, requested=frozenset({A, "process:https://farm.example/"}), decision=None):
    return Runtime(Host().push, str(tmp_path), "L1", requested=set(requested), decision=decision or {})


# -- refcount and shutdown ---------------------------------------------------------------------------------------


def test_the_runtime_stops_only_when_its_last_capability_detaches(tmp_path):
    runtime = make(tmp_path)
    runtime.attach("agent")
    runtime.attach("page")
    assert runtime.detach("page") is None
    assert not runtime.stopping
    report = runtime.detach("agent")
    assert runtime.stopping and report["reason"] == "unload"


def test_shutdown_runs_its_steps_in_the_spec_order_and_only_once(tmp_path):
    runtime = make(tmp_path)
    order = []
    waiter = type("W", (), {"wake": lambda self, why: order.append(f"wake:{why}")})()
    runtime.add_waiter(waiter)
    runtime.on_shutdown("cancel", lambda: order.append("cancel"))
    runtime.on_shutdown("stop", lambda: order.append("stop"))
    runtime.on_shutdown("cleanup", lambda: order.append("cleanup"))
    first = runtime.shutdown("unload")
    second = runtime.shutdown("unload")
    assert order == ["wake:unload_fence", "cancel", "stop", "cleanup"]
    assert first["repeated"] is False and second["repeated"] is True
    assert runtime.context.closed


def test_after_the_fence_nothing_new_is_admitted(tmp_path):
    runtime = make(tmp_path)
    token = runtime.admit("upload")
    assert token is not None
    runtime.shutdown("unload")
    assert runtime.admit("upload") is None
    assert runtime.add_waiter(type("W", (), {"wake": lambda self, why: None})()) is False
    runtime.release(token)  # a release after the fence is harmless


def test_a_step_that_fails_or_overruns_leaves_its_resources_alone(tmp_path):
    # H1 review R2: cleanup only after the owners it cleans up after are known to have finished.
    runtime = make(tmp_path)
    ran = []
    runtime.on_shutdown("cancel", lambda: 1 / 0)
    runtime.on_shutdown("stop", lambda: time.sleep(5), deadline=0.1)
    runtime.on_shutdown("cleanup", lambda: ran.append("cleanup"))
    started = time.monotonic()
    report = runtime.shutdown("unload")
    assert time.monotonic() - started < 2
    assert ran == []
    assert report["steps"]["cancel"] == "ZeroDivisionError"
    assert report["steps"]["stop"] == "deadline"
    assert report["steps"]["cleanup"] == "skipped"
    assert report["unfinished"] == ["stop"]


def test_the_fleet_starts_once_and_its_stop_is_the_runtimes_step(tmp_path):
    runtime = make(tmp_path)
    assert runtime.start_fleet() and runtime.start_fleet()
    assert sum(1 for _order, stage, _fn, _deadline in runtime._steps if stage == "stop") == 1
    report = runtime.shutdown("unload")
    assert report["steps"]["stop"] == "ok"
    assert runtime.start_fleet() is False  # nothing starts after the fence


def test_a_fleet_whose_threads_outlive_their_stop_keeps_cleanup_from_running(tmp_path):
    runtime = make(tmp_path)
    runtime.start_fleet()
    stop = runtime.fleet.stop
    runtime.fleet.stop = lambda timeout: (stop(timeout), False)[1]  # threads still alive at the deadline
    ran = []
    runtime.on_shutdown("cleanup", lambda: ran.append("cleanup"))
    report = runtime.shutdown("unload")
    assert report["steps"]["stop"] == "RuntimeError"
    assert report["steps"]["cleanup"] == "skipped" and ran == []


def test_a_fleet_never_started_owns_no_thread_and_no_step(tmp_path):
    runtime = make(tmp_path)
    report = runtime.shutdown("unload")
    assert "stop" not in report["steps"]
    assert runtime.relay.active_connections == 0


def test_cleanup_waits_for_the_owner_to_actually_finish(tmp_path):
    runtime = make(tmp_path)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    owned = tmp_path / "staging-owned.txt"
    owned.write_text("owned", encoding="utf-8")

    def stop():
        entered.set()
        release.wait(3)
        finished.set()

    runtime.on_shutdown("stop", stop, deadline=0.03)
    runtime.on_shutdown("cleanup", owned.unlink)
    try:
        report = runtime.shutdown("unload")
        assert entered.is_set() and not finished.is_set()
        assert report["steps"]["stop"] == "deadline"
        assert owned.exists()
    finally:
        release.set()
        finished.wait(2)


def test_cleanup_is_skipped_while_an_admitted_operation_has_not_finished(tmp_path):
    runtime = make(tmp_path)
    ran = []
    runtime.admit("upload")  # never released: its owner is still running
    runtime.on_shutdown("cleanup", lambda: ran.append("cleanup"))
    report = runtime.shutdown("unload")
    assert ran == [] and report["steps"]["cleanup"] == "skipped"


def test_shutdown_never_waits_for_the_notifier_held_by_the_host(tmp_path):
    entered, release = threading.Event(), threading.Event()

    def push(level, text, hyper_text, on_click):
        entered.set()
        release.wait(10)

    runtime = Runtime(push, str(tmp_path), "L1", requested=set(), decision={})
    runtime.notify("Regular", "in flight")
    assert entered.wait(5)
    started = time.monotonic()
    runtime.shutdown("unload")
    assert time.monotonic() - started < 0.5
    release.set()


# -- the snapshot, its generation and the targets it may use -----------------------------------------------------


def test_a_snapshot_names_its_source_and_a_change_makes_a_new_generation(tmp_path):
    runtime = make(tmp_path, requested={A, B})
    first = runtime.apply_settings(BASE, None, None)
    same = runtime.apply_settings(dict(BASE), None, None)
    assert first.source == "base" and first.connection.base_url == "https://farm.example"
    assert same.generation == first.generation
    preset = runtime.apply_settings(BASE, {"server_url": "http://192.168.1.5:8000"}, "X1 LAN")
    assert preset.generation == first.generation + 1
    assert preset.source == "preset" and preset.preset_name == "X1 LAN"


def test_a_target_this_load_did_not_ask_for_gets_no_client(tmp_path):
    runtime = make(tmp_path, requested={A})
    snap = runtime.apply_settings(BASE, {"server_url": "http://192.168.1.5:8000"}, "X1 LAN")
    assert snap.connection is None
    assert snap.blocked == "needs_restart"  # new: asked for at the next start


def test_a_held_target_gets_no_client_and_says_so_neutrally(tmp_path):
    decision = {"previous_load": "unfinished", "held_now": [A]}
    runtime = make(tmp_path, requested=set(), decision=decision)
    runtime.update_state(lambda s: {**s, "held_targets": [A]})  # what begin_load wrote at import
    snap = runtime.apply_settings(BASE, None, None)
    assert snap.connection is None
    assert snap.blocked == "held"
    view = runtime.view()
    assert view["previous_load"] == "unfinished" and view["held"] == [A]
    assert KEY not in repr(view)


def test_asking_again_from_the_tab_queues_the_held_target_for_the_next_start(tmp_path):
    decision = {"previous_load": "unfinished", "held_now": [A]}
    runtime = make(tmp_path, requested=set(), decision=decision)
    runtime.update_state(lambda s: {**s, "held_targets": [A]})
    runtime.apply_settings(BASE, None, None)
    done = []
    runtime.ask_again([A], done=done.append)
    assert runtime.flush(5)
    state = read_state(str(tmp_path))
    assert A not in state.get("held_targets", []) and A in state["pending_targets"]
    assert done == [True]


def test_on_load_completes_this_loads_marker(tmp_path):
    runtime = make(tmp_path)
    runtime.update_state(lambda s: {**s, "load_marker": {"id": "L1", "targets": [], "completed": False}})
    runtime.loaded()
    assert read_state(str(tmp_path))["load_marker"]["completed"] is True


def test_what_a_runtime_sees_first_is_queued_but_keeps_no_shared_baseline(tmp_path):
    runtime = make(tmp_path, requested={A, B})
    runtime.apply_settings(BASE, {"server_url": "http://192.168.1.5:8000"}, "X1 LAN")
    assert runtime.flush(5)
    state = read_state(str(tmp_path))
    assert A in state["pending_targets"] and B in state["pending_targets"]
    assert "last_seen" not in state


def test_a_change_this_runtime_saw_releases_the_hold_even_back_to_the_old_value(tmp_path):
    runtime = make(tmp_path, requested=set())
    runtime.update_state(lambda s: {**s, "held_targets": [A]})
    runtime.apply_settings(BASE, None, None)  # first sighting of A: still held
    assert runtime.flush(5) and A in runtime.held()
    runtime.apply_settings({**BASE, "server_url": "http://192.168.1.5:8000"}, None, None)
    runtime.apply_settings(BASE, None, None)  # B was seen in between: A is a change now
    assert runtime.flush(5)
    assert A not in runtime.held()


def test_another_runtime_seeing_another_preset_does_not_release_this_loads_hold(tmp_path):
    old = make(tmp_path, requested={A, "network:https://other.example"})
    other = make(tmp_path, requested={A, "network:https://other.example"})
    old.apply_settings(BASE, None, None)
    old.update_state(lambda s: {**s, "held_targets": [A]})
    other.apply_settings(BASE, {"server_url": "https://other.example"}, "other")
    old.apply_settings(BASE, None, None)  # old never saw anything but A
    assert old.flush(5) and other.flush(5)
    assert A in old.held()


# -- ordering, the fence and the UI thread (H1 review R3, R4) ---------------------------------------------------


def _slow_effective(monkeypatch, marker, entered, release):
    original = service.effective

    def slow(base, override, preset_name=None, override_error=None):
        result = original(base, override, preset_name, override_error)
        if (result.server_url or "").endswith(marker):
            entered.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(service, "effective", slow)


def test_an_older_settings_read_never_publishes_over_a_newer_one(tmp_path, monkeypatch):
    runtime = make(tmp_path, requested={A})
    entered, release = threading.Event(), threading.Event()
    _slow_effective(monkeypatch, "/old", entered, release)
    old = {**BASE, "server_url": BASE["server_url"] + "/old"}
    worker = threading.Thread(target=lambda: runtime.apply_settings(old, None, None))
    worker.start()
    assert entered.wait(5)
    newer = runtime.apply_settings({**BASE, "server_url": BASE["server_url"] + "/new"}, None, None)
    release.set()
    worker.join(5)
    assert runtime.snapshot is newer


def test_settings_cannot_publish_after_shutdown(tmp_path, monkeypatch):
    runtime = make(tmp_path, requested={A})
    entered, release = threading.Event(), threading.Event()
    _slow_effective(monkeypatch, "farm.example", entered, release)
    worker = threading.Thread(target=lambda: runtime.apply_settings(BASE, None, None))
    worker.start()
    assert entered.wait(5)
    runtime.shutdown("unload")
    release.set()
    worker.join(5)
    assert runtime.snapshot is None
    assert runtime.apply_settings(BASE, None, None) is None


def test_settings_and_requests_never_wait_for_the_state_file_lock(tmp_path):
    runtime = make(tmp_path, requested={A})
    held_lock, release = threading.Event(), threading.Event()

    def hold():
        with FileLock(str(tmp_path / "state.lock"), 5):
            held_lock.set()
            release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    assert held_lock.wait(5)
    try:
        started = time.monotonic()
        runtime.apply_settings(BASE, None, None)
        done = []
        runtime.ask_again([A], done=done.append)
        assert time.monotonic() - started < 0.3
        assert done == []  # not recorded yet: never shown as saved before it is
    finally:
        release.set()
        holder.join(5)
    assert runtime.flush(5) and done == [True]
    assert A in read_state(str(tmp_path))["pending_targets"]


def test_shutdown_drains_the_state_writes_it_already_accepted(tmp_path):
    runtime = make(tmp_path, requested={A})
    runtime.ask_again([B])
    runtime.shutdown("unload")
    assert B in read_state(str(tmp_path))["pending_targets"]
    assert runtime.ask_again([A]) is False  # nothing new after the fence


# -- the state writer against the fence and against newer holds (H1 fix review F1) --------------------------------


def test_state_requests_close_with_the_runtime_fence(tmp_path):
    runtime = make(tmp_path)
    entered, release = threading.Event(), threading.Event()
    runtime.on_shutdown("stop", lambda: (entered.set(), release.wait(5)))
    worker = threading.Thread(target=runtime.shutdown, args=("unload",))
    worker.start()
    done = []
    try:
        assert entered.wait(5) and runtime.stopping
        assert runtime.ask_again([A], done=done.append) is False  # no window between the fence and the writer
    finally:
        release.set()
        worker.join(5)
    assert A not in read_state(str(tmp_path)).get("pending_targets", [])


def _late_writer(runtime, monkeypatch):
    """Hold this runtime's state writer just before its transaction, as a slow disk or a busy lock would."""
    entered, release = threading.Event(), threading.Event()
    original = permits.update_state

    def late(folder, change, *args, **kwargs):
        if threading.current_thread() is runtime.state_writer.thread:
            entered.set()
            assert release.wait(5)
        return original(folder, change, *args, **kwargs)

    monkeypatch.setattr(permits, "update_state", late)
    return entered, release, original


def _refused_then_held(write, folder):
    """The next load asks for A and is refused (``on_load`` never runs); the load after it holds A."""
    write(folder, lambda s: begin_load(s, "refused", {A})[0])
    write(folder, lambda s: begin_load(s, "after", {A})[0])


def test_a_late_write_of_an_old_load_cannot_undo_a_newer_refusal(tmp_path, monkeypatch):
    runtime = make(tmp_path, requested={A})
    runtime.apply_settings({**BASE, "server_url": "https://other.example"}, None, None)
    assert runtime.flush(5)
    entered, release, write = _late_writer(runtime, monkeypatch)
    monkeypatch.setattr(service, "STEP_DEADLINE", 0.05)
    try:
        runtime.apply_settings(BASE, None, None)  # this load saw other -> farm: a change it would release
        assert entered.wait(5)
        assert runtime.shutdown("unload")["unfinished"] == ["state"]
        _refused_then_held(write, str(tmp_path))
        assert is_held(read_state(str(tmp_path)), A)
    finally:
        release.set()
    assert runtime.flush(5)
    assert is_held(read_state(str(tmp_path)), A)


def test_a_retry_formed_before_a_newer_hold_is_reported_not_recorded(tmp_path, monkeypatch):
    runtime = make(tmp_path, requested={A})
    permits.update_state(str(tmp_path), lambda s: begin_load(s, "L0", set())[0])  # one incarnation throughout:
    permits.update_state(str(tmp_path), lambda s: complete_load(s, "L0"))  # only the epoch tells this retry stale
    entered, release, write = _late_writer(runtime, monkeypatch)
    done = []
    try:
        assert runtime.ask_again([A], done=done.append)
        assert entered.wait(5)
        _refused_then_held(write, str(tmp_path))
    finally:
        release.set()
    assert runtime.flush(5)
    assert done == [False]  # never shown as recorded: the newer refusal stands
    assert is_held(read_state(str(tmp_path)), A)


# -- a client for a held target only once its release is written (H1 fix2 review F1-R) ----------------------------

OTHER = "network:https://other.example"


def _held_by_another_load(write, folder, target, name):
    """Another load asks for ``target`` after an explicit retry, is refused, and the next one holds it."""
    write(folder, lambda s: ask_again(s, [target], intent_basis(s)))
    write(folder, lambda s: begin_load(s, name, {target})[0])
    write(folder, lambda s: begin_load(s, name + "-recovered", set())[0])
    write(folder, lambda s: complete_load(s, name + "-recovered"))


@pytest.mark.parametrize("newer", [A, OTHER], ids=["same-target", "unrelated-target"])
def test_a_stale_observed_change_leaves_the_held_target_without_a_client(tmp_path, monkeypatch, newer):
    runtime = make(tmp_path, requested={A, OTHER})
    runtime.apply_settings({**BASE, "server_url": "https://other.example"}, None, None)
    assert runtime.flush(5)
    _held_by_another_load(permits.update_state, str(tmp_path), A, "first")
    entered, release, write = _late_writer(runtime, monkeypatch)
    try:
        snap = runtime.apply_settings(BASE, None, None)  # other -> farm while farm is held
        assert snap.connection is None and snap.blocked == "held"  # not before the release is written
        assert entered.wait(5)
        _held_by_another_load(write, str(tmp_path), newer, "newer")
    finally:
        release.set()
    assert runtime.flush(5)
    assert is_held(read_state(str(tmp_path)), A)
    assert runtime.snapshot.connection is None and runtime.snapshot.blocked == "held"


def test_a_change_seen_while_held_gives_a_client_once_its_release_is_written(tmp_path, monkeypatch):
    runtime = make(tmp_path, requested={A, OTHER})
    runtime.apply_settings({**BASE, "server_url": "https://other.example"}, None, None)
    assert runtime.flush(5)
    _held_by_another_load(permits.update_state, str(tmp_path), A, "first")
    entered, release, _write = _late_writer(runtime, monkeypatch)
    try:
        held_snap = runtime.apply_settings(BASE, None, None)
        assert entered.wait(5)
        assert runtime.snapshot.connection is None
    finally:
        release.set()
    assert runtime.flush(5)
    snap = runtime.snapshot
    assert snap.blocked is None and snap.connection.base_url == "https://farm.example"
    assert snap.generation == held_snap.generation + 1 and snap.revision == held_snap.revision


def test_a_tab_retry_gives_a_client_to_a_target_held_after_this_load_began(tmp_path):
    runtime = make(tmp_path, requested={A})
    _held_by_another_load(permits.update_state, str(tmp_path), A, "elsewhere")
    assert runtime.apply_settings(BASE, None, None).blocked == "held"
    done = []
    assert runtime.ask_again([A], done=done.append)
    assert runtime.flush(5) and done == [True]
    assert runtime.snapshot.blocked is None and runtime.snapshot.connection is not None


def test_the_fleet_follows_the_snapshot_a_written_release_decides(tmp_path):
    # The connection a release gives is the fleet's at once: a connect after it is not refused as held.
    runtime = make(tmp_path, requested={A})
    _held_by_another_load(permits.update_state, str(tmp_path), A, "elsewhere")
    heard = []
    runtime.callbacks.set("local_connect", lambda *args: heard.append(args))
    runtime.apply_settings(BASE, None, None)
    runtime.fleet.connect("bamdude-x")
    assert heard == [(1, "bamdude-x", runtime.say("connect.restart"))]
    assert runtime.ask_again([A]) and runtime.flush(5)
    heard.clear()
    runtime.fleet.connect("bamdude-x")  # waits for the list now: nothing refused
    assert heard == []


def test_the_runtimes_sentences_follow_orcas_language(tmp_path):
    runtime = make(tmp_path)
    english = runtime.say("connect.foreign")
    runtime.set_language("uk_UA")
    assert runtime.say("connect.foreign") != english
    runtime.set_language(None)
    assert runtime.say("connect.foreign") == english


def test_a_late_release_is_decided_for_the_newest_settings(tmp_path, monkeypatch):
    runtime = make(tmp_path, requested={A, OTHER})
    runtime.apply_settings({**BASE, "server_url": "https://other.example"}, None, None)
    assert runtime.flush(5)
    _held_by_another_load(permits.update_state, str(tmp_path), A, "first")
    entered, release, _write = _late_writer(runtime, monkeypatch)
    try:
        runtime.apply_settings(BASE, None, None)
        assert entered.wait(5)
        newer = runtime.apply_settings({**BASE, "server_url": "https://farm.example/newer"}, None, None)
    finally:
        release.set()
    assert runtime.flush(5)
    snap = runtime.snapshot  # the old revision's server never comes back
    assert snap.revision == newer.revision and snap.effective.server_url == "https://farm.example/newer"
    assert snap.blocked is None and snap.connection.base_url == "https://farm.example/newer"


def test_a_release_decided_while_a_newer_snapshot_lands_publishes_nothing(tmp_path, monkeypatch):
    runtime = make(tmp_path, requested={A, OTHER})
    runtime.apply_settings({**BASE, "server_url": "https://other.example"}, None, None)
    assert runtime.flush(5)
    _held_by_another_load(permits.update_state, str(tmp_path), A, "first")
    entered, release, _write = _late_writer(runtime, monkeypatch)
    original = permits.load_state
    landed = []

    def newer_lands_first(folder):
        # The reconsider's own read: a newer snapshot is published between its judgement and its publication.
        if sys._getframe(1).f_code.co_name == "_reconsider" and not landed:
            landed.append("in progress")
            landed.append(runtime.apply_settings({**BASE, "server_url": "https://other.example"}, None, None))
        return original(folder)

    try:
        runtime.apply_settings(BASE, None, None)  # other -> farm while farm is held
        assert entered.wait(5)
        monkeypatch.setattr(permits, "load_state", newer_lands_first)
    finally:
        release.set()
    assert runtime.flush(5)
    assert landed[-1] is not None  # the newer snapshot landed while the older release was being decided
    assert runtime.snapshot.revision == landed[-1].revision
    assert runtime.snapshot.effective.server_url == "https://other.example"


def test_a_late_release_never_publishes_after_the_fence(tmp_path, monkeypatch):
    runtime = make(tmp_path, requested={A, OTHER})
    runtime.apply_settings({**BASE, "server_url": "https://other.example"}, None, None)
    assert runtime.flush(5)
    _held_by_another_load(permits.update_state, str(tmp_path), A, "first")
    entered, release, _write = _late_writer(runtime, monkeypatch)
    monkeypatch.setattr(service, "STEP_DEADLINE", 0.05)
    try:
        held_snap = runtime.apply_settings(BASE, None, None)
        assert entered.wait(5)
        runtime.shutdown("unload")
    finally:
        release.set()
    assert runtime.flush(5)
    assert not is_held(read_state(str(tmp_path)), A)  # the release itself was accepted before the fence
    assert runtime.snapshot is held_snap and runtime.snapshot.connection is None


def test_an_unreadable_state_gives_no_client(tmp_path):
    # A state that exists but cannot be read is not "nothing held" (H1 fix2 review F1-R: a failed attempt stays
    # blocked).
    (tmp_path / "state.json").write_text("{not json", encoding="utf-8")
    runtime = make(tmp_path, requested={A})
    snap = runtime.apply_settings(BASE, None, None)
    assert snap.connection is None and snap.blocked == "state_unreadable"


# -- an unreadable state is never written over by an ordinary write (H1 fix3 review F1-R2) ------------------------


@pytest.mark.parametrize("content", ["{not json", "[1, 2]"], ids=["corrupt-json", "non-object"])
def test_an_observation_never_repairs_an_unreadable_state(tmp_path, content):
    runtime = make(tmp_path, requested={A})
    runtime.update_state(lambda s: begin_load(s, "L1", {A})[0])
    runtime.loaded()
    (tmp_path / "state.json").write_text(content, encoding="utf-8")  # damaged before the first UI entry
    before = (tmp_path / "state.json").read_bytes()
    assert runtime.apply_settings(BASE, None, None).blocked == "state_unreadable"
    assert runtime.flush(5)
    assert (tmp_path / "state.json").read_bytes() == before
    assert runtime.snapshot.blocked == "state_unreadable" and runtime.snapshot.connection is None


def test_a_failed_read_inside_the_writer_neither_erases_a_hold_nor_unblocks(tmp_path, monkeypatch):
    runtime = make(tmp_path, requested={A})
    runtime.update_state(lambda s: {"held_targets": [A], "hold_epoch": 1})
    original = permits.load_state
    failed = []

    def unreadable_once(folder):
        # The snapshot's read works; only the writer's transaction read fails, as a transient read error would.
        if threading.current_thread() is runtime.state_writer.thread and not failed:
            failed.append(True)
            return None
        return original(folder)

    monkeypatch.setattr(permits, "load_state", unreadable_once)
    assert runtime.apply_settings(BASE, None, None).blocked == "held"  # a first sighting
    assert runtime.flush(5) and failed
    assert original(str(tmp_path)) == {"held_targets": [A], "hold_epoch": 1}
    assert runtime.snapshot.blocked == "held" and runtime.snapshot.connection is None


def test_a_tab_retry_over_an_unreadable_state_is_reported_not_recorded(tmp_path):
    runtime = make(tmp_path, requested={A})
    (tmp_path / "state.json").write_text("{not json", encoding="utf-8")
    done = []
    assert runtime.ask_again([A], done=done.append)
    assert runtime.flush(5) and done == [False]
    assert load_state(str(tmp_path)) is None


def test_on_load_over_an_unreadable_state_writes_nothing_and_does_not_raise(tmp_path):
    runtime = make(tmp_path)
    (tmp_path / "state.json").write_text("{not json", encoding="utf-8")
    assert runtime.loaded() is False
    assert (tmp_path / "state.json").read_text(encoding="utf-8") == "{not json"


def test_a_tab_retry_formed_before_a_repair_is_not_recorded_over_the_newer_refusal(tmp_path, monkeypatch):
    # H1 fix4 review F1-R3: the repair starts a new incarnation, so an older intent cannot reuse its epoch numbers.
    runtime = make(tmp_path, requested={A})
    history = "network:https://history.example"
    permits.update_state(str(tmp_path), lambda s: begin_load(s, "L1", {history})[0])
    permits.update_state(str(tmp_path), lambda s: begin_load(s, "L2", set())[0])  # L1 refused: epoch 1
    permits.update_state(str(tmp_path), lambda s: complete_load(s, "L2"))
    assert hold_epoch(read_state(str(tmp_path))) == 1
    entered, release, write = _late_writer(runtime, monkeypatch)
    done = []
    try:
        assert runtime.ask_again([A], done=done.append)
        assert entered.wait(5)
        (tmp_path / "state.json").write_text("{broken", encoding="utf-8")
        write(str(tmp_path), lambda s: begin_load(s, "repaired", {A})[0], repair=True)
        _refused_then_held(write, str(tmp_path))
    finally:
        release.set()
    assert runtime.flush(5) and done == [False]
    assert is_held(read_state(str(tmp_path)), A)


# -- after a restart the journal's open records are asked about, GETs only (spec r2 §7) -----------------------------


def test_the_runtime_reconciles_a_namespaces_open_records_once_it_reads_its_list(tmp_path):
    import time as _time

    from orca_bamdude.api import Capabilities
    from orca_bamdude.journal import Journal

    caps = Capabilities("srv", "ep", "key", "0.8.0", True, {}, {})
    journal = Journal(str(tmp_path), caps.namespace)
    record = journal.create("part.gcode.3mf", "a" * 64, origin="native")

    def unknown(stored):
        stored.phase, stored.body = "unknown", {"operation_id": record.operation_id}

    journal.update(record.operation_id, unknown)
    pushed, calls = [], []

    class Client:
        def get(self, operation_id, server_id, epoch):
            calls.append(("get", operation_id))
            return 200, {"state": "complete", "entries": []}

    runtime = Runtime(lambda *a: pushed.append(a), str(tmp_path), "L", requested=set(), decision={})
    runtime.fleet.on_namespace(Client(), caps)
    deadline = _time.monotonic() + 3
    while not pushed and _time.monotonic() < deadline:
        _time.sleep(0.02)
    assert calls == [("get", record.operation_id)]
    assert journal.load(record.operation_id).phase == "accepted"
    assert pushed and pushed[0][0] == "Regular" and "part.gcode.3mf" in pushed[0][1]
