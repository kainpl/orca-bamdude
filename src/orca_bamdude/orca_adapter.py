"""The only module that imports ``orca``: registration, the permission request, the two capabilities.

Host spec r2.3.2 §4–§5. Everything here is a thin delegation to host-independent modules; a decision lives there.

Import and ``register_capabilities()`` run without the plugin's audit context, so this module reads its install
facts, the stored base and its own state from disk at import (run 1), writes the load marker (§4.1 ⟨R23-B2⟩) and
builds the one permission request. ``on_load`` is audited: storage is taken there and the marker completed. Names
follow what the probe proved on the real host (RF-1): the agent capability is ``BamDudePrinter`` and ``AgentInfo.id``
equals it, so a preset's ``printer_agent`` finds it; the tab is a separate capability named ``BamDude``.
"""

from __future__ import annotations

import encodings.cp437  # noqa: F401 -- lazy stdlib codecs are imported at load, not under audit later (§4.3)
import encodings.idna  # noqa: F401
import json
import os
import uuid
from pathlib import PurePath

import orca

from . import __version__, choice_window, jobs, locking, permits, submit
from .config import ConfigError
from .journal import Journal
from .service import Runtime
from .settings import apply_edit, effective, parse_base, user_targets

AGENT_CAPABILITY = "BamDudePrinter"
AGENT_NAME = "BamDude"
PAGE_CAPABILITY = "BamDude"
RELAY_ORIGIN = "http://127.0.0.1"
OK, ERR, ERR_NOT_SUPPORTED = 0, -1, -7010
NOTIFICATION_LEVELS = {"Regular": "RegularNotificationLevel", "Warning": "WarningNotificationLevel"}


def install_facts(module_file: str) -> dict:
    """Where OrcaSlicer unpacked us: ``<data_dir>/orca_plugins/<key>.whl/__whl_extracted__/<package>/``.

    The plugin key is the install folder without ``.whl``; an unexpected layout leaves it unknown, never guessed.
    """
    parts = PurePath(os.path.abspath(module_file)).parts
    facts = {"data_dir": None, "plugin_key": None}
    if "__whl_extracted__" not in parts:
        return facts
    index = len(parts) - 1 - parts[::-1].index("__whl_extracted__")
    if index < 3 or parts[index - 2] != "orca_plugins":
        return facts
    folder = parts[index - 1]
    facts["data_dir"] = str(PurePath(*parts[: index - 2]))
    facts["plugin_key"] = folder[:-4] if folder.lower().endswith(".whl") else folder
    return facts


def storage_path(facts: dict) -> str | None:
    if not facts["data_dir"] or not facts["plugin_key"]:
        return None
    return os.path.join(facts["data_dir"], "orca_plugins", "plugin_data", facts["plugin_key"])


def read_base(facts: dict) -> dict | None:
    """The agent's base entry in ``orca_plugins/config.json``, read as a file (no API exists before ``on_load``)."""
    if not facts["data_dir"] or not facts["plugin_key"]:
        return None
    try:
        # OrcaSlicer (or a second one closing down) may be replacing the file: a sharing violation is waited out.
        root = json.loads(locking.read_text(os.path.join(facts["data_dir"], "orca_plugins", "config.json")))
    except (OSError, ValueError):
        return None
    entries = root.get("config") if isinstance(root, dict) else None
    for entry in entries if isinstance(entries, list) else []:
        if (
            isinstance(entry, dict)
            and entry.get("plugin_key") == facts["plugin_key"]
            and entry.get("capability") == AGENT_CAPABILITY
        ):
            return _object(entry.get("cap_config"))
    return None


def _object(stored):
    """A config value stored as an object or as a JSON string; anything else is not an object."""
    if isinstance(stored, str):
        try:
            stored = json.loads(stored) if stored.strip() else {}
        except ValueError:
            return "not_an_object"
    return stored


def temp_model_tree() -> str:
    """OrcaSlicer's per-user temp model tree (``per_user_temp_dir`` + ``orcaslicer_model``), as P0 confirmed."""
    names, fallback = ("TMP", "TEMP", "USERPROFILE"), os.environ.get("WINDIR", "C:\\Windows")
    if os.name != "nt":
        names, fallback = ("TMPDIR", "TMP", "TEMP"), "/tmp"
    root = next((os.environ[n] for n in names if os.environ.get(n)), fallback).rstrip("/\\")
    if os.name != "nt":
        root = os.path.join(root, f"orcaslicer_{os.getuid()}")
    return os.path.join(root, "orcaslicer_model")


