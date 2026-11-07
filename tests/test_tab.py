"""The tab's own logic (spec r2 §8): the live-monitoring lease, the stepwise connection check, and what the page is
told about the fleet and the history -- never a secret."""

import pytest
from fakes import Server, json_answer

from orca_bamdude.config import Connection
from orca_bamdude.journal import Journal
from orca_bamdude.tab import LEASE_SECONDS, Lease, check_connection, fleet_rows, history_rows

# -- the live-monitoring lease (§8.2, RF-12) ----------------------------------------------------------------------------


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_the_lease_starts_off_and_an_action_while_off_does_not_switch_it_on():
    clock = Clock()
    lease = Lease(clock)
    assert lease.wanted() is False
    lease.activity()
    assert lease.wanted() is False and lease.on is False


def test_switching_on_opens_ten_minutes_that_each_action_renews():
    clock = Clock()
    lease = Lease(clock)
    lease.switch(True)
    clock.now += LEASE_SECONDS - 1
    lease.activity()
    clock.now += LEASE_SECONDS - 1
    assert lease.wanted() is True
    clock.now += 2
    assert lease.wanted() is False and lease.expired() is True and lease.on is False


def test_a_pulse_does_not_renew_the_lease():
    clock = Clock()
    lease = Lease(clock)
    lease.switch(True)
    clock.now += LEASE_SECONDS - 1
    lease.pulse()
    clock.now += 2
    assert lease.wanted() is False


def test_a_hidden_window_pauses_the_demand_without_extending_the_lease():
    clock = Clock()
    lease = Lease(clock)
    lease.switch(True)
    lease.visibility(hidden=True)
    assert lease.wanted() is False and lease.on is True
    clock.now += LEASE_SECONDS + 1
    lease.visibility(hidden=False)
    assert lease.wanted() is False and lease.on is False


def test_the_expiry_is_told_once():
    clock = Clock()
    lease = Lease(clock)
    lease.switch(True)
    clock.now += LEASE_SECONDS + 1
    assert lease.expired() is True and lease.expired() is False


# -- the stepwise connection check (§8.1) --------------------------------------------------------------------------


CAPS = {
    "product": "bamdude",
    "protocol_version": 1,
    "connector_server_id": "s",
    "storage_epoch": "e",
    "actor_id": "a",
    "server_version": "0.8.0",
    "enabled": True,
    "features": {"monitor": True, "library_upload": True, "submissions": False, "camera_stream": True},
    "limits": {},
}


def _farm(caps=CAPS, printers=None, caps_status=200):
    def route(method, path):
        if path.endswith("/connector/capabilities"):
            return json_answer(caps_status, caps)
        if path.endswith("/printers/"):
            return json_answer(
                200,
                printers
                if printers is not None
                else [
                    {"id": 1, "model_code": "C12", "connector_identity": "u1"},
                    {"id": 2, "connector_identity": "u2"},
                ],
            )
        return json_answer(404, {})

    return Server(route)


def _steps(result):
    return [(step["step"], step["ok"], step.get("code")) for step in result]


def test_a_working_connection_passes_every_step_and_names_the_missing_rights():
    server = _farm()
    try:
        steps = check_connection(Connection.build(server.url, "bd_k"), blocked=None)
    finally:
        server.close()
    assert _steps(steps)[:5] == [
        ("permission", True, None),
        ("reach", True, None),
        ("protocol", True, None),
        ("key", True, None),
        ("printers", True, None),
    ]
    features = steps[5]
    assert features["step"] == "features" and features["missing"] == ["submissions"]
    assert steps[4]["count"] == 2 and steps[4]["known"] == 1


def test_a_target_orca_has_not_allowed_stops_at_the_first_step():
    steps = check_connection(Connection.build("https://farm.example", "bd_k"), blocked="needs_restart")
    assert _steps(steps) == [("permission", False, "needs_restart")]


@pytest.mark.parametrize(
    "caps,status,step,code",
    [
        (CAPS, 401, "key", "unauthorized"),
        (CAPS, 403, "key", "forbidden"),
        ({**CAPS, "protocol_version": 2}, 200, "protocol", "protocol_version"),
        ({**CAPS, "product": "other"}, 200, "protocol", "not_bamdude"),
        ({**CAPS, "enabled": False}, 200, "protocol", "connector_disabled"),
    ],
)
def test_each_refusal_has_its_own_answer(caps, status, step, code):
    server = _farm(caps=caps, caps_status=status)
    try:
        steps = check_connection(Connection.build(server.url, "bd_k"), blocked=None)
    finally:
        server.close()
    assert _steps(steps)[-1] == (step, False, code)


def test_a_proxys_login_page_is_named_as_such():
    server = Server(lambda m, p: (200, {"Content-Type": "text/html"}, b"<html>login</html>"))
    try:
        steps = check_connection(Connection.build(server.url, "bd_k"), blocked=None)
    finally:
        server.close()
    assert _steps(steps)[-1] == ("reach", False, "html_answer")


def test_an_unreachable_server_stops_at_reach():
    steps = check_connection(Connection.build("http://127.0.0.1:9", "bd_k"), blocked=None, timeout=2)
    assert _steps(steps)[-1][:2] == ("reach", False)


# -- what the page is told ---------------------------------------------------------------------------------------------


def test_fleet_rows_carry_only_what_the_tab_shows():
    printers = [
        {"id": 1, "name": "X1C left", "model_code": "BL-P001", "connector_identity": "uuid-1", "ip": "10.0.0.5"}
    ]
    statuses = {1: {"state": "RUNNING", "progress": 40.0, "remaining_time": 600, "connected": True, "access_code": "x"}}
    (row,) = fleet_rows(printers, statuses)
    assert row == {
        "id": 1,
        "name": "X1C left",
        "model": "BL-P001",
        "state": "RUNNING",
        "progress": 40.0,
        "remaining": 600,
        "connected": True,
    }


def test_history_rows_name_what_happened_and_what_can_be_done(tmp_path):
    journal = Journal(str(tmp_path), ("srv", "ep", "key"))
    record = journal.create("part.gcode.3mf", "a" * 64, origin="native", plates=[2], target={"kind": "printer"})
    journal.update(record.operation_id, lambda r: setattr(r, "phase", "unknown"))
    other = Journal(str(tmp_path), ("srv", "old", "key"))
    other.create("old.gcode.3mf", "b" * 64, origin="manual")
    rows = history_rows(journal, str(tmp_path))
    mine = next(r for r in rows if r["file"] == "part.gcode.3mf")
    theirs = next(r for r in rows if r["file"] == "old.gcode.3mf")
    assert mine["phase"] == "unknown" and mine["actions"] == ["reconcile"] and mine["foreign"] is False
    assert theirs["foreign"] is True and theirs["actions"] == []
    assert "a" * 64 not in str(rows)  # no hash, body or key goes to the page beyond what it shows
