"""The host's job thread and the plugin's send (spec r2 §4.3, RF-6).

OrcaSlicer runs ``start_*`` on a worker thread and, on Cancel, holds its UI thread in ``wait_for_idle`` with no
timeout until ``start_*`` returns. So that thread only orchestrates: the send runs on a plugin thread with an event
of its own, and the job thread polls the host's ``cancel_fn`` with a short tick -- on a Cancel it signals the event
and returns -18 at once, waiting for nothing. ``update_fn`` (non-blocking: it queues on the host's worker) is called
by the job thread alone, and none of the host's callbacks is ever handed to work that outlives ``start_*``: the host's
C++ lambdas capture the job's locals by reference.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from .submit import Outcome

OK, FAILED, CANCELLED = 0, -1, -18  # BAMBU_NETWORK_SUCCESS / ERR / ERR_CANCELED (bambu_networking.hpp)
UPLOAD_STAGE = 1  # SendingPrintJobStage::Upload -- not bound to Python; Finished is never sent (spec r2 §6.2 step 11)
TICK = 0.1


class Job:
    """One send on its own plugin thread. ``outcome`` is set before ``done``."""

    def __init__(self, work: Callable[[threading.Event, Callable[[str, int], None]], Outcome], name="bamdude-send"):
        self._work = work
        self._name = name
        self.cancel = threading.Event()
        self.done = threading.Event()
        self.outcome: Outcome | None = None
        self._lock = threading.Lock()
        self._progress: tuple[int, str, int] = (0, "", 0)

    def start(self) -> None:
        threading.Thread(target=self._run, name=self._name, daemon=True).start()

    def report(self, stage: str, percent: int) -> None:
        with self._lock:
            seq = self._progress[0] + 1
            self._progress = (seq, stage, max(0, min(100, int(percent))))

    def progress(self) -> tuple[int, str, int]:
        with self._lock:
            return self._progress

    def _run(self) -> None:
        try:
            self.outcome = self._work(self.cancel, self.report)
        except Exception:  # noqa: BLE001 -- a broken send is a refusal the person sees, never a hung dialog
            self.outcome = Outcome("refused", "internal")
        finally:
            self.done.set()


def _asked_to_cancel(cancel_fn) -> bool:
    if cancel_fn is None:
        return False
    try:
        return bool(cancel_fn())
    except Exception:  # noqa: BLE001 -- a callback that cannot answer has not asked
        return False


def _code(outcome: Outcome | None) -> int:
    if outcome is None or outcome.result == "refused":
        return FAILED
    if outcome.result == "cancelled":
        return CANCELLED
    # accepted, or unknown: the server most likely has it; a negative code would invite a second, duplicate send.
    # The plugin's own notification says which (spec r2 §6.2 step 11).
    return OK


def run_for_host(job: Job, update_fn, cancel_fn, say, tick: float = TICK) -> int:
    """Run ``job`` for one ``start_*`` call, on the host's job thread; return the host's result code."""
    job.start()
    forwarded = 0
    while not job.done.wait(tick):
        if _asked_to_cancel(cancel_fn):
            job.cancel.set()
            return CANCELLED
        forwarded = _forward(job, update_fn, say, forwarded)
    _forward(job, update_fn, say, forwarded)
    return _code(job.outcome)


def _forward(job: Job, update_fn, say, forwarded: int) -> int:
    seq, stage, percent = job.progress()
    if update_fn is not None and seq != forwarded and stage:
        try:
            update_fn(UPLOAD_STAGE, percent, say(f"progress.{stage}"))
        except Exception:  # noqa: BLE001 -- the host's progress is a courtesy
            pass
    return seq
