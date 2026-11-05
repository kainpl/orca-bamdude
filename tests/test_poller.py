"""The one poll scheduler (spec r2 §10): when each channel asks, how it backs off, and what it ignores."""

import time

import pytest

from orca_bamdude.api import IncompatibleServer
from orca_bamdude.poller import MAX_BACKOFF, STALE_SECONDS, Poller
from orca_bamdude.transport import HttpError, TransportError


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Jobs:
    """A submitter that only queues; the test decides when a request finishes."""

    def __init__(self):
        self.queued = []

    def __call__(self, job):
        self.queued.append(job)

    def run_all(self):
        jobs, self.queued = self.queued, []
        for job in jobs:
            job()


class Channel:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = 0
        self.delivered = []

    def fetch(self):
        self.calls += 1
        answer = self.answers.pop(0) if self.answers else "ok"
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def deliver(self, value):
        self.delivered.append(value)


@pytest.fixture
def rig():
    clock, jobs = Clock(), Jobs()
    poller = Poller(clock=clock, submit=jobs, jitter=lambda: 1.0)
    poller.configure(generation=1, poll_min_seconds=3)
    return poller, clock, jobs


def _add(poller, name, interval, channel, wanted=True):
    poller.add(name, interval, channel.fetch, channel.deliver)
    poller.want(name, wanted)


def test_a_wanted_channel_asks_at_once_then_on_its_interval(rig):
    poller, clock, jobs = rig
    status = Channel()
    _add(poller, "status", 5, status)

    poller.tick()
    jobs.run_all()
    assert status.calls == 1 and status.delivered == ["ok"]

    clock.advance(4.9)
    poller.tick()
    jobs.run_all()
    assert status.calls == 1

    clock.advance(0.2)
    poller.tick()
    jobs.run_all()
    assert status.calls == 2


def test_a_channel_nobody_wants_never_asks(rig):
    poller, clock, jobs = rig
    printers = Channel()
    _add(poller, "printers", 60, printers, wanted=False)

    for _ in range(5):
        clock.advance(100)
        poller.tick()
        jobs.run_all()
    assert printers.calls == 0

    poller.want("printers", True)
    poller.tick()
    jobs.run_all()
    assert printers.calls == 1


def test_one_request_in_flight_and_the_interval_counts_from_its_end(rig):
    poller, clock, jobs = rig
    status = Channel()
    _add(poller, "status", 5, status)

    poller.tick()
    for _ in range(4):  # a slow answer: ticks keep coming
        clock.advance(5)
        poller.tick()
    assert len(jobs.queued) == 1

    jobs.run_all()
    clock.advance(4)
    poller.tick()
    assert jobs.queued == []
    clock.advance(1)
    poller.tick()
    assert len(jobs.queued) == 1


def test_no_interval_is_shorter_than_the_servers_minimum(rig):
    poller, clock, jobs = rig
    poller.configure(generation=2, poll_min_seconds=8)
    status = Channel()
    _add(poller, "status", 5, status)

    poller.tick()
    jobs.run_all()
    clock.advance(7.9)
    poller.tick()
    jobs.run_all()
    assert status.calls == 1
    clock.advance(0.2)
    poller.tick()
    jobs.run_all()
    assert status.calls == 2


def test_a_transient_failure_backs_off_to_a_ceiling_and_success_resets_it(rig):
    poller, clock, jobs = rig
    status = Channel(*([TransportError("timeout")] * 8))  # eight failures, then answers
    _add(poller, "status", 5, status)
    delays = []

    poller.tick()
    jobs.run_all()  # failure 1
    for _ in range(7):  # failures 2..8, each after the delay the previous one set
        due = poller.next_due("status")
        delays.append(due - clock.now)
        clock.now = due
        poller.tick()
        jobs.run_all()

    assert delays == [10, 20, 40, 60, 60, 60, 60]
    clock.now = poller.next_due("status")
    poller.tick()
    jobs.run_all()  # the first success
    assert status.calls == 9
    assert poller.next_due("status") - clock.now == 5


def test_backoff_is_jittered_downward():
    clock, jobs = Clock(), Jobs()
    poller = Poller(clock=clock, submit=jobs, jitter=lambda: 0.5)
    poller.configure(generation=1, poll_min_seconds=3)
    status = Channel(TransportError("connect"))
    _add(poller, "status", 5, status)

    poller.tick()
    jobs.run_all()

    assert poller.next_due("status") - clock.now == 5  # 10 s halved, never below the interval


