"""Host spec r2.3.2 §4.1 ⟨PF1-r, R23-B2, RF-3, R23.1⟩: which user targets a load asks for, and which it holds."""

import json
import threading

from orca_bamdude.permits import (
    MAX_HELD,
    MAX_PENDING,
    ask_again,
    ask_again_all,
    begin_load,
    complete_load,
    held,
    holds_everything,
    is_held,
    read_state,
    record_observation,
    update_state,
)

A = "network:https://a.example"
B = "network:https://b.example"
DASH_A = "process:https://a.example/"


def started(state, load_id, current):
    state, requested, decision = begin_load(state, load_id, current)
    return state, requested, decision


def test_a_first_load_asks_for_the_current_targets_and_leaves_its_marker_unfinished():
    state, requested, decision = started({}, "L1", {A, DASH_A})
    assert requested == {A, DASH_A}
    assert decision["previous_load"] is None and decision["held_now"] == []
    assert state["load_marker"] == {"id": "L1", "targets": sorted({A, DASH_A}), "completed": False}


def test_on_load_completes_only_its_own_marker():
    state, _, _ = started({}, "L1", {A})
    assert complete_load(state, "other")["load_marker"]["completed"] is False
    assert complete_load(state, "L1")["load_marker"]["completed"] is True


def test_after_a_completed_load_nothing_is_held():
    state, _, _ = started({}, "L1", {A})
    state = complete_load(state, "L1")
    state, requested, decision = started(state, "L2", {A})
    assert requested == {A} and held(state) == set()
    assert decision["previous_load"] == "completed"


def test_an_unfinished_load_holds_every_user_target_it_asked_for_base_included():
    state, _, _ = started({}, "L1", {A, DASH_A})  # refused: on_load never ran
    state, requested, decision = started(state, "L2", {A, DASH_A})
    assert held(state) == {A, DASH_A}
    assert requested == set()
    assert decision == {"previous_load": "unfinished", "held_now": sorted({A, DASH_A})}
    # the new marker names only what this load asked for
    assert state["load_marker"]["targets"] == []


def test_a_held_target_stays_held_across_completed_loads_and_restarts():
    state, _, _ = started({}, "L1", {A})
    state, _, _ = started(state, "L2", {A})  # L1 unfinished -> A held
    state = complete_load(state, "L2")
    for load_id in ("L3", "L4"):
        state, requested, _ = started(state, load_id, {A})
        state = complete_load(state, load_id)
        assert requested == set() and held(state) == {A}


def test_two_aborted_loads_in_a_row_keep_the_earlier_hold():
    state, _, _ = started({}, "L1", {A})
    state, _, _ = started(state, "L2", {A})  # A held, L2 asks for nothing and also never finishes
    state, requested, decision = started(state, "L3", {A})
    assert held(state) == {A} and requested == set()
    assert decision["previous_load"] == "unfinished"


def test_a_change_a_runtime_saw_releases_the_new_value_and_queues_it():
    # Which values changed is decided by the Runtime that saw them (H1 review R5); the state only records it.
    state = record_observation({"held_targets": [A]}, changed=[A], first=[])
    assert held(state) == set() and state["pending_targets"] == [A]
    _, requested, _ = started(state, "L9", {A})
    assert A in requested


def test_a_first_sighting_is_queued_unless_held_and_never_releases_a_hold():
    state = record_observation({"held_targets": [A]}, changed=[], first=[A, DASH_A])
    assert held(state) == {A}
    assert state["pending_targets"] == [DASH_A]


def test_the_state_keeps_no_shared_baseline_of_what_was_seen():
    state = record_observation({}, changed=[B], first=[A])
    assert "last_seen" not in state


def test_a_full_hold_list_holds_everything_instead_of_forgetting_the_oldest():
    # H1 review R5: a memory bound must never turn into an automatic retry of an old refusal.
    holds = [A] + [f"network:https://h{i}.example" for i in range(MAX_HELD - 1)]
    state = {
        "held_targets": holds,
        "load_marker": {"id": "x", "targets": ["network:https://new.example"], "completed": False},
    }
    state, requested, _ = started(state, "next", {A, "network:https://new.example", B})
    assert holds_everything(state)
    assert is_held(state, A) and is_held(state, B) and requested == set()
    assert len(state["held_targets"]) <= MAX_HELD


def test_under_hold_everything_only_an_explicit_target_comes_back():
    state = {"held_all": True, "held_targets": [A]}
    state = ask_again(state, [B])
    assert not is_held(state, B) and is_held(state, A)
    _, requested, _ = started(state, "L1", {A, B})
    assert requested == {B}
    state = ask_again_all(state, [A])
    assert not holds_everything(state) and held(state) == set()
    _, requested, _ = started(state, "L2", {A, B})
    assert requested == {A, B}


def test_asking_again_releases_and_queues_the_target():
    state = {"held_targets": [A, DASH_A]}
    state = ask_again(state, [A])
    assert held(state) == {DASH_A}
    _, requested, _ = started(state, "L5", set())
    assert requested == {A}  # pending even when no current setting names it


def test_pending_keeps_the_newest_few_and_is_asked_for_minus_holds():
    state = {}
    targets = [f"network:https://h{i}.example" for i in range(MAX_PENDING + 3)]
    for target in targets:
        state = ask_again(state, [target])
    assert state["pending_targets"] == targets[-MAX_PENDING:]
    state["held_targets"] = [targets[-1]]
    _, requested, _ = started(state, "L1", set())
    assert requested == set(targets[-MAX_PENDING:-1])


def test_a_broken_state_is_an_empty_one(tmp_path):
    (tmp_path / "state.json").write_text("{not json", encoding="utf-8")
    assert read_state(str(tmp_path)) == {}
    junk = {"load_marker": "x", "held_targets": "y", "pending_targets": [1, None, A], "last_seen": []}
    state, requested, decision = started(junk, "L1", {A})
    assert requested == {A} and decision["previous_load"] is None
    assert held(state) == set()


def test_update_state_is_one_read_change_write_under_the_lock(tmp_path):
    folder = str(tmp_path)

    def bump(state):
        return {**state, "n": state.get("n", 0) + 1}

    workers = [threading.Thread(target=lambda: [update_state(folder, bump) for _ in range(25)]) for _ in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert read_state(folder)["n"] == 100
    assert json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))["n"] == 100
    assert not list(tmp_path.glob("*.tmp"))
