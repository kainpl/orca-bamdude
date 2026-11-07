"""The host's job thread only orchestrates (spec r2 §4.3, RF-6): the send runs on a plugin thread, a Cancel returns
-18 within a second without waiting for it, and the host's callbacks are called by the job thread alone."""

import threading
import time

from orca_bamdude.jobs import CANCELLED, FAILED, OK, Job, run_for_host
from orca_bamdude.submit import Outcome


def _say(key):
    return key


def test_an_accepted_send_returns_0_and_its_progress_reaches_the_host_from_the_job_thread():
    calls = []

    def work(cancel, report):
        report("uploading", 50)
        time.sleep(0.15)
        report("uploading", 100)
        time.sleep(0.15)
        return Outcome("accepted", "queued")

    me = threading.get_ident()
    code = run_for_host(Job(work), lambda s, c, m: calls.append((threading.get_ident(), s, c, m)), lambda: False, _say)
    assert code == OK
    assert calls and all(ident == me for ident, *_ in calls)
    assert (1, 100, "progress.uploading") in [(s, c, m) for _, s, c, m in calls]


def test_a_cancel_returns_minus_18_at_once_and_signals_the_plugin_thread():
    signalled, update_calls = threading.Event(), []
    cancel_pressed = threading.Event()

    def work(cancel, report):
        cancel.wait(5)  # the window is open
        signalled.set()
        time.sleep(0.2)  # the server is asked to stop; the host is long gone
        report("submitting", 100)
        return Outcome("cancelled", "cancelled")

    threading.Timer(0.1, cancel_pressed.set).start()
    started = time.monotonic()
    job = Job(work)
    code = run_for_host(job, lambda *a: update_calls.append(a), cancel_pressed.is_set, _say)
    returned = len(update_calls)
    assert code == CANCELLED and time.monotonic() - started < 1.0
    assert signalled.wait(2) and job.done.wait(2)
    assert len(update_calls) == returned  # nothing reaches the host's callbacks after start_* returned


def test_a_cancel_in_the_plugins_own_window_returns_minus_1():
    # H7 B4s/E5, owner's decision 2026-11-08: rc SendJob/PrintJob finish nothing on an ERR_CANCELED they did not
    # cause -- the dialog stayed "Sending ... 30 %" until its own Cancel. -1 frees it; our notification says cancelled.
    cancelled_here = Job(lambda c, r: Outcome("cancelled", "cancelled"))
    assert run_for_host(cancelled_here, None, lambda: False, _say) == FAILED


def test_a_cancel_orca_asked_for_as_the_send_ended_stays_minus_18():
    asked = threading.Event()

    def work(cancel, report):
        asked.set()  # Orca's Cancel lands while the plugin thread is finishing its own cancel
        return Outcome("cancelled", "cancelled")

    assert run_for_host(Job(work), None, asked.is_set, _say, tick=0.5) == CANCELLED


def test_a_refusal_returns_minus_1_and_an_unknown_answer_0():
    assert run_for_host(Job(lambda c, r: Outcome("refused", "x")), None, lambda: False, _say) == FAILED
    assert run_for_host(Job(lambda c, r: Outcome("unknown", "submit_unknown")), None, lambda: False, _say) == OK


def test_a_send_that_breaks_is_a_refusal_not_a_hang():
    def work(cancel, report):
        raise RuntimeError("boom")

    job = Job(work)
    assert run_for_host(job, None, lambda: False, _say) == FAILED
    assert job.outcome.result == "refused" and job.outcome.code == "internal"


def test_a_cancel_callback_that_raises_does_not_cancel():
    def raising():
        raise RuntimeError("host callback")

    assert run_for_host(Job(lambda c, r: Outcome("accepted", "queued")), None, raising, _say) == OK
