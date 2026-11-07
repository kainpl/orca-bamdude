"""The choice window (spec r2 §6.3, §4.3): what it shows, what it accepts, and how its answer is waited for."""

import threading
import time
import zipfile

import pytest

from orca_bamdude.choice_window import Answer, ask, parse, render
from orca_bamdude.inspector import inspect
from orca_bamdude.service import LoadContext
from orca_bamdude.staging import Staged
from orca_bamdude.submit import Choice, Offer, Request, Target

PRINTER = Target.printer(7, "11111111-1111-4111-8111-111111111111")


def _offer(tmp_path, kind="print", plates=(1, 2), proposed=(1, 2), action="queue_asap", name="Cube<b>"):
    path = tmp_path / "p.3mf"
    blocks = "".join(f'<plate><metadata key="index" value="{i}"/></plate>' for i in plates)
    with zipfile.ZipFile(path, "w") as archive:
        for index in plates:
            archive.writestr(f"Metadata/plate_{index}.gcode", "G28\n")
        archive.writestr("Metadata/slice_info.config", f"<config>{blocks}</config>")
    request = Request(kind, str(path), name, PRINTER, 1)
    staged = Staged(str(path), "ab" * 32, 1234, request.filename)
    return Offer(request, staged, inspect(str(path)), tuple(plates), tuple(proposed), action)


# -- what the window shows ---------------------------------------------------------------------------------------------


def test_the_window_shows_the_staged_file_and_escapes_every_text(tmp_path):
    html = render(_offer(tmp_path), lambda key: key, target_name="Printer <1>")
    assert "Cube&lt;b&gt;" in html and "Printer &lt;1&gt;" in html and "<b>" not in html.split("<script")[0]
    assert "abababababab" in html  # the copy's hash, shortened


def test_a_print_preselects_the_queue_and_a_send_the_library(tmp_path):
    printed = render(_offer(tmp_path), lambda key: key, target_name="P")
    sent = render(_offer(tmp_path, kind="send", proposed=(), action="library_only"), lambda key: key, target_name="P")
    assert 'value="queue_asap" checked' in printed
    assert 'value="library_only" checked' in sent and 'value="queue_asap" checked' not in sent


def test_only_the_proposed_plates_are_ticked(tmp_path):
    html = render(_offer(tmp_path, plates=(1, 2), proposed=()), lambda key: key, target_name="P")
    assert 'data-plate="1" checked' not in html and 'data-plate="2" checked' not in html


# -- what it accepts ---------------------------------------------------------------------------------------------------


def test_an_answer_becomes_a_choice(tmp_path):
    offer = _offer(tmp_path)
    data = {"action": "queue_manual", "plates": [2], "copies": 3, "filament": "external_only"}
    assert parse(data, offer) == Choice("queue_manual", (2,), 3, {"mode": "external_only"})


@pytest.mark.parametrize(
    "data",
    [
        None,
        "queue_asap",
        {"action": "erase", "plates": [1]},
        {"action": "queue_asap", "plates": ["1"]},
        {"action": "queue_asap", "plates": [1], "copies": True},
        {"action": "queue_asap", "plates": [1], "copies": 0},
        {"action": "queue_asap", "plates": [1], "filament": "pinned"},
    ],
)
def test_an_answer_the_window_could_not_have_sent_is_no_choice(tmp_path, data):
    assert parse(data, _offer(tmp_path)) is None


# -- how the answer is waited for (§4.3) -------------------------------------------------------------------------------


def test_the_first_answer_wins_and_a_late_callback_changes_nothing():
    answer = Answer(LoadContext())
    answer.on_submit({"action": "library_only"})
    answer.on_close()
    assert answer.wait(threading.Event(), poll=0.01) == ("submitted", {"action": "library_only"})


def test_a_callback_after_the_fence_does_nothing():
    context = LoadContext()
    answer = Answer(context)
    context.close()
    answer.on_submit({"action": "library_only"})
    answer.wake("unload_fence")
    assert answer.wait(threading.Event(), poll=0.01) == ("fence", None)