def test_retry_after_is_honoured_up_to_the_ceiling(rig):
    poller, clock, jobs = rig
    busy = HttpError(429, "busy", "", retry_after=45)
    status = Channel(busy, HttpError(503, "restoring", "", retry_after=600))
    _add(poller, "status", 5, status)

    poller.tick()
    jobs.run_all()
    assert poller.next_due("status") - clock.now == 45
    clock.now = poller.next_due("status")
    poller.tick()
    jobs.run_all()
    assert poller.next_due("status") - clock.now == MAX_BACKOFF


@pytest.mark.parametrize(
    "failure,code",
    [
        (HttpError(401, "http_error", ""), "unauthorized"),
        (HttpError(403, "permission_denied", ""), "forbidden"),
        (HttpError(404, "http_error", ""), "not_found"),
        (TransportError("tls"), "tls"),
        (TransportError("redirect"), "redirect"),
        (IncompatibleServer("protocol_version"), "incompatible"),
    ],
)
def test_an_auth_or_configuration_failure_stops_the_channel_until_reconfigured(rig, failure, code):
    poller, clock, jobs = rig
    status = Channel(failure)
    _add(poller, "status", 5, status)

    poller.tick()
    jobs.run_all()
    for _ in range(3):
        clock.advance(120)
        poller.tick()
        jobs.run_all()

    assert status.calls == 1
    assert poller.halted("status") == code
    poller.configure(generation=2, poll_min_seconds=3)
    assert poller.halted("status") is None
    poller.tick()
    jobs.run_all()
    assert status.calls == 2


def test_an_answer_from_an_older_connection_is_ignored(rig):
    poller, clock, jobs = rig
    status = Channel("old printer list")
    _add(poller, "status", 5, status)

    poller.tick()
    poller.configure(generation=2, poll_min_seconds=3)  # reconnect while the request is out
    jobs.run_all()

    assert status.delivered == []
    assert poller.last_success("status") is None
    poller.tick()  # the new connection asks for itself
    jobs.run_all()
    assert status.delivered == ["ok"]


def test_no_successful_answer_for_thirty_seconds_is_stale(rig):
    poller, clock, jobs = rig
    status = Channel("first", *([TransportError("timeout")] * 10))
    _add(poller, "status", 5, status)

    assert not poller.is_stale("status")
    clock.advance(STALE_SECONDS)
    assert poller.is_stale("status")  # never answered since the connection began

    poller.tick()
    jobs.run_all()
    assert not poller.is_stale("status")
    clock.advance(STALE_SECONDS - 1)
    assert not poller.is_stale("status")
    clock.advance(1)
    assert poller.is_stale("status")


def test_wake_asks_now_unless_a_request_is_already_out(rig):
    poller, clock, jobs = rig
    printers = Channel()
    _add(poller, "printers", 60, printers)
    poller.tick()
    jobs.run_all()

    clock.advance(1)
    poller.wake("printers")
    poller.tick()
    assert len(jobs.queued) == 1
    poller.wake("printers")
    poller.tick()
    assert len(jobs.queued) == 1
    jobs.run_all()
    assert printers.calls == 2


def test_a_failing_delivery_does_not_stop_the_channel(rig):
    poller, clock, jobs = rig
    status = Channel()

    def deliver(value):
        raise RuntimeError("the page went away")

    poller.add("status", 5, status.fetch, deliver)
    poller.want("status", True)
    poller.tick()
    jobs.run_all()
    clock.advance(5)
    poller.tick()
    jobs.run_all()

    assert status.calls == 2


def test_the_running_poller_asks_and_stops_within_its_deadline():
    poller = Poller(tick_seconds=0.05)
    poller.configure(generation=1, poll_min_seconds=3)
    status = Channel()
    _add(poller, "status", 5, status)

    poller.start()
    deadline = time.monotonic() + 3
    while not status.delivered and time.monotonic() < deadline:
        time.sleep(0.02)
    started = time.monotonic()
    assert poller.stop(timeout=3) is True
    assert time.monotonic() - started < 3

    assert status.delivered == ["ok"]
