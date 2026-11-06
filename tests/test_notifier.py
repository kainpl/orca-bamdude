"""Host spec r2.3.2 §4.3 ⟨R23-B1, R23.1-A1⟩: the notifier never blocks its caller, owns nothing, never waits at
shutdown, and every callback it hands the host checks its own load's fence."""

import threading
import time

from orca_bamdude.service import LoadContext, Notifier


class BlockingHost:
    """``push_notification`` that blocks like OrcaSlicer's ``run_on_ui_blocking`` until the test releases it."""

    def __init__(self, block=False):
        self.calls = []
        self.entered = threading.Event()
        self.release = threading.Event()
        if not block:
            self.release.set()
        self.raise_closing = False

    def push(self, level, text, hyper_text, on_click):
        self.calls.append((level, text, hyper_text, on_click))
        self.entered.set()
        self.release.wait(10)
        if self.raise_closing:
            raise RuntimeError("OrcaSlicer is closing")


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_no_thread_until_the_first_notification():
    notifier = Notifier(BlockingHost().push, LoadContext())
    assert notifier.thread is None


def test_notifications_are_delivered_in_order_without_blocking_the_caller():
    host = BlockingHost(block=True)
    notifier = Notifier(host.push, LoadContext())
    started = time.monotonic()
    assert notifier.notify("Regular", "one") and notifier.notify("Warning", "two")
    assert time.monotonic() - started < 0.5  # the host call blocks; the caller never does
    assert host.entered.wait(5)
    host.release.set()
    assert wait_for(lambda: len(host.calls) == 2)
    assert [c[1] for c in host.calls] == ["one", "two"]
    assert [c[0] for c in host.calls] == ["Regular", "Warning"]


def test_a_full_queue_refuses_at_once():
    host = BlockingHost(block=True)
    notifier = Notifier(host.push, LoadContext(), capacity=2)
    assert notifier.notify("Regular", "in flight")
    assert host.entered.wait(5)
    assert notifier.notify("Regular", "q1") and notifier.notify("Regular", "q2")
    started = time.monotonic()
    assert notifier.notify("Regular", "dropped") is False
    assert time.monotonic() - started < 0.2
    host.release.set()
    assert wait_for(lambda: len(host.calls) == 3)
    assert "dropped" not in [c[1] for c in host.calls]


def test_the_fence_drops_what_waits_and_never_waits_for_the_call_in_flight():
    host = BlockingHost(block=True)
    context = LoadContext()
    notifier = Notifier(host.push, context)
    notifier.notify("Regular", "in flight")
    assert host.entered.wait(5)
    notifier.notify("Regular", "queued")
    started = time.monotonic()
    context.close()
    notifier.close()
    assert time.monotonic() - started < 0.2  # the call in flight still holds the host; close does not wait
    assert notifier.notify("Regular", "after") is False
    host.release.set()
    assert wait_for(lambda: not notifier.thread.is_alive())
    assert [c[1] for c in host.calls] == ["in flight"]
    assert notifier.dropped == 1


def test_a_click_after_the_fence_does_nothing_even_when_the_host_kept_the_callback():
    host = BlockingHost(block=True)
    context = LoadContext()
    notifier = Notifier(host.push, context)
    clicks = []
    notifier.notify("Warning", "refused", on_click=lambda: clicks.append("handled"), hyper_text="Open")
    assert host.entered.wait(5)
    # OrcaSlicer cleared the plugin's callbacks before our on_unload; this one registered after that, so it is
    # still active in the host -- only our own fence can make it harmless.
    context.close()
    notifier.close()
    host.release.set()
    assert wait_for(lambda: not notifier.thread.is_alive())
    on_click = host.calls[0][3]
    assert on_click() is True  # closes the notification
    assert clicks == []


def test_a_click_before_the_fence_reaches_the_handler():
    host = BlockingHost()
    notifier = Notifier(host.push, LoadContext())
    clicks = []
    notifier.notify("Regular", "done", on_click=lambda: clicks.append("handled") or True, hyper_text="Open")
    assert wait_for(lambda: host.calls)
    assert host.calls[0][2] == "Open"
    assert host.calls[0][3]() is True
    assert clicks == ["handled"]


def test_a_notification_without_a_handler_hands_the_host_no_callback():
    host = BlockingHost()
    notifier = Notifier(host.push, LoadContext())
    notifier.notify("Regular", "plain")
    assert wait_for(lambda: host.calls)
    assert host.calls[0][3] is None and host.calls[0][2] == ""


def test_orca_closing_ends_the_notifier_quietly():
    host = BlockingHost()
    host.raise_closing = True
    notifier = Notifier(host.push, LoadContext())
    notifier.notify("Regular", "late")
    assert wait_for(lambda: notifier.thread is not None and not notifier.thread.is_alive())
    assert notifier.notify("Regular", "later") is False


def test_the_notifier_thread_is_a_daemon():
    notifier = Notifier(BlockingHost().push, LoadContext())
    notifier.notify("Regular", "x")
    assert notifier.thread.daemon


def test_a_guarded_callback_checks_the_fence_of_its_own_load_not_the_newest():
    old, new = LoadContext(), LoadContext()
    calls = []
    guarded = old.guard(lambda: calls.append("old"))
    old.close()
    assert new.closed is False  # a re-import's context never reopens the old one
    assert guarded() is None and calls == []
