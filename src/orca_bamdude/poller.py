"""The plugin's one poll scheduler (spec r2 §10).

Every periodic read of the farm goes through here -- printer status, tracked submissions, the printer list -- so
there is one owner of timing, concurrency and cancellation:

- a channel asks only while something wants it (an open page, a selected device, a tracked job); nobody watching
  means no traffic;
- its interval never drops below the server's ``poll_min_seconds`` and counts from the end of the previous request,
  with at most one request in flight per channel, so a slow server never piles up requests;
- a transient failure (timeout, connection, 5xx, 429) backs off exponentially with downward jitter up to
  ``MAX_BACKOFF``, honouring Retry-After within that ceiling; an authentication or configuration failure (401, 403,
  404, TLS, redirect, incompatible server) stops the channel until the connection is configured again, because
  asking again cannot fix it;
- every request carries the connection generation it started under, and an answer from an older generation is
  dropped: after a reconnect, a late answer about the previous server never reaches the page;
- a channel with no successful answer for ``STALE_SECONDS`` is stale, which the page shows as unknown -- never as
  idle.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from orca_bamdude.api import IncompatibleServer
from orca_bamdude.transport import HttpError, TransportError

STATUS_SECONDS = 5.0
TRACKED_SECONDS = 10.0
PRINTERS_SECONDS = 60.0
MAX_BACKOFF = 60.0
STALE_SECONDS = 30.0
TICK_SECONDS = 0.25
JOIN_SECONDS = 5.0

_TRANSIENT_KINDS = {"timeout", "connect", "protocol", "incomplete", "too_large", "cancelled"}
_HALT_STATUSES = {401: "unauthorized", 403: "forbidden", 404: "not_found"}


def classify(exc: BaseException) -> tuple[str | None, float | None]:
    """``(halt code, None)`` for a failure asking again cannot fix; ``(None, retry_after)`` for a transient one."""
    if isinstance(exc, IncompatibleServer):
        return "incompatible", None
    if isinstance(exc, TransportError):
        return (None, None) if exc.kind in _TRANSIENT_KINDS else (exc.kind, None)
    if isinstance(exc, HttpError):
        if exc.status in _HALT_STATUSES:
            return _HALT_STATUSES[exc.status], None
        if exc.status in (408, 429) or exc.status >= 500:
            return None, exc.retry_after
        return f"http_{exc.status}", None
    return None, None


def _default_jitter() -> float:
    return 0.5 + random.random() / 2


@dataclass
class _Channel:
    name: str
    interval: float
    fetch: Callable[[], object]
    deliver: Callable[[object], None]
    wanted: bool = False
    in_flight: bool = False
    next_due: float = 0.0
    failures: int = 0
    halted: str | None = None
    last_success: float | None = None
    started_generation: int = field(default=-1)


class Poller:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        submit: Callable[[Callable[[], None]], None] | None = None,
        jitter: Callable[[], float] = _default_jitter,
        tick_seconds: float = TICK_SECONDS,
    ):
        self._clock = clock
        self._submit = submit or self._spawn
        self._jitter = jitter
        self._tick_seconds = tick_seconds
        self._lock = threading.Lock()
        self._channels: dict[str, _Channel] = {}
        self._generation = 0
        self._poll_min = 0.0
        self._configured_at = clock()
        self._workers: set[threading.Thread] = set()
        self._runner: threading.Thread | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------ configuration

    def configure(self, *, generation: int, poll_min_seconds: float) -> None:
        """A new connection (or new settings): every channel starts over and asks at once."""
        with self._lock:
            self._generation = generation
            self._poll_min = float(poll_min_seconds or 0)
            now = self._clock()
            self._configured_at = now
            for channel in self._channels.values():
                channel.failures, channel.halted, channel.last_success, channel.next_due = 0, None, None, now

    def limit(self, *, poll_min_seconds: float) -> None:
        """The server's floor, learnt from its capabilities after the connection was configured: it lengthens the
        intervals from the next answer on and resets nothing -- not a halt, not a channel's last success."""
        with self._lock:
            self._poll_min = float(poll_min_seconds or 0)

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def add(self, name: str, interval: float, fetch: Callable[[], object], deliver: Callable[[object], None]) -> None:
        with self._lock:
            self._channels[name] = _Channel(name, float(interval), fetch, deliver, next_due=self._clock())

    def want(self, name: str, wanted: bool) -> None:
        """Something does (or no longer does) need this channel; a newly wanted one asks at once."""
        with self._lock:
            channel = self._channels[name]
            if wanted and not channel.wanted:
                channel.next_due = min(channel.next_due, self._clock())
            channel.wanted = wanted

    def wake(self, name: str) -> None:
        """Ask now (an explicit refresh); a request already out is not doubled."""
        with self._lock:
            self._channels[name].next_due = self._clock()

    # ------------------------------------------------------------------ what the page reads

    def halted(self, name: str) -> str | None:
        with self._lock:
            return self._channels[name].halted

    def next_due(self, name: str) -> float:
        with self._lock:
            return self._channels[name].next_due

    def last_success(self, name: str) -> float | None:
        with self._lock:
            return self._channels[name].last_success

    def is_stale(self, name: str) -> bool:
        with self._lock:
            channel = self._channels[name]
            since = channel.last_success if channel.last_success is not None else self._configured_at
            return self._clock() - since >= STALE_SECONDS

    # ------------------------------------------------------------------ scheduling

    def _interval(self, channel: _Channel) -> float:
        return max(channel.interval, self._poll_min)

    def tick(self) -> None:
        """Start every channel that is wanted, due, not halted and not already asking."""
        starts = []
        with self._lock:
            now = self._clock()
            for channel in self._channels.values():
                if channel.wanted and not channel.in_flight and channel.halted is None and channel.next_due <= now:
                    channel.in_flight = True
                    channel.started_generation = self._generation
                    starts.append((channel, self._generation))
        for channel, generation in starts:
            self._submit(lambda channel=channel, generation=generation: self._run(channel, generation))

    def _run(self, channel: _Channel, generation: int) -> None:
        try:
            value, error = channel.fetch(), None
        except Exception as exc:  # every failure is classified; none escapes the worker
            value, error = None, exc
        deliver = False
        with self._lock:
            channel.in_flight = False
            now = self._clock()
            if generation != self._generation:
                channel.next_due = now  # the new connection asks for itself
                return
            interval = self._interval(channel)
            if error is None:
                channel.failures, channel.last_success = 0, now
                channel.next_due = now + interval
                deliver = True
            else:
                halt, retry_after = classify(error)
                if halt is not None:
                    channel.halted = halt
                else:
                    channel.failures += 1
                    delay = min(MAX_BACKOFF, interval * 2**channel.failures) * self._jitter()
                    if retry_after is not None:
                        delay = max(delay, min(retry_after, MAX_BACKOFF))
                    channel.next_due = now + max(interval, delay)
        if deliver:
            try:
                channel.deliver(value)
            except Exception:
                pass  # a page that went away does not stop the channel

    # ------------------------------------------------------------------ running

    def _spawn(self, job: Callable[[], None]) -> None:
        def run() -> None:
            try:
                job()
            finally:
                with self._lock:
                    self._workers.discard(threading.current_thread())

        thread = threading.Thread(target=run, name="orca-bamdude-poll", daemon=True)
        with self._lock:
            self._workers.add(thread)
        thread.start()

    def start(self) -> None:
        self._stop.clear()

        def loop() -> None:
            while not self._stop.is_set():
                self.tick()
                self._stop.wait(self._tick_seconds)

        self._runner = threading.Thread(target=loop, name="orca-bamdude-poller", daemon=True)
        self._runner.start()

    def stop(self, timeout: float = JOIN_SECONDS) -> bool:
        """Stop asking, drop every late answer and join the threads; ``True`` only if they all ended in time.

        A request already out is bounded by the transport's timeout; its answer is discarded."""
        self._stop.set()
        with self._lock:
            self._generation += 1
            threads = list(self._workers)
        if self._runner is not None:
            threads.append(self._runner)
        deadline = time.monotonic() + timeout
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        return not any(thread.is_alive() for thread in threads)
