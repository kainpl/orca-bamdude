"""A stand-in for OrcaSlicer 2.5.0-rc's ``orca`` module and its plugin loader, built from what is proven.

Sources: OrcaSlicer at tag ``v2.5.0-rc`` (read through ``git show``) and the H-preflight runs 1 and 2 on the real
host (host spec r2.3.2, analysis RF-1…RF-16). What it models:

- **Install layout.** The wheel is unpacked to ``<data_dir>/orca_plugins/<key>.whl/__whl_extracted__/<package>``;
  config lives in ``<data_dir>/orca_plugins/config.json``, storage in ``<data_dir>/orca_plugins/plugin_data/<key>``.
- **Registration.** Import and ``register_capabilities()`` run in the discovery context without an audit context.
  ``request_permissions`` works only there; one combined prompt asks for what is not granted yet. A refusal discards
  the load: no capability is created, the plugin is marked disabled, and later loads do not import it until
  Activate (RF-2).
- **Audit context.** ``plugin.storage()`` works only inside an audited capability call (``on_load``, agent methods,
  the page's ``on_message``) and raises in window and notification callbacks (RF-8).
- **Threads.** ``app_info()`` only on the UI thread. ``push_notification`` blocks until the UI thread runs it and gives
  up only while OrcaSlicer is closing (RF-6).
- **Unload.** The host disables the plugin's registered UI callbacks *before* Python ``on_unload`` and keeps
  accepting new registrations (⟨R23.1-A1⟩), then removes the plugin's modules, so Activate imports them again in the
  same process (RF-2).
- **Presets.** The edited printer preset carries ``printer_plugin_config_overrides`` (a JSON list); the override for
  a capability wins wholesale over the base entry (RF-1).
- **Agent callbacks.** Selecting the agent sets its ``set_on_*_fn`` callbacks on the UI thread, then calls
  ``start_discovery(True, False)`` (rc ``NetworkAgent::apply_printer_callbacks``, ``GUI_App::switch_printer_agent``).
  Each callback re-posts to the UI thread with ``CallAfter`` and returns at once (``GUI_App::init_networking_callbacks``);
  the SSDP one is dropped unless a ``queue_on_main`` function was set (``NetworkAgent::apply_discovery_callback``). A
  failed ``local_connect`` shows its message as it is: "Connect <name> failed! [SN:<dev_id>, code=<msg>]" (RF-4).
"""

from __future__ import annotations

import enum
import json
import queue
import shutil
import sys
import threading
import time
import types
from pathlib import Path

SRC_PACKAGE = Path(__file__).resolve().parents[1] / "src" / "orca_bamdude"
PACKAGE = "orca_bamdude"
WHEEL_KEY = "orca_bamdude_any"

_local = threading.local()


class NotificationLevel(enum.Enum):
    RegularNotificationLevel = 1
    ImportantNotificationLevel = 2
    WarningNotificationLevel = 3
    ErrorNotificationLevel = 4


class CameraStreamMode(enum.Enum):
    None_ = 0
    HTTP = 1
    HTTPS = 2
    HTTP_SNAPSHOT = 3
    RTSP = 4


class PrinterConnectionParams:
    """What ``MachineObject::connect`` hands ``connect_printer``: the password is the access code the user typed."""

    def __init__(self, dev_id="", host="", port="", username="", password="", use_ssl=False, ca_file=""):
        self.dev_id, self.host, self.port, self.username = dev_id, host, port, username
        self.password, self.use_ssl, self.ca_file = password, use_ssl, ca_file


AGENT_CALLBACK_SETTERS = (
    "set_queue_on_main_fn",
    "set_on_ssdp_msg_fn",
    "set_on_printer_connected_fn",
    "set_on_subscribe_failure_fn",
    "set_on_message_fn",
    "set_on_user_message_fn",
    "set_on_local_connect_fn",
    "set_on_local_message_fn",
    "set_server_callback",
)

