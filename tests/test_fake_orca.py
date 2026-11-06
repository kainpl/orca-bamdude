"""The fake OrcaSlicer itself, where it models a host rule a test leans on (H1 review R6).

``run_on_ui_blocking`` in ``src/slic3r/plugin/host/PluginHostUi.cpp`` (tag v2.5.0-rc) gives a queued call three
states: Pending, Claimed by the UI thread, or Abandoned by a caller that saw OrcaSlicer closing while the call was
still Pending. An abandoned call never runs; a claimed one is always waited for.
"""

import threading

import pytest
from fake_orca import Host


@pytest.fixture
def host(tmp_path):
    host = Host(tmp_path / "data")
    yield host
    host.close()


def push(host, errors, returned):
    try:
        host.orca.host.ui.push_notification(host.orca.host.ui.NotificationLevel.RegularNotificationLevel, "synthetic")
    except RuntimeError:
        errors.append("closing")
    finally:
        returned.set()


def test_closing_abandons_a_pending_call_which_then_never_runs(host):
    gate, entered, returned = threading.Event(), threading.Event(), threading.Event()
    host.ui.post(lambda: (entered.set(), gate.wait(5)))  # the UI thread is busy: our call stays Pending
    errors = []
    caller = threading.Thread(target=push, args=(host, errors, returned))
    caller.start()
    assert entered.wait(5)
    assert host.ui.wait_pending(1, 5)
    host.closing = True
    assert returned.wait(2) and errors == ["closing"]
    gate.set()
    caller.join(5)
    host.ui.call(lambda: None)
    assert host.notifications == []  # the abandoned call was skipped


def test_closing_never_releases_a_caller_whose_call_the_ui_already_claimed(host):
    gate, entered, returned = threading.Event(), threading.Event(), threading.Event()

    class SlowList(list):
        def append(self, item):
            entered.set()
            gate.wait(5)
            super().append(item)

    host.notifications = SlowList()
    errors = []
    caller = threading.Thread(target=push, args=(host, errors, returned))
    caller.start()
    assert entered.wait(5)  # the UI thread is inside the call: Claimed
    host.closing = True
    assert not returned.wait(0.2)
    gate.set()
    caller.join(5)
    assert returned.is_set() and errors == []
    assert len(host.notifications) == 1


def test_a_call_made_while_closing_is_refused_before_it_is_queued(host):
    host.closing = True
    errors, returned = [], threading.Event()
    caller = threading.Thread(target=push, args=(host, errors, returned))
    caller.start()
    caller.join(5)
    host.ui.call(lambda: None)
    assert errors == ["closing"] and host.notifications == []
