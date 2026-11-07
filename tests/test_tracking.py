"""Following what was sent (spec r2 §9.2-§9.4; plan r2 H5): milestones from the server's observation projection,
each said once -- also across a restart -- by the three identities of §11 S3; errors addressed to one operation stop
only it, global ones pause tracking once, temporary ones back off that operation."""

import time

import pytest
from fakes import Server, json_answer

from orca_bamdude.api import Capabilities, ConnectorClient
from orca_bamdude.config import Connection
from orca_bamdude.journal import Journal
from orca_bamdude.tracking import Tracker
from orca_bamdude.transport import HttpError, Transport, TransportError

CAPS = Capabilities("srv", "ep", "key", "0.8.0", True, {"submissions": True}, {})


def job(entry, identity, state="queued", printer=7, assignment=None, executions=(), kind="queue", reason=None):
    out = {
        "entry_id": entry,
        "job": {"kind": kind, "identity": identity},
        "state": state,
        "printer": {"id": printer, "identity": f"p{printer}"} if printer else None,
        "executions": list(executions),
        "executions_complete": True,
    }
    if assignment is not None:
        out["assignment"] = assignment
    if reason:
        out["reason"] = reason
    return out


def execution(identity, state, first_layer=None):
    return {"identity": identity, "state": state, "first_layer": first_layer}


class Farm:
    """Answers GET .../submissions/{op}?observations=1 from a script per operation; ``pages`` maps a cursor to a page."""

    def __init__(self):
        self.pages: dict[str, dict] = {}  # operation id -> {cursor or None: (jobs, next_cursor) | Exception}
        self.calls = []

    def observe(self, operation_id, server_id, epoch, cursor=None):
        self.calls.append((operation_id, cursor))
        answer = self.pages[operation_id][cursor]
        if isinstance(answer, Exception):
            raise answer
        jobs, next_cursor = answer
        return 200, {"operation_id": operation_id, "state": "complete", "entries": [],
                     "observations": {"jobs": jobs, "next_cursor": next_cursor}}  # fmt: skip


@pytest.fixture
def env(tmp_path):
    class Env:
        journal = Journal(str(tmp_path), CAPS.namespace)
        farm = Farm()
        said = []
        now = [time.time()]

        def tracker(self):
            return Tracker(
                lambda: (self.farm, CAPS),
                lambda: str(tmp_path),
                lambda level, text: self.said.append((level, text)),
                names=lambda printer_id: f"Printer {printer_id}",
                say=lambda key: key + " {file} {printer} {count} {reason}",
                clock=lambda: self.now[0],
            )

        def sent(self, name="part.gcode.3mf"):
            record = self.journal.create(name, "a" * 64, origin="native", plates=[1])

            def accepted(r):
                r.phase, r.body, r.result = "accepted", {"x": 1}, {"state": "complete"}

            self.journal.update(record.operation_id, accepted)
            return record.operation_id

    return Env()


def _texts(env):
    return [text.split(" ", 1)[0] for _level, text in env.said]


def test_each_milestone_is_said_once_as_the_job_moves(env):
    op = env.sent()
    tracker = env.tracker()
    steps = [
        [job("e1", "q1")],
        [job("e1", "q1", "printing", executions=[execution("a1", "printing")])],
        [job("e1", "q1", "printing", executions=[execution("a1", "printing", first_layer=True)])],
        [job("e1", "q1", "completed", executions=[execution("a1", "completed")])],
        [job("e1", "q1", "completed", executions=[execution("a1", "completed")])],
    ]
    for jobs in steps:
        env.farm.pages[op] = {None: (jobs, None)}
        tracker.poll()
    assert _texts(env) == ["track.queued", "track.started", "track.first_layer", "track.completed"]
    assert [level for level, _ in env.said] == ["Regular"] * 4


def test_a_dispatch_not_yet_accepted_by_the_printer_is_not_started(env):
    op = env.sent()
    env.farm.pages[op] = {None: ([job("e1", "q1", "printing", executions=[execution("a1", "dispatching")])], None)}
    env.tracker().poll()
    assert _texts(env) == ["track.queued"]


def test_two_executions_of_one_job_are_two_sets_of_milestones(env):
    op = env.sent()
    tracker = env.tracker()
    env.farm.pages[op] = {None: ([job("e1", "q1", "failed", executions=[execution("a1", "failed")])], None)}
    tracker.poll()
    runs = [execution("a2", "completed"), execution("a1", "failed")]
    env.farm.pages[op] = {None: ([job("e1", "q1", "completed", executions=runs)], None)}
    tracker.poll()
    assert _texts(env) == ["track.queued", "track.failed", "track.completed"]
    assert env.said[1][0] == "Warning"


@pytest.mark.parametrize("second", ["p2", "p1"])
def test_each_observed_assignment_is_said_once_even_back_to_the_same_printer(env, second):
    # U -> P1 -> back to the AutoQueue -> P2 (or P1 again), the pending in between never observed: two assignments.
    op = env.sent()
    tracker = env.tracker()

    def assigned(identity, printer):
        return job("e1", "u1", "assigned", kind="auto", printer=printer,
                   assignment={"identity": identity, "printer": {"id": printer, "identity": f"p{printer}"}})  # fmt: skip

    for jobs in ([assigned("q-a", 1)], [assigned("q-a", 1)], [assigned("q-b", 2 if second == "p2" else 1)]):
        env.farm.pages[op] = {None: (jobs, None)}
        tracker.poll()
    assert _texts(env) == ["track.queued", "track.assigned", "track.assigned"]
    env.said.clear()
    restarted = env.tracker()  # a new load reads the same journal: nothing is said again
    restarted.poll()
    assert env.said == []