PENDING, CLAIMED, ABANDONED = "pending", "claimed", "abandoned"


class UiThread:
    """OrcaSlicer's UI thread: runs posted calls in order.

    A posted call is Pending until the UI thread claims it (compare-and-set, as ``run_on_ui_blocking`` does); a
    caller may abandon it only while it is still Pending, and an abandoned call never runs (H1 review R6).
    """

    def __init__(self):
        self._calls: queue.Queue = queue.Queue()
        self._state_lock = threading.Lock()
        self._pending = 0
        self.thread = threading.Thread(target=self._run, name="fake-ui", daemon=True)
        self.thread.start()

    def _run(self):
        _local.ui = True
        while True:
            fn, done = self._calls.get()
            if fn is None:
                return
            with self._state_lock:
                self._pending -= 1
                if done["state"] != PENDING:
                    continue
                done["state"] = CLAIMED
            try:
                done["result"] = fn()
            except BaseException as exc:  # noqa: BLE001 -- handed back to the caller
                done["error"] = exc
            done["event"].set()

    def post(self, fn) -> dict:
        done = {"event": threading.Event(), "state": PENDING}
        with self._state_lock:
            self._pending += 1
        self._calls.put((fn, done))
        return done

    def abandon(self, done: dict) -> bool:
        """The caller gives up -- possible only while the call is still Pending."""
        with self._state_lock:
            if done["state"] != PENDING:
                return False
            done["state"] = ABANDONED
            return True

    def wait_pending(self, count: int, timeout: float) -> bool:
        """For tests: wait until at least ``count`` calls are queued and not yet claimed."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._state_lock:
                if self._pending >= count:
                    return True
            time.sleep(0.005)
        return False

    def call(self, fn, timeout: float = 10.0):
        """Run ``fn`` on the UI thread and wait for it (the test thread stands in for wx's event loop driver)."""
        if getattr(_local, "ui", False):
            return fn()
        done = self.post(fn)
        if not done["event"].wait(timeout):
            raise TimeoutError("UI thread did not run the call")
        if "error" in done:
            raise done["error"]
        return done.get("result")

    def stop(self):
        self._calls.put((None, None))


class Host:
    """One OrcaSlicer process with one data dir and the plugin's install folder."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.plugins_dir = self.data_dir / "orca_plugins"
        self.extracted = self.plugins_dir / f"{WHEEL_KEY}.whl" / "__whl_extracted__"
        self.ui = UiThread()
        self.closing = False
        self.prompt_answer = True  # what the user answers to the next permission prompt
        self.prompts: list[dict] = []  # each prompt's missing targets
        self.requests: list[dict] = []  # every request_permissions call
        self.granted: dict[str, set] = {"network": set(), "fs_read": set(), "process": set(), "threading": set()}
        self.enabled = True
        self.loaded: dict | None = None  # {"module", "capabilities": {name: instance}}
        self.callbacks: list = []  # registered UI callbacks (notification on_click), each a dict with "active"
        self.notifications: list[dict] = []
        self.preset_name = "Bambu Lab X1 probe"
        self.preset_overrides: list[dict] = []
        self.preset_raw: str | None = None  # when set, the envelope string the preset returns as it is
        self.preset_error: Exception | None = None  # when set, the preset accessor raises it
        self.preset_reads: list[bool] = []  # one entry per preset read: whether it ran on the UI thread
        self.language = "en_US"  # app_info()["language"], as current_language_code_safe() reports it
        self.devices: dict[str, dict] = {}  # SSDP announcements the host accepted, by dev_id
        self.device_messages: list[tuple] = []  # (dev_id, message) from local_message
        self.connects: list[tuple] = []  # (status, dev_id, message) from local_connect
        self.dialogs: list[str] = []  # what a failed connect shows the user
        self.connected_topics: list[str] = []
        self.queue_on_main_set = False
        self._saved_modules: dict | None = None
        self.orca = self._build_module()

    # -- install, load, unload ---------------------------------------------------------------------------------------

    def install(self) -> None:
        target = self.extracted / PACKAGE
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(SRC_PACKAGE, target, ignore=shutil.ignore_patterns("__pycache__"))

    @property
    def plugin_key(self) -> str:
        return WHEEL_KEY

    @property
    def storage_dir(self) -> Path:
        return self.plugins_dir / "plugin_data" / WHEEL_KEY

    def write_base(self, capability: str, config: dict, as_string: bool = False) -> None:
        path = self.plugins_dir / "config.json"
        root = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"config": []}
        root["config"] = [
            e for e in root["config"] if not (e["plugin_key"] == WHEEL_KEY and e["capability"] == capability)
        ]
        root["config"].append(
            {
                "plugin_key": WHEEL_KEY,
                "capability": capability,
                "capability_type": "printer-connection",
                "plugin_version": "0.1.0",
                "cap_config": json.dumps(config) if as_string else config,
            }
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(root, indent="\t"), encoding="utf-8")

    def read_base(self, capability: str) -> dict | None:
        path = self.plugins_dir / "config.json"
        if not path.exists():
            return None
        for entry in json.loads(path.read_text(encoding="utf-8"))["config"]:
            if entry["plugin_key"] == WHEEL_KEY and entry["capability"] == capability:
                stored = entry["cap_config"]
                return json.loads(stored) if isinstance(stored, str) else stored
        return None

    def load(self) -> bool:
        """Import, register, prompt, materialize, ``on_load``. Returns whether the plugin loaded."""
        if not self.enabled:
            return False
        self._swap_in()
        self._discovery = {"plugin_cls": None, "capabilities": [], "requests": []}
        _local.discovery = self._discovery
        try:
            module = __import__(PACKAGE)
            plugin_cls = self._discovery["plugin_cls"]
            assert plugin_cls is not None, "the package registered no @orca.plugin class"
            plugin_cls().register_capabilities()
        finally:
            _local.discovery = None
        missing = self._missing(self._discovery["requests"])
        if any(missing.values()):
            self.prompts.append(missing)
            if not self.prompt_answer:
                self.enabled = False
                self._purge_modules()
                return False
            for bucket, targets in missing.items():
                self.granted[bucket] |= targets
        instances = {}
        for cls in self._discovery["capabilities"]:
            instance = cls()
            instances[instance.get_name()] = instance
        self.loaded = {"module": module, "capabilities": instances}
        for name, instance in instances.items():
            if hasattr(instance, "get_agent_info"):
                self.ui.call(instance.get_agent_info)
            self.audited(name, instance.on_load)
        return True

    def unload(self) -> None:
        """Disable: the host's teardown first (callbacks off), then Python ``on_unload`` on the UI thread."""
        assert self.loaded is not None
        for callback in self.callbacks:
            callback["active"] = False
        for name, instance in self.loaded["capabilities"].items():
            self.ui.call(lambda i=instance, n=name: self.audited(n, i.on_unload))
        self.loaded = None
        self._purge_modules()

    def activate(self) -> bool:
        self.enabled = True
        return self.load()

    def close(self) -> None:
        if self.loaded is not None:
            self.unload()
        self.ui.stop()
        self._restore_modules()

    def capability(self, name: str):
        return self.loaded["capabilities"][name]

    def audited(self, capability: str, fn, *args):
        """A trampoline call: the plugin's audit context is set for its duration."""
        previous = getattr(_local, "plugin", None)
        _local.plugin = (WHEEL_KEY, capability)
        try:
            return fn(*args)
        finally:
            _local.plugin = previous

    def page_message(self, data):
        """The page's ``on_message``: UI thread, audited."""
        page = self.capability("BamDude")
        return self.ui.call(lambda: self.audited("BamDude", page.on_message, data))

    def agent_call(self, method: str, *args):
        """A printer-agent method as OrcaSlicer calls it from its UI (discovery, selection, connection): audited."""
        agent = self.capability("BamDudePrinter")
        return self.ui.call(lambda: self.audited("BamDudePrinter", getattr(agent, method), *args))

    def job_call(self, method: str, *args):
        """A ``start_*`` as OrcaSlicer's job worker calls it (rc PrintJob/SendJob::process): audited, on the calling
        (non-UI) thread, which the test stands in for."""
        agent = self.capability("BamDudePrinter")
        return self.audited("BamDudePrinter", getattr(agent, method), *args)

    def window_submit(self, index: int, data) -> None:
        """The page calls ``window.orca.submit(data)``: ``on_submit`` on the UI thread, the window closes, no
        ``on_close`` (rc WebDialog::finish)."""
        window = self.windows[index]

        def run():
            window["open"] = False
            if window["on_submit"] is not None:
                window["on_submit"](data)

        self.ui.call(run)

    def window_close(self, index: int) -> None:
        """The close box or ``window.orca.close()``: ``on_close`` on the UI thread (rc WebDialog::on_close_window)."""
        window = self.windows[index]

        def run():
            if window["open"]:
                window["open"] = False
                if window["on_close"] is not None:
                    window["on_close"]()

        self.ui.call(run)

    def select_agent(self) -> None:
        """The user picks our agent (or a preset that uses it): callbacks first, then discovery, on the UI thread."""
        host = self

        def queue_on_main(fn):
            host.ui.post(fn)

        def ssdp(message):
            if not host.queue_on_main_set:
                return  # rc drops the SSDP callback unless queue_on_main was set
            data = json.loads(message)
            host.ui.post(lambda: host.devices.__setitem__(data["dev_id"], data))

        def local_connect(status, dev_id, message):
            def show():
                host.connects.append((status, dev_id, message))
                if status == 1:
                    name = host.devices.get(dev_id, {}).get("dev_name", dev_id)
                    host.dialogs.append(f"Connect {name} failed! [SN:{dev_id}, code={message}]")

            host.ui.post(show)

        def local_message(dev_id, message):
            host.ui.post(lambda: host.device_messages.append((dev_id, message)))

        def printer_connected(topic):
            host.ui.post(lambda: host.connected_topics.append(topic))

        given = {
            "set_queue_on_main_fn": queue_on_main,
            "set_on_ssdp_msg_fn": ssdp,
            "set_on_local_connect_fn": local_connect,
            "set_on_local_message_fn": local_message,
            "set_on_printer_connected_fn": printer_connected,
        }
        for setter in AGENT_CALLBACK_SETTERS:
            self.agent_call(setter, given.get(setter, lambda *args: None))
            if setter == "set_queue_on_main_fn":
                self.queue_on_main_set = True
        self.agent_call("start_discovery", True, False)

    def connect_device(self, dev_id: str, access_code: str = "12345678"):
        """``DeviceManager::set_selected_machine`` -> ``connect_printer`` on the UI thread."""
        return self.agent_call("connect_printer", PrinterConnectionParams(dev_id=dev_id, password=access_code))

    def settle(self) -> None:
        """Let the UI thread run what was posted to it so far."""
        self.ui.call(lambda: None)

    def lifecycle(self, event_name: str):
        for name, instance in self.loaded["capabilities"].items():
            event = getattr(self.orca.LifecycleEvent, event_name)
            self.ui.call(lambda i=instance, n=name, e=event: self.audited(n, i.on_lifecycle_event, e, None))

    def _missing(self, requests: list[dict]) -> dict:
        asked: dict[str, set] = {"network": set(), "fs_read": set(), "process": set(), "threading": set()}
        for request in requests:
            for bucket in ("network", "fs_read", "process"):
                asked[bucket] |= set(request.get(bucket, []))
            if request.get("threading"):
                asked["threading"].add("thread")
        return {bucket: targets - self.granted[bucket] for bucket, targets in asked.items()}

    def _swap_in(self) -> None:
        if self._saved_modules is None:
            self._saved_modules = {
                name: mod
                for name, mod in sys.modules.items()
                if name == PACKAGE or name.startswith(PACKAGE + ".") or name == "orca"
            }
        self._purge_modules()
        sys.modules["orca"] = self.orca
        if str(self.extracted) not in sys.path:
            sys.path.insert(0, str(self.extracted))

    def _purge_modules(self) -> None:
        for name in [n for n in sys.modules if n == PACKAGE or n.startswith(PACKAGE + ".")]:
            del sys.modules[name]

    def _restore_modules(self) -> None:
        self._purge_modules()
        sys.modules.pop("orca", None)
        if str(self.extracted) in sys.path:
            sys.path.remove(str(self.extracted))
        sys.modules.update(self._saved_modules or {})

    # -- the orca module ---------------------------------------------------------------------------------------------

    def _build_module(self):
        host = self
        orca = types.ModuleType("orca")

        class base:
            def register_capabilities(self):
                pass

        def plugin(cls):
            discovery = getattr(_local, "discovery", None)
            if discovery is None:
                raise ValueError("@orca.plugin used outside plugin discovery context")
            if discovery["plugin_cls"] is not None:
                raise ValueError("multiple @orca.plugin classes registered")
            discovery["plugin_cls"] = cls
            return cls

        def register_capability(cls):
            discovery = getattr(_local, "discovery", None)
            if discovery is None:
                raise ValueError("register_capability() called outside plugin discovery context")
            discovery["capabilities"].append(cls)

        def request_permissions(
            fs_read=(), fs_readwrite=(), network_http=(), network_socket=(), process=(), threading=False, network=()
        ):
            discovery = getattr(_local, "discovery", None)
            if discovery is None:
                raise ValueError("request_permissions() called outside plugin discovery context")
            request = {
                "fs_read": list(fs_read),
                "network": list(network),
                "process": list(process),
                "threading": bool(threading),
            }
            if any(not t for bucket in ("fs_read", "network", "process") for t in request[bucket]):
                raise ValueError("empty permission target")
            discovery["requests"].append(request)
            host.requests.append(request)
            return True

        class _Capability:
            def get_config(self):
                override = host._override_for(self.get_name())
                if override is not None:
                    return json.dumps(override)
                return json.dumps(host.read_base(self.get_name()) or {})

            def save_config(self, config: str) -> bool:
                try:
                    parsed = json.loads(config)
                except ValueError:
                    return False
                host.write_base(self.get_name(), parsed)
                return True

            def on_load(self):
                pass

            def on_unload(self):
                pass

            def on_lifecycle_event(self, event, ctx):
                pass

        class AgentInfo:
            def __init__(self, id="", name="", version="", description=""):  # noqa: A002 -- the binding's names
                self.id, self.name, self.version, self.description = id, name, version, description

        class PrinterAgentBase(_Capability):
            pass

        class PagesPluginCapabilityBase(_Capability):
            def post_message(self, data):
                host.page_posts.append(json.loads(json.dumps(data)))

        host.page_posts = []
        orca.base = base
        orca.plugin = plugin
        orca.register_capability = register_capability
        orca.request_permissions = request_permissions
        orca.LifecycleEvent = enum.Enum("LifecycleEvent", "PresetSelected PresetSaved ProjectOpened")
        orca.printer_agent = types.SimpleNamespace(
            PrinterAgentBase=PrinterAgentBase,
            AgentInfo=AgentInfo,
            CameraStreamMode=CameraStreamMode,
            PrinterConnectionParams=PrinterConnectionParams,
        )
        orca.pages = types.SimpleNamespace(PagesPluginCapabilityBase=PagesPluginCapabilityBase)

        def storage():
            if getattr(_local, "plugin", None) is None:
                raise RuntimeError("plugin.storage() must be called from a plugin callback")
            path = host.storage_dir
            path.mkdir(parents=True, exist_ok=True)
            return str(path)

        def app_info():
            if not getattr(_local, "ui", False):
                raise RuntimeError("app_info() must be called from the UI thread")
            return {"version": "2.5.0-rc", "language": host.language}

        def push_notification(level, text, hyper_text="", on_click=None):
            record = {"level": level, "text": text, "hyper_text": hyper_text, "on_click": on_click, "active": True}

            def show():
                host.notifications.append(record)

            if on_click is not None:
                host.callbacks.append(record)
            if getattr(_local, "ui", False):
                return show()
            if host.closing:
                raise RuntimeError("OrcaSlicer is closing")
            done = host.ui.post(show)
            while not done["event"].wait(0.05):
                if host.closing and host.ui.abandon(done):  # only a call still Pending can be given up
                    raise RuntimeError("OrcaSlicer is closing")
            return None

        class Preset:
            def __init__(self, name):
                self.name = name

            def config_value(self, key):
                if key == "printer_plugin_config_overrides":
                    if host.preset_raw is not None:
                        return host.preset_raw
                    return json.dumps(host.preset_overrides) if host.preset_overrides else ""
                return None

        def edited_preset():
            if host.preset_error is not None:
                raise host.preset_error
            return Preset(host.preset_name)

        printers = types.SimpleNamespace(
            get_edited_preset=edited_preset, get_selected_preset_name=lambda: host.preset_name
        )
        bundle = types.SimpleNamespace(printers=printers)

        def preset_bundle():
            # The real accessor has no thread check (PluginHostApp.cpp); the fake records where it was read so a
            # test can hold the adapter to reading it on the UI thread only (H1 review R3).
            host.preset_reads.append(bool(getattr(_local, "ui", False)))
            return bundle

        def create_window(html, title="OrcaSlicer", width=820, height=600, on_message=None, on_close=None, style=0,
                          on_submit=None):  # fmt: skip
            # rc PluginHostUi.cpp ui_create_window: other style bits are invalid_argument (ValueError in Python); the
            # dialog is created by CallAfter and the handle returned at once, even for a modal window.
            if style & ~1:
                raise ValueError("unsupported orca.host.ui.create_window style flags")
            window = {"html": html, "title": title, "style": style, "on_submit": on_submit, "on_close": on_close,
                      "open": False, "closed_by_plugin": False}  # fmt: skip
            host.windows.append(window)
            host.ui.post(lambda: window.update(open=True))

            class UiWindow:
                def post(self, data):
                    pass

                def close(self):
                    window["closed_by_plugin"] = True
                    host.ui.post(lambda: window.update(open=False))

                def is_open(self):
                    return window["open"]

            return UiWindow()

        host.windows = []
        orca.host = types.SimpleNamespace(
            plugin=types.SimpleNamespace(storage=storage),
            app_info=app_info,
            preset_bundle=preset_bundle,
            ui=types.SimpleNamespace(
                push_notification=push_notification,
                NotificationLevel=NotificationLevel,
                create_window=create_window,
                WINDOW_MODELESS=0,
                WINDOW_MODAL=1,
            ),
        )
        return orca

    def _override_for(self, capability: str):
        for entry in self.preset_overrides:
            if entry.get("capability") == capability and entry.get("plugin_key") == WHEEL_KEY:
                return entry.get("cap_config")
        return None

    def set_override(self, capability: str, config: dict | None) -> None:
        self.preset_overrides = [e for e in self.preset_overrides if e.get("capability") != capability]
        if config is not None:
            self.preset_overrides.append(
                {
                    "capability": capability,
                    "capability_type": "printer-connection",
                    "plugin_key": WHEEL_KEY,
                    "plugin_version": "0.1.0",
                    "cap_config": config,
                }
            )


def storage_files(host: Host) -> dict:
    """The plugin's storage folder, file name -> text, for assertions."""
    folder = host.storage_dir
    if not folder.exists():
        return {}
    return {p.name: p.read_text(encoding="utf-8") for p in folder.iterdir() if p.is_file()}


__all__ = ["Host", "NotificationLevel", "storage_files"]