def permission_request(user: set[str]) -> dict:
    """The one request: the fixed targets, always, plus the user targets this load may ask for."""
    network = {RELAY_ORIGIN} | {t.split(":", 1)[1] for t in user if t.startswith("network:")}
    fs_read = [os.path.join(temp_model_tree(), "*")]
    fs_read += sorted(os.path.join(t.split(":", 1)[1], "*") for t in user if t.startswith("fs_read:"))
    process = sorted(t.split(":", 1)[1] for t in user if t.startswith("process:"))
    return {"threading": True, "fs_read": fs_read, "network": sorted(network), "process": process}


def _begin(facts: dict, storage: str | None, load_id: str) -> tuple[set[str], dict]:
    base = parse_base(read_base(facts) or {})
    current = user_targets(base, effective(base, None))
    if storage is None:
        return current, {"previous_load": None, "held_now": []}
    out: dict = {}

    def change(state):
        state, out["requested"], out["decision"] = permits.begin_load(state, load_id, current)
        return state

    try:
        # The one deliberate repair of an unreadable state: its holds are lost, so at worst one refusal repeats; every
        # later write refuses such a state instead (H1 fix3 review F1-R2).
        permits.update_state(storage, change, repair=True, on_repair=lambda: out.update(repaired=True))
    except OSError:
        return current, {"previous_load": None, "held_now": [], "state_error": True}
    if out.get("repaired"):
        out["decision"]["state_repaired"] = True
    return out["requested"], out["decision"]


def _push(level, text, hyper_text, on_click):
    ui = orca.host.ui
    value = getattr(ui.NotificationLevel, NOTIFICATION_LEVELS.get(level, level))
    ui.push_notification(value, text, hyper_text, on_click)


INSTALL = install_facts(__file__)
LOAD_ID = uuid.uuid4().hex
_STORAGE = storage_path(INSTALL)
_REQUESTED, _DECISION = _begin(INSTALL, _STORAGE, LOAD_ID)
REQUEST = permission_request(_REQUESTED)
RUNTIME = Runtime(_push, _STORAGE, LOAD_ID, requested=_REQUESTED, decision=_DECISION)
_capabilities: dict = {}


def _preset_override():
    """The edited printer preset's override for our agent: ``(override, preset name, error)``.

    Three states, never confused (H1 review R1): no override (``None`` with no error) -- base applies; an override
    read; or an override that could not be read -- an unreadable or malformed envelope, a preset that cannot be read,
    an entry of ours whose config is not an object. The last refuses the connection: falling back to base could send
    the key to the global server when the preset meant another one. Called on the UI thread only (R3): the preset
    bundle is OrcaSlicer's live object.
    """
    try:
        preset = orca.host.preset_bundle().printers.get_edited_preset()
        raw = preset.config_value("printer_plugin_config_overrides")
        name = getattr(preset, "name", None)
    except Exception:  # noqa: BLE001 -- we cannot tell whether an override exists
        return None, None, "unreadable"
    if raw is None or raw == "":
        return None, name, None
    if not isinstance(raw, str):
        return None, name, "unreadable"
    try:
        entries = json.loads(raw)
    except ValueError:
        return None, name, "unreadable"
    if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
        return None, name, "unreadable"
    for entry in entries:
        if entry.get("capability") == AGENT_CAPABILITY and entry.get("plugin_key") == INSTALL["plugin_key"]:
            config = _object(entry.get("cap_config"))
            if not isinstance(config, dict):
                return None, name, "not_an_object"
            return config, name, None
    return None, name, None


def refresh_settings():
    """Read base, the preset's override and the UI language, and freeze them. UI thread only (§4.3, H1 review R3)."""
    try:
        info = orca.host.app_info()
        RUNTIME.set_language(info.get("language"))
        RUNTIME.set_host_version(info.get("version"))
    except Exception:  # noqa: BLE001 -- English is said instead, and nothing is sent
        pass
    override, preset_name, error = _preset_override()
    return RUNTIME.apply_settings(read_base(INSTALL) or {}, override, preset_name, override_error=error)