def test_a_cancel_decides_while_the_window_is_still_open():
    answer = Answer(LoadContext())
    cancel = threading.Event()
    threading.Timer(0.05, cancel.set).start()
    started = time.monotonic()
    assert answer.wait(cancel, poll=0.01) == ("cancelled", None)
    assert time.monotonic() - started < 1.0
    answer.on_submit({"action": "library_only"})  # Orca's on_close after a Cancel comes late: ignored
    assert answer.wait(cancel, poll=0.01) == ("cancelled", None)


class Windows:
    """``orca.host.ui.create_window`` as rc builds it: deferred, the handle returned at once (WebDialog.cpp)."""

    def __init__(self):
        self.opened, self.closed = [], []

    def create_window(self, html, title="OrcaSlicer", width=820, height=600, on_message=None, on_close=None, style=0,
                      on_submit=None):  # fmt: skip
        window = type("UiWindow", (), {})()
        window.close = lambda: self.closed.append(window)
        window.is_open = lambda: window not in self.closed
        self.opened.append({"window": window, "style": style, "on_submit": on_submit, "on_close": on_close})
        return window


class Runtime:
    def __init__(self):
        self.context = LoadContext()
        self.waiters = set()

    def add_waiter(self, waiter):
        if self.context.closed:
            return False
        self.waiters.add(waiter)
        return True

    def remove_waiter(self, waiter):
        self.waiters.discard(waiter)


def test_ask_opens_a_modal_window_and_returns_the_submitted_choice(tmp_path):
    windows, runtime = Windows(), Runtime()
    offer = _offer(tmp_path)

    def person():
        while not windows.opened:
            time.sleep(0.005)
        windows.opened[0]["on_submit"]({"action": "queue_asap", "plates": [1, 2], "copies": 1, "filament": "auto"})

    threading.Thread(target=person).start()
    choice = ask(offer, windows.create_window, 1, runtime, lambda key: key, threading.Event(), target_name="P")
    assert choice == Choice("queue_asap", (1, 2))
    assert windows.opened[0]["style"] == 1 and runtime.waiters == set()


def test_ask_closes_the_window_on_cancel_without_waiting_for_its_close(tmp_path):
    windows, runtime = Windows(), Runtime()
    cancel = threading.Event()
    threading.Timer(0.05, cancel.set).start()
    assert ask(_offer(tmp_path), windows.create_window, 1, runtime, lambda k: k, cancel, target_name="P") is None
    assert windows.closed == [windows.opened[0]["window"]]


def test_a_window_the_host_cannot_create_is_no_hang(tmp_path):
    runtime = Runtime()

    def refuse(*args, **kwargs):
        raise RuntimeError("OrcaSlicer application is not initialized")

    done = []
    worker = threading.Thread(
        target=lambda: done.append(
            pytest.raises(
                RuntimeError, ask, _offer(tmp_path), refuse, 1, runtime, lambda k: k, threading.Event(), target_name="P"
            )  # fmt: skip
        )
    )
    worker.start()
    worker.join(2)
    assert done and runtime.waiters == set()


def test_ask_after_the_fence_opens_nothing(tmp_path):
    windows, runtime = Windows(), Runtime()
    runtime.context.close()
    assert (
        ask(_offer(tmp_path), windows.create_window, 1, runtime, lambda k: k, threading.Event(), target_name="P")
        is None
    )
    assert windows.opened == []


def test_orcas_slots_are_offered_only_when_orca_named_them(tmp_path):
    offer = _offer(tmp_path, plates=(1,), proposed=(1,))
    pinned = Offer(Request("print", offer.request.source, "Cube", PRINTER, 1, pins=(2, -1)), *_rest(offer))
    assert 'value="pinned"' in render(pinned, lambda k: k, target_name="P")
    assert 'value="pinned"' not in render(offer, lambda k: k, target_name="P")
    data = {"action": "queue_asap", "plates": [1], "copies": 1, "filament": "pinned"}
    assert parse(data, pinned).filament == {"mode": "pinned", "ams_mapping": [2, -1]}
    assert parse(data, offer) is None


def _rest(offer):
    return offer.staged, offer.inspection, offer.plates, offer.proposed, offer.default_action
