"""The window in which the person chooses what a send does (spec r2 §6.3), and how its answer is waited for (§4.3).

It shows the plugin's staged copy -- its name, size and hash -- with its plates, the target, the action, copies and
filament, and the options Orca's dialog chose. A Print offers the queue first; a Send and the tab offer the library
first, and their plates start unticked, so a queue job is always an explicit choice.

The window is OrcaSlicer's (``orca.host.ui.create_window``, modal): created by ``CallAfter`` and returned at once,
its callbacks run on the UI thread with no audit context (rc ``WebDialog.cpp``, ``PluginHostUi.cpp``). So they only
put the answer in an :class:`Answer` and signal -- the first answer wins, a later one (Orca's ``on_close`` after a
Cancel comes after ``start_*`` returned) changes nothing, and after the load's fence none is taken. The waiting is
the plugin thread's: it polls the job's cancel and the fence, and on either closes the window and returns at once,
never waiting for its ``on_close``.
"""

from __future__ import annotations

import html
import threading

from .submit import ACTIONS, Choice, Offer

FILAMENTS = ("auto", "external_only")
_FIRST = {"print": ("queue_asap", "queue_manual", "library_only")}
_OTHERS = ("library_only", "queue_manual", "queue_asap")


class Answer:
    """One window's answer: written by its callbacks, read by the waiting plugin thread."""

    def __init__(self, context):
        self._context = context
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._decided: tuple[str, object] | None = None

    def _decide(self, kind: str, data=None, *, from_host: bool = False) -> None:
        with self._lock:
            if self._decided is not None or (from_host and self._context.closed):
                return
            self._decided = (kind, data)
        self._event.set()

    def on_submit(self, data) -> None:
        self._decide("submitted", data, from_host=True)

    def on_close(self) -> None:
        self._decide("closed", from_host=True)

    def wake(self, _reason: str) -> None:
        """The Runtime's fence (§4.5): even a window the host destroyed without ``on_close`` stops being waited for."""
        self._decide("fence")

    def wait(self, cancel: threading.Event, poll: float = 0.1) -> tuple[str, object]:
        while not self._event.wait(poll):
            if self._context.closed:
                self._decide("fence")
            elif cancel.is_set():
                self._decide("cancelled")
        with self._lock:
            return self._decided


def parse(data, offer: Offer) -> Choice | None:
    """The page's answer as a :class:`Choice`, or ``None`` for anything the window could not have sent."""
    if not isinstance(data, dict) or data.get("action") not in ACTIONS:
        return None
    plates, copies, filament = data.get("plates", []), data.get("copies", 1), data.get("filament", "auto")
    if not isinstance(plates, list) or any(isinstance(p, bool) or not isinstance(p, int) for p in plates):
        return None
    if any(plate not in offer.plates for plate in plates):
        return None
    if isinstance(copies, bool) or not isinstance(copies, int) or copies < 1:
        return None
    if filament == "pinned" and offer.request.pins is not None:
        return Choice(
            data["action"], tuple(plates), copies, {"mode": "pinned", "ams_mapping": list(offer.request.pins)}
        )
    if filament not in FILAMENTS:
        return None
    return Choice(data["action"], tuple(plates), copies, {"mode": filament})


def _size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


