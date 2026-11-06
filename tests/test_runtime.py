"""Host spec r2.3.2 §4.1, §4.4, §4.5: one Runtime per load -- capability refcount, admission, the fence, the
snapshot and its generation, the permission targets it may use, and shutdown in its fixed order."""

import threading
import time

from orca_bamdude import service
from orca_bamdude.locking import FileLock
from orca_bamdude.permits import read_state
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