def _reason(outcome: submit.Outcome) -> str:
    """The refusal in words: the server's own sentence when it gave one, else the plugin's for the code."""
    if outcome.message:
        return outcome.message
    text = RUNTIME.say(f"reason.{outcome.code}")
    return text if text != f"reason.{outcome.code}" else RUNTIME.say("reason.generic").replace("{code}", outcome.code)


def announce(outcome: submit.Outcome, name: str, target_name: str) -> None:
    """The send's own notification (§6.2 step 11, §9.4): Orca's dialog says nothing true about a farm."""
    if outcome.result == "accepted" and outcome.code in ("queued", "library"):
        key, level = f"send.{outcome.code}", "Regular"
    elif outcome.result == "accepted":
        key, level = "send.partial", "Warning"
    elif outcome.result == "cancelled":
        key, level = "send.cancelled", "Regular"
    elif outcome.result == "unknown":
        key, level = "send.unknown", "Warning"
    else:
        key, level = "send.refused", "Warning"
    text = RUNTIME.say(key).replace("{file}", name).replace("{target}", target_name)
    RUNTIME.notify(level, text.replace("{reason}", _reason(outcome)))


def send_job(kind: str, params, update_fn, cancel_fn) -> int:
    """One of Orca's Print or Send, on the host's job thread: everything that reads, copies or asks the farm runs on
    a plugin thread (``jobs``); this thread decides only what is known now and polls (spec r2 §4.3, §6.2)."""
    name = str(getattr(params, "project_name", "") or "plate")
    token = RUNTIME.admit("send")
    if token is None:
        return ERR  # the plugin is unloading
    context = RUNTIME.fleet.job_context(str(getattr(params, "dev_id", "")))
    refusal = None
    if isinstance(context, str):
        refusal = submit.Outcome("refused", context)
    elif RUNTIME.storage is None:
        refusal = submit.Outcome("refused", "disk_error")
    options, warnings = {}, ()
    if refusal is None and kind == "print":
        try:
            options, warnings = submit.options_from_params(params)
        except submit.OptionError as exc:
            message = RUNTIME.say("reason.option").replace("{option}", RUNTIME.say(f"option.{exc.option}"))
            refusal = submit.Outcome("refused", "option", message)
    if refusal is not None:
        RUNTIME.release(token)
        announce(refusal, name, "")
        return jobs.FAILED
    device, client, capabilities = context
    target = submit.Target.printer(device.printer_id, device.identity)
    source, plate_index = str(getattr(params, "filename", "")), int(getattr(params, "plate_index", 0) or 0)
    if kind == "print":
        request = submit.Request.native_print(
            source, name, target, plate_index=plate_index, options=options, warnings=warnings
        )
    else:
        request = submit.Request.native_send(source, name, target, plate_index=plate_index)
    storage = RUNTIME.storage

    def work(cancel, report):
        try:
            outcome = submit.send(
                request,
                client=client,
                capabilities=capabilities,
                journal=Journal(storage, capabilities.namespace),
                staging_dir=os.path.join(storage, "staging"),
                gate=RUNTIME.upload_gate,
                cancel=cancel,
                choose=lambda offer: choice_window.ask(
                    offer,
                    orca.host.ui.create_window,
                    orca.host.ui.WINDOW_MODAL,
                    RUNTIME,
                    RUNTIME.say,
                    cancel,
                    target_name=device.name,
                ),  # fmt: skip
                host_writable=RUNTIME.host_writable,
                progress=report,
            )
            announce(outcome, name, device.name)
            return outcome
        finally:
            RUNTIME.release(token)

    return jobs.run_for_host(jobs.Job(work), update_fn, cancel_fn, RUNTIME.say)


def _ui_entry():
    """A UI entry of the agent: the snapshot is taken here and the fleet's thread starts at the first one."""
    refresh_settings()
    RUNTIME.start_fleet()


def _take_storage() -> None:
    """``on_load`` is audited: the host's storage folder is taken once here (RF-8)."""
    try:
        storage = orca.host.plugin.storage()
    except Exception:  # noqa: BLE001 -- without storage the plugin keeps no state, it does not stop
        return
    RUNTIME.adopt_storage(storage)


