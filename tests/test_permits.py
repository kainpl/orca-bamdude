"""Host spec r2.3.2 §4.1 ⟨PF1-r, R23-B2, RF-3, R23.1⟩: which user targets a load asks for, and which it holds."""

import json
import threading

import pytest

from orca_bamdude.permits import (
    MAX_HELD,
    MAX_PENDING,
    StaleIntent,
    StateUnreadable,
    ask_again,
    begin_load,
    complete_load,
    held,
    hold_epoch,
    holds_everything,
    incarnation,
    intent_basis,
    is_held,
    load_state,
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
    state = record_observation({"held_targets": [A]}, changed=[A], first=[], basis=(None, 0))
    assert held(state) == set() and state["pending_targets"] == [A]
    _, requested, _ = started(state, "L9", {A})
    assert A in requested


def test_a_first_sighting_is_queued_unless_held_and_never_releases_a_hold():
    state = record_observation({"held_targets": [A]}, changed=[], first=[A, DASH_A], basis=(None, 0))
    assert held(state) == {A}
    assert state["pending_targets"] == [DASH_A]


def test_the_state_keeps_no_shared_baseline_of_what_was_seen():
    state = record_observation({}, changed=[B], first=[A], basis=(None, 0))
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
    state = ask_again(state, [B], basis=intent_basis(state))
    assert not is_held(state, B) and is_held(state, A)
    _, requested, _ = started(state, "L1", {A, B})
    assert requested == {B}


def test_a_retry_of_the_current_settings_under_hold_everything_leaves_other_refusals_held():
    # H1 fix review F2: an older refusal that is no current setting stays held, even while it is still pending.
    holds = [B] + [f"network:https://h{i}.example" for i in range(MAX_HELD - 1)]
    state = {
        "held_targets": holds,
        "pending_targets": [B],
        "load_marker": {"id": "x", "targets": [A], "completed": False},
    }
    state, requested, _ = started(state, "overflow", {A})
    assert holds_everything(state) and requested == set()
    state = ask_again(state, [A], basis=intent_basis(state))
    state, requested, _ = started(state, "next", {A})
    assert requested == {A} and is_held(state, B)
    state, requested, _ = started(state, "after", {A})  # "next" was refused: A is held again
    assert is_held(state, A) and requested == set()


def test_a_new_refusal_under_hold_everything_re_holds_only_what_that_load_asked_for():
    state = {"held_targets": [f"network:https://h{i}.example" for i in range(MAX_HELD)], "held_all": True}
    state = ask_again(state, [A], basis=intent_basis(state))
    state, requested, _ = started(state, "L1", set())
    assert requested == {A}  # L1 is refused: on_load never runs
    state = ask_again(state, [B], basis=intent_basis(state))
    state, requested, _ = started(state, "L2", set())
    assert is_held(state, A) and requested == {B}  # B, released explicitly, stays released


# -- an intent is judged against the holds made after it (H1 fix review F1) ----------------------------------------


def test_only_a_load_that_holds_something_advances_the_hold_epoch():
    state, _, _ = started({}, "L1", {A})
    assert hold_epoch(state) == 0
    state, _, _ = started(state, "L2", {A})  # L1 unfinished: A held
    assert hold_epoch(state) == 1
    state = complete_load(state, "L2")
    state, _, _ = started(state, "L3", {A})
    assert hold_epoch(state) == 1


def test_a_retry_formed_before_a_newer_hold_is_refused_whole():
    state, _, _ = started({}, "L1", {A})
    formed = intent_basis(state)
    state, _, _ = started(state, "L2", {A})  # the refusal is detected after the retry was formed
    with pytest.raises(StaleIntent):
        ask_again(state, [A], basis=formed)
    state = ask_again(state, [A], basis=intent_basis(state))  # one formed after it is honoured
    assert not is_held(state, A)


def test_a_change_seen_before_a_newer_hold_counts_only_as_a_first_sighting():
    state, _, _ = started({}, "L1", {A})
    formed = intent_basis(state)
    state, _, _ = started(state, "L2", {A, DASH_A})  # A held; DASH_A asked for, not held
    state = record_observation(state, changed=[A, DASH_A], first=[], basis=formed)
    assert is_held(state, A)
    assert state["pending_targets"] == [DASH_A]


def test_asking_again_releases_and_queues_the_target():
    state = {"held_targets": [A, DASH_A]}
    state = ask_again(state, [A], basis=intent_basis(state))
    assert held(state) == {DASH_A}
    _, requested, _ = started(state, "L5", set())
    assert requested == {A}  # pending even when no current setting names it


def test_pending_keeps_the_newest_few_and_is_asked_for_minus_holds():
    state = {}
    targets = [f"network:https://h{i}.example" for i in range(MAX_PENDING + 3)]
    for target in targets:
        state = ask_again(state, [target], basis=intent_basis(state))
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


def test_a_decision_tells_a_missing_state_from_an_unreadable_one(tmp_path):
    folder = str(tmp_path)
    assert load_state(folder) == {}  # none yet: nothing was ever held
    (tmp_path / "state.json").write_text("{not json", encoding="utf-8")
    assert load_state(folder) is None
    (tmp_path / "state.json").write_text("[1, 2]", encoding="utf-8")
    assert load_state(folder) is None
    (tmp_path / "state.json").write_text(json.dumps({"held_targets": [A]}), encoding="utf-8")
    assert load_state(folder) == {"held_targets": [A]}


@pytest.mark.parametrize("content", ["{not json", "[1, 2]"], ids=["corrupt-json", "non-object"])
def test_a_change_never_writes_over_an_unreadable_state(tmp_path, content):
    # H1 fix3 review F1-R2: unknown holds are not erased by an ordinary write.
    (tmp_path / "state.json").write_text(content, encoding="utf-8")
    before = (tmp_path / "state.json").read_bytes()
    with pytest.raises(StateUnreadable):
        update_state(str(tmp_path), lambda s: {**s, "pending_targets": [A]})
    assert (tmp_path / "state.json").read_bytes() == before


def test_only_the_explicit_repair_starts_an_unreadable_state_again(tmp_path):
    repaired = []
    update_state(str(tmp_path), lambda s: {**s, "n": 1}, repair=True, on_repair=lambda: repaired.append(True))
    assert repaired == []  # a missing state is a first start, not a repair
    (tmp_path / "state.json").write_text("{not json", encoding="utf-8")
    written = update_state(str(tmp_path), lambda s: {**s, "n": 2}, repair=True, on_repair=lambda: repaired.append(1))
    assert repaired == [1] and written == {"n": 2} and read_state(str(tmp_path)) == {"n": 2}


# -- a repaired state is a new incarnation (H1 fix4 review F1-R3) ------------------------------------------------


def test_a_load_gives_a_state_without_one_an_incarnation_and_keeps_it():
    state, _, _ = started({}, "L1", {A})
    first = incarnation(state)
    assert isinstance(first, str) and len(first) >= 16
    state, _, _ = started(complete_load(state, "L1"), "L2", {A})
    assert incarnation(state) == first
    assert incarnation(started({}, "L1", {A})[0]) != first  # another new state: another incarnation


def _history(folder, epoch):
    """Reach ``epoch`` through real unfinished loads."""
    for i in range(epoch):
        target = f"network:https://history{i}.example"
        update_state(folder, lambda s, t=target, n=i: begin_load(s, f"refused-{n}", {t})[0])
        update_state(folder, lambda s, n=i: begin_load(s, f"recovered-{n}", set())[0])
        update_state(folder, lambda s, n=i: complete_load(s, f"recovered-{n}"))


def _repair_then_refuse(folder, path):
    """The file is damaged, the import repairs it, its request for A is refused and the next load holds A."""
    path.write_text("{broken", encoding="utf-8")
    update_state(folder, lambda s: begin_load(s, "repaired", {A})[0], repair=True)
    update_state(folder, lambda s: begin_load(s, "recovered", set())[0])
    update_state(folder, lambda s: complete_load(s, "recovered"))
    assert is_held(read_state(folder), A) and hold_epoch(read_state(folder)) == 1


@pytest.mark.parametrize("epoch", [1, 3], ids=["the-same-epoch-again", "an-older-larger-epoch"])
@pytest.mark.parametrize("kind", ["retry", "observation"])
def test_a_release_formed_before_a_repair_cannot_undo_a_refusal_made_after_it(tmp_path, epoch, kind):
    folder = str(tmp_path)
    _history(folder, epoch)
    formed = intent_basis(read_state(folder))
    assert formed[1] == epoch
    _repair_then_refuse(folder, tmp_path / "state.json")
    if kind == "retry":
        with pytest.raises(StaleIntent):
            update_state(folder, lambda s: ask_again(s, [A], basis=formed))
    else:
        update_state(folder, lambda s: record_observation(s, changed=[A], first=[], basis=formed))
    state = read_state(folder)
    assert is_held(state, A)
    _, requested, _ = started(state, "next", {A})
    assert A not in requested
    update_state(folder, lambda s: ask_again(s, [A], basis=intent_basis(s)))  # one formed after it works
    assert not is_held(read_state(folder), A)


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
