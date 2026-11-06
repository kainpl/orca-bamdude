"""Host spec r2.3.2 §4.1, §4.4, §4.5: one Runtime per load -- capability refcount, admission, the fence, the
snapshot and its generation, the permission targets it may use, and shutdown in its fixed order."""

import threading
import time

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


def test_a_shutdown_step_that_fails_or_overruns_does_not_stop_the_others(tmp_path):
    runtime = make(tmp_path)
    ran = []
    runtime.on_shutdown("cancel", lambda: 1 / 0)
    runtime.on_shutdown("stop", lambda: time.sleep(5), deadline=0.1)
    runtime.on_shutdown("cleanup", lambda: ran.append("cleanup"))
    started = time.monotonic()
    report = runtime.shutdown("unload")
    assert time.monotonic() - started < 2
    assert ran == ["cleanup"]
    assert report["steps"]["cancel"] == "ZeroDivisionError"
    assert report["steps"]["stop"] == "deadline"
    assert report["steps"]["cleanup"] == "ok"


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
    runtime.ask_again([A])
    state = read_state(str(tmp_path))
    assert A not in state.get("held_targets", []) and A in state["pending_targets"]


def test_on_load_completes_this_loads_marker(tmp_path):
    runtime = make(tmp_path)
    runtime.update_state(lambda s: {**s, "load_marker": {"id": "L1", "targets": [], "completed": False}})
    runtime.loaded()
    assert read_state(str(tmp_path))["load_marker"]["completed"] is True


def test_a_snapshot_records_what_the_runtime_saw(tmp_path):
    runtime = make(tmp_path, requested={A, B})
    runtime.apply_settings(BASE, {"server_url": "http://192.168.1.5:8000"}, "X1 LAN")
    seen = read_state(str(tmp_path))["last_seen"]
    assert seen["base_network"] == A and seen["effective_network"] == B