_CONFIG_UI = """<!doctype html><meta charset="utf-8"><body><p id="msg"></p><script>
var scope = (window.orca && window.orca.getContext) ? window.orca.getContext().scope : "global";
document.getElementById("msg").textContent = scope === "preset"
  ? "The BamDude connection for this preset is set in the BamDude tab."
  : "BamDude connection settings are in the BamDude tab.";
</script></body>"""


class BamDudeAgent(orca.printer_agent.PrinterAgentBase):
    """The printer agent: discovery, connection, reports and the camera through the Runtime's fleet (§5); jobs come
    in H3. Every method returns at once on the UI thread -- what needs the farm is answered by the fleet's thread
    through the callbacks the host handed over."""

    def __init__(self):
        super().__init__()
        self._selected = ""

    def get_name(self):
        return AGENT_CAPABILITY

    def get_agent_info(self):
        return orca.printer_agent.AgentInfo(AGENT_CAPABILITY, AGENT_NAME, __version__, "Send plates to a BamDude farm")

    def get_default_config(self):
        return {}

    def has_config_ui(self):
        return True

    def get_config_ui(self):
        return _CONFIG_UI

    def on_load(self):
        # OrcaSlicer's load thread, not the UI thread: no preset is read here; the first UI entry takes the snapshot,
        # and until then there is no connection (H1 review R3).
        _capabilities["agent"] = self
        RUNTIME.attach("agent")
        _take_storage()
        RUNTIME.loaded()

    def on_unload(self):
        _capabilities.pop("agent", None)
        RUNTIME.detach("agent")

    def on_lifecycle_event(self, event, ctx):
        if str(getattr(event, "name", event)).endswith(("PresetSelected", "PresetSaved")):
            refresh_settings()

    # -- devices (§5.1-§5.3) ---------------------------------------------------------------------------------------
    def connect_printer(self, params):
        """UI thread (``DeviceManager::set_selected_machine``): the answer follows as ``local_connect``."""
        _ui_entry()
        return RUNTIME.fleet.connect(getattr(params, "dev_id", "") or "", getattr(params, "password", "") or "")

    def disconnect_printer(self):
        return RUNTIME.fleet.disconnect()

    def send_message(self, dev_id, json_str, qos, flag):
        return RUNTIME.fleet.command(dev_id, json_str)

    def send_message_to_printer(self, dev_id, json_str, qos, flag):
        return RUNTIME.fleet.command(dev_id, json_str)

    def start_discovery(self, start, sending):
        if start:  # UI thread, after an agent switch: the first point a snapshot may be taken
            _ui_entry()
            RUNTIME.fleet.discover()
        return True

    def get_camera_stream_mode(self):
        return orca.printer_agent.CameraStreamMode.HTTP

    def get_camera_url(self):
        return RUNTIME.fleet.camera_url()

    def bind_detect(self, dev_ip, sec_link, detect):
        return ERR_NOT_SUPPORTED

    def get_user_selected_machine(self):
        return self._selected

    def set_user_selected_machine(self, dev_id):
        if dev_id:  # the host also calls it with "" every few seconds; that is not a deselect (P0)
            self._selected = dev_id
        return OK

    def start_send_gcode_to_sdcard(self, params, update_fn, cancel_fn, wait_fn):
        if getattr(params, "project_name", "") == "verify_job":  # the access-code check: no I/O (§6.1)
            return OK
        return send_job("send", params, update_fn, cancel_fn)

    def start_local_print(self, params, update_fn, cancel_fn):
        """Orca's Print to a LAN printer (rc PrintJob::process, after verify_job) -- the path BamDude's devices take."""
        return send_job("print", params, update_fn, cancel_fn)

    def start_print(self, params, update_fn, cancel_fn, wait_fn):
        return ERR

    def start_local_print_with_record(self, params, update_fn, cancel_fn, wait_fn):
        return ERR

    def start_sdcard_print(self, params, update_fn, cancel_fn):
        return ERR_NOT_SUPPORTED

    def check_cert(self):
        return OK

    def ping_bind(self, ping_code):
        return ERR_NOT_SUPPORTED

    def bind(self, dev_ip, dev_id, dev_model, sec_link, timezone, improved, update_fn):
        return ERR_NOT_SUPPORTED

    def unbind(self, dev_id):
        return ERR_NOT_SUPPORTED

    # -- the host's callbacks: kept, called by the fleet from its thread (the host re-posts each) ---------------------
    def set_server_callback(self, fn):
        RUNTIME.callbacks.set("server_error", fn)
        return OK

    def set_on_ssdp_msg_fn(self, fn):
        RUNTIME.callbacks.set("ssdp", fn)
        return OK

    def set_on_printer_connected_fn(self, fn):
        RUNTIME.callbacks.set("printer_connected", fn)
        return OK

    def set_on_subscribe_failure_fn(self, fn):
        RUNTIME.callbacks.set("subscribe_failure", fn)
        return OK

    def set_on_message_fn(self, fn):
        RUNTIME.callbacks.set("message", fn)
        return OK

    def set_on_user_message_fn(self, fn):
        RUNTIME.callbacks.set("user_message", fn)
        return OK

    def set_on_local_connect_fn(self, fn):
        RUNTIME.callbacks.set("local_connect", fn)
        return OK

    def set_on_local_message_fn(self, fn):
        RUNTIME.callbacks.set("local_message", fn)
        return OK

    def set_queue_on_main_fn(self, fn):
        RUNTIME.callbacks.set("queue_on_main", fn)
        return OK