@pytest.mark.parametrize("reason", ["chain_broken", "out_of_scope", "legacy_receipt"])
def test_a_job_the_server_can_no_longer_follow_is_said_once_and_ends(env, reason):
    op = env.sent()
    tracker = env.tracker()
    env.farm.pages[op] = {None: ([job("e1", None, "unavailable", printer=None, reason=reason)], None)}
    tracker.poll()
    tracker.poll()
    assert _texts(env) == ["track.unavailable"] and env.said[0][0] == "Warning"
    assert len(env.farm.calls) == 1  # the operation's every job is over: it is not asked again


def test_the_tail_past_the_first_page_is_reached(env):
    op = env.sent()
    tracker = env.tracker()
    first = [job(f"e{i}", f"q{i}", "completed", executions=[execution(f"a{i}", "completed")]) for i in range(100)]
    last = [job("e100", "q100", "printing", executions=[execution("a100", "printing")])]
    env.farm.pages[op] = {None: (first, "c1"), "c1": (last, None)}
    tracker.poll()
    assert (op, "c1") in env.farm.calls
    assert "track.started" in _texts(env)


@pytest.mark.parametrize("code", ["printer_scope", "auto_requires_unrestricted_key"])
def test_an_error_addressed_to_one_operation_ends_only_it(env, code):
    narrowed, other = env.sent("narrowed.gcode.3mf"), env.sent("other.gcode.3mf")
    tracker = env.tracker()
    env.farm.pages[narrowed] = {None: HttpError(403, code, "")}
    env.farm.pages[other] = {None: ([job("e1", "q1")], None)}
    tracker.poll()
    env.farm.pages[other] = {None: ([job("e1", "q1", "printing", executions=[execution("a1", "printing")])], None)}
    tracker.poll()
    assert [c for c in env.farm.calls if c[0] == narrowed] == [(narrowed, None)]
    assert "track.started" in _texts(env) and not tracker.paused


@pytest.mark.parametrize(
    "error",
    [HttpError(401, "http_error", ""), HttpError(403, "http_error", ""), HttpError(503, "connector_disabled", "")],
)
def test_a_global_failure_pauses_tracking_once_without_a_retry_loop(env, error):
    op = env.sent()
    tracker = env.tracker()
    env.farm.pages[op] = {None: error}
    for _ in range(5):
        tracker.poll()
    assert len(env.farm.calls) == 1 and tracker.paused
    assert _texts(env) == ["track.paused"]
    tracker.resume()
    env.farm.pages[op] = {None: ([job("e1", "q1")], None)}
    tracker.poll()
    assert "track.queued" in _texts(env)


@pytest.mark.parametrize("error", [HttpError(503, "http_error", ""), TransportError("timeout")])
def test_a_temporary_failure_backs_that_operation_off(env, error):
    op = env.sent()
    tracker = env.tracker()
    env.farm.pages[op] = {None: error}
    tracker.poll()
    tracker.poll()
    assert len(env.farm.calls) == 1 and not tracker.paused  # backed off, not asked again at once
    env.now[0] += 120
    env.farm.pages[op] = {None: ([job("e1", "q1")], None)}
    tracker.poll()
    assert "track.queued" in _texts(env)


def test_a_restored_server_makes_the_operation_unknown(env):
    op = env.sent()
    env.farm.pages[op] = {None: HttpError(409, "context_changed", "")}
    tracker = env.tracker()
    tracker.poll()
    tracker.poll()
    assert _texts(env) == ["track.ended"] and len(env.farm.calls) == 1


def test_an_operation_older_than_a_day_is_not_followed(env):
    op = env.sent()
    env.now[0] += 25 * 3600
    env.farm.pages[op] = {None: ([job("e1", "q1")], None)}
    tracker = env.tracker()
    assert tracker.poll() is False and env.farm.calls == []


# -- the global refusals as the real route gate and transport deliver them, not as a made-up fixture ------------------


@pytest.mark.parametrize(
    "status,body",
    [
        (403, {"detail": "Not enough permissions"}),  # the route gate when queue:create is gone: text, no code
        (401, {"detail": "Could not validate credentials"}),
        (503, {"detail": {"error": "connector_disabled", "message": "Off"}}),
    ],
)
def test_a_global_refusal_from_the_wire_pauses_once(tmp_path, status, body):
    server = Server(lambda m, p: json_answer(status, body))
    client = ConnectorClient(Transport(Connection.build(server.url, "bd_k"), timeout=5))
    journal = Journal(str(tmp_path), CAPS.namespace)
    record = journal.create("part.gcode.3mf", "a" * 64, origin="native")
    journal.update(record.operation_id, lambda r: (setattr(r, "phase", "accepted"), setattr(r, "body", {"x": 1})))
    said = []
    tracker = Tracker(lambda: (client, CAPS), lambda: str(tmp_path), lambda level, text: said.append(text),
                      names=lambda pid: None, say=lambda key: key)  # fmt: skip
    try:
        for _ in range(3):
            tracker.poll()
    finally:
        client.close()
        server.close()
    assert tracker.paused and said == ["track.paused"] and len(server.seen) == 1