def render(offer: Offer, say, *, target_name: str) -> str:
    """The window's page. Every text is escaped; nothing the file or the server says becomes markup."""
    e = html.escape
    request = offer.request
    actions = _FIRST.get(request.kind, _OTHERS)
    rows = [
        f"<h1>{e(say('window.heading'))}</h1>",
        f"<p><span class=k>{e(say('window.file'))}</span> {e(request.name)}</p>",
        f"<p><span class=k>{e(say('window.size'))}</span> {e(_size(offer.staged.size))} · "
        f"<span class=k>{e(say('window.hash'))}</span> <code>{e(offer.staged.sha256[:12])}</code></p>",
        f"<p><span class=k>{e(say('window.target'))}</span> {e(target_name)}</p>",
        f"<fieldset><legend>{e(say('window.plates'))}</legend>",
    ]
    for plate in offer.plates:
        ticked = " checked" if plate in offer.proposed else ""
        label = e(say("window.plate").replace("{n}", str(plate)))
        rows.append(f'<label><input type="checkbox" data-plate="{plate}"{ticked}> {label}</label>')
    rows.append(f"</fieldset><fieldset><legend>{e(say('window.action'))}</legend>")
    for action in actions:
        ticked = " checked" if action == offer.default_action else ""
        rows.append(
            f'<label><input type="radio" name="action" value="{action}"{ticked}> {e(say("action." + action))}</label>'
        )
    rows.append(
        f"</fieldset><p><label>{e(say('window.copies'))} "
        f'<input type="number" id="copies" min="1" max="999" value="1"></label></p>'
        f"<fieldset><legend>{e(say('window.filament'))}</legend>"
    )
    modes = FILAMENTS + (("pinned",) if request.pins is not None else ())  # only what Orca's dialog named
    for mode in modes:
        ticked = " checked" if mode == "auto" else ""
        rows.append(
            f'<label><input type="radio" name="filament" value="{mode}"{ticked}> {e(say("filament." + mode))}</label>'
        )
    rows.append("</fieldset>")
    if request.options:
        shown = ", ".join(f"{say('option.' + name)}: {say('value.' + str(value).lower())}" for name, value in
                          request.options.items())  # fmt: skip
        rows.append(f"<p><span class=k>{e(say('window.options'))}</span> {e(shown)}</p>")
    for warning in request.warnings:
        rows.append(f'<p class="warn">{e(say("warning." + warning))}</p>')
    rows.append(
        f'<p class="buttons"><button id="cancel">{e(say("window.cancel"))}</button> '
        f'<button id="send" class="primary">{e(say("window.send"))}</button></p>'
    )
    return _PAGE.replace("{BODY}", "\n".join(rows))


_PAGE = """<!doctype html><html lang="en"><meta charset="utf-8">
<style>
body{font:13px system-ui,sans-serif;margin:16px;color:#222;background:#fff}
@media (prefers-color-scheme:dark){body{color:#ddd;background:#2b2b2b}}
h1{font-size:16px;margin:0 0 12px} .k{opacity:.7} fieldset{border:1px solid #8884;margin:10px 0;padding:6px 10px}
label{display:block;margin:3px 0} .warn{color:#b26b00} .buttons{text-align:right;margin-top:16px}
button{padding:5px 14px;margin-left:8px} .primary{font-weight:600}
</style>
<body>{BODY}
<script>
document.getElementById("cancel").onclick = function () { window.orca.close(); };
document.getElementById("send").onclick = function () {
  var plates = [];
  document.querySelectorAll("input[data-plate]").forEach(function (box) {
    if (box.checked) { plates.push(parseInt(box.getAttribute("data-plate"), 10)); }
  });
  var picked = function (name) { var c = document.querySelector("input[name=" + name + "]:checked"); return c ? c.value : ""; };
  window.orca.submit({action: picked("action"), plates: plates,
    copies: parseInt(document.getElementById("copies").value, 10) || 1, filament: picked("filament")});
};
</script>
</body></html>
"""


def ask(offer: Offer, create_window, modal_style, runtime, say, cancel: threading.Event, *, target_name: str):
    """Show the window and wait for the person (plugin thread). ``None`` for a close, a cancel or the fence."""
    if runtime.context.closed:
        return None
    answer = Answer(runtime.context)
    if not runtime.add_waiter(answer):
        return None
    window, kind = None, None
    try:
        window = create_window(
            render(offer, say, target_name=target_name),
            say("window.title"),
            640,
            620,
            on_close=answer.on_close,
            style=modal_style,
            on_submit=answer.on_submit,
        )
        kind, data = answer.wait(cancel)
        return parse(data, offer) if kind == "submitted" else None
    finally:
        runtime.remove_waiter(answer)
        if window is not None and kind != "submitted":
            try:
                window.close()  # deferred by the host (CallAfter): never waits for the UI thread
            except Exception:  # noqa: BLE001 -- a window the host already destroyed
                pass