_PAGE_HTML = """<!doctype html><meta charset="utf-8"><title>BamDude</title><body><p>BamDude</p></body>"""


class BamDudePage(orca.pages.PagesPluginCapabilityBase):
    """The tab. ``on_message`` runs on the UI thread and answers at once (§4.3); its HTML comes in H4."""

    def get_name(self):
        return PAGE_CAPABILITY

    def get_ui(self):
        return _PAGE_HTML

    def get_default_config(self):
        return {}

    def has_config_ui(self):
        return False

    def on_load(self):
        _capabilities["page"] = self
        RUNTIME.attach("page")

    def on_unload(self):
        _capabilities.pop("page", None)
        RUNTIME.detach("page")

    def _post(self, kind: str, **data):
        try:
            self.post_message({"kind": kind, **data})
        except Exception:  # noqa: BLE001 -- a page that is gone loses the message (§4.3)
            pass

    def on_message(self, data):
        """UI thread: read, decide, queue -- never wait for the state file (H1 review R4). An explicit retry is
        reported as recorded only once the state writer has written it."""
        op = data.get("op") if isinstance(data, dict) else None
        if op == "state":
            refresh_settings()
            self._post("state", **RUNTIME.view())
        elif op == "ask_again":
            self._ask_again(saved=False)
        elif op == "save_settings":
            self._save_settings(data.get("edit"))

    def _ask_again(self, saved: bool, targets=None) -> None:
        def recorded(ok: bool) -> None:  # the state writer's thread; posting to the page is safe from any thread
            self._post("state", **RUNTIME.view(), saved=saved, recorded=ok)

        if targets is None:
            if RUNTIME.holds_everything():
                # Everything is held: only the current settings come back, through the bounded release list; every
                # other refusal stays held, pending or not (H1 fix review F2). Clearing every hold at once would
                # need its own agreed action.
                refresh_settings()
                snapshot = RUNTIME.snapshot
                targets = sorted(set(snapshot.slots.values())) if snapshot is not None else []
            else:
                targets = sorted(RUNTIME.held())
        accepted = RUNTIME.ask_again(targets, done=recorded)
        if not accepted:
            self._post("error", op="ask_again", code="stopping")

    def _save_settings(self, edit) -> None:
        agent = _capabilities.get("agent")
        try:
            stored = read_base(INSTALL) or {}
            new = apply_edit(stored if isinstance(stored, dict) else {}, edit)
        except ConfigError as exc:
            self._post("error", op="save_settings", code=exc.code)
            return
        if agent is None or not agent.save_config(json.dumps(new)):
            self._post("error", op="save_settings", code="save_failed")
            return
        # Saving in the tab is an explicit action: the targets of the saved base are asked for at the next start
        # even if an unfinished load held them (§4.1 ⟨R23.1-A2⟩).
        base = parse_base(new)
        refresh_settings()
        self._ask_again(saved=True, targets=sorted(user_targets(base, effective(base, None))))


@orca.plugin
class BamDudePlugin(orca.base):
    def register_capabilities(self):
        orca.request_permissions(**REQUEST)
        orca.register_capability(BamDudeAgent)
        orca.register_capability(BamDudePage)
