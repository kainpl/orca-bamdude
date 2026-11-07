"""The host adapter on the fake OrcaSlicer (tests/fake_orca.py): registration, the one permission request, the
capability names, storage, the settings snapshot, a refused prompt and the next start, re-activation (host spec
r2.3.2 §4.1-§4.5, §5), and the H1 review's R1, R3 and R4."""

import json
import os
import sys
import threading
import time
import types

import pytest
from fake_orca import Host, storage_files

from orca_bamdude.locking import FileLock
from orca_bamdude.permits import MAX_HELD, begin_load

AGENT = "BamDudePrinter"
PAGE = "BamDude"
KEY = "bd_secret_key_SENTINEL"
BASE = {"server_url": "https://farm.example", "api_key": KEY}


@pytest.fixture
def host(tmp_path):
    host = Host(tmp_path / "data")
    host.install()
    yield host
    host.close()


def adapter(host):
    return sys.modules["orca_bamdude.orca_adapter"]


def ui_entry(host):
    """A first UI entry, as OrcaSlicer makes after an agent switch: the snapshot is taken here, never in on_load."""
    host.agent_call("start_discovery", True, False)
    return adapter(host).RUNTIME.snapshot


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_the_plugin_registers_two_capabilities_with_the_proven_names(host):
    assert host.load()
    assert set(host.loaded["capabilities"]) == {AGENT, PAGE}
    info = host.ui.call(host.capability(AGENT).get_agent_info)
    assert (info.id, info.name) == (AGENT, "BamDude")  # id == capability name: the preset finds it (RF-1)


def test_a_first_load_asks_once_for_the_fixed_targets_and_the_base(host):
    host.write_base(AGENT, BASE)
    assert host.load()
    assert len(host.requests) == 1
    request = host.requests[0]
    assert request["threading"] is True
    assert set(request["network"]) == {"http://127.0.0.1", "https://farm.example"}
    assert request["process"] == ["https://farm.example/"]
    assert len(request["fs_read"]) == 1 and request["fs_read"][0].endswith("orcaslicer_model" + os.sep + "*")


def test_a_base_stored_as_a_json_string_is_read_the_same(host):
    host.write_base(AGENT, BASE, as_string=True)
    assert host.load()
    assert "https://farm.example" in host.requests[0]["network"]


def test_the_base_read_while_orca_saves_its_config_is_still_the_base(host, monkeypatch):
    # OrcaSlicer (or a second one closing down) may replace orca_plugins/config.json while it is read; on Windows that
    # read fails with a sharing violation, which must not look like "no settings" (H2 review, same class as d18e919).
    host.write_base(AGENT, BASE)
    assert host.load()
    locking = sys.modules["orca_bamdude.locking"]
    real_open, calls = open, []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise PermissionError(13, "The process cannot access the file")
        return real_open(*args, **kwargs)

    for module in (locking, adapter(host)):  # whichever reads the file meets the violation first
        monkeypatch.setattr(module, "open", flaky, raising=False)
    monkeypatch.setattr(locking.time, "sleep", lambda s: None)
    assert adapter(host).read_base(adapter(host).INSTALL)["server_url"] == "https://farm.example"


def test_storage_is_the_derived_folder_and_on_load_completes_the_marker(host):
    host.write_base(AGENT, BASE)
    assert host.load()
    module = adapter(host)
    assert module.RUNTIME.storage == str(host.storage_dir)
    state = json.loads(storage_files(host)["state.json"])
    assert state["load_marker"]["completed"] is True
    assert state["load_marker"]["id"] == module.RUNTIME.load_id


def test_on_load_reads_no_preset_and_the_first_ui_entry_takes_the_snapshot(host):
    # H1 review R3: on_load runs on OrcaSlicer's load thread; the live preset bundle is read on the UI thread only.
    host.write_base(AGENT, BASE)
    assert host.load()
    assert adapter(host).RUNTIME.snapshot is None
    assert host.preset_reads == []
    snap = ui_entry(host)
    assert snap.source == "base" and snap.connection.base_url == "https://farm.example"
    assert snap.blocked is None
    assert host.preset_reads and all(on_ui for on_ui in host.preset_reads)


def test_get_agent_info_stays_trivial(host):
    host.write_base(AGENT, BASE)
    assert host.load()
    host.ui.call(host.capability(AGENT).get_agent_info)
    assert adapter(host).RUNTIME.snapshot is None  # called thousands of times: no reading there


def test_a_preset_override_takes_the_address_and_keeps_the_base_key(host):
    host.write_base(AGENT, BASE)
    host.set_override(AGENT, {"server_url": "https://farm.example:8443/lan", "api_key": "bd_typed"})
    assert host.load()
    snap = ui_entry(host)
    assert snap.source == "preset" and snap.preset_name == host.preset_name
    assert snap.effective.server_url == "https://farm.example:8443/lan"
    assert snap.effective.connection.api_key == KEY
    assert snap.effective.ignored == ("api_key",)


@pytest.mark.parametrize("kind", ["malformed_envelope", "not_a_list", "null_config", "accessor_raises"])
def test_an_override_that_cannot_be_read_never_falls_back_to_base(host, kind):
    # H1 review R1: absent, read, and unreadable are three states; only "absent" means base.
    host.write_base(AGENT, BASE)
    assert host.load()
    raw = {
        "malformed_envelope": "{broken",
        "not_a_list": json.dumps({"capability": AGENT}),
        "null_config": json.dumps([{"capability": AGENT, "plugin_key": host.plugin_key, "cap_config": None}]),
    }.get(kind)
    if kind == "accessor_raises":
        host.preset_error = RuntimeError("no preset")
    else:
        host.preset_raw = raw
    snap = ui_entry(host)
    assert snap.connection is None and snap.blocked == "problem"


def test_an_empty_envelope_and_another_plugins_entry_are_no_override(host):
    host.write_base(AGENT, BASE)
    assert host.load()
    host.preset_raw = json.dumps([{"capability": AGENT, "plugin_key": "someone_else", "cap_config": {}}])
    assert ui_entry(host).source == "base"
    host.preset_raw = ""
    host.lifecycle("PresetSelected")
    assert adapter(host).RUNTIME.snapshot.source == "base"


def test_a_preset_switch_refreshes_the_snapshot(host):
    host.write_base(AGENT, BASE)
    assert host.load()
    before = ui_entry(host).generation
    host.set_override(AGENT, {"server_url": "http://192.168.1.5:8000"})
    host.lifecycle("PresetSelected")
    snap = adapter(host).RUNTIME.snapshot
    assert snap.generation == before + 1
    assert snap.blocked == "needs_restart"  # this load never asked for 192.168.1.5


def test_a_refused_prompt_disables_the_plugin_and_the_next_load_holds_the_base(host):
    host.write_base(AGENT, BASE)
    host.prompt_answer = False
    assert host.load() is False
    assert host.enabled is False
    assert host.load() is False  # disabled: not imported at all until Activate
    host.prompt_answer = True
    assert host.activate()
    request = host.requests[-1]
    assert "https://farm.example" not in request["network"]  # held, base included
    assert request["process"] == []
    assert host.prompts[-1]["network"] == {"http://127.0.0.1"}  # the refusal granted nothing, so the fixed ones
    snap = ui_entry(host)
    assert snap.blocked == "held" and snap.connection is None
    assert adapter(host).RUNTIME.view()["previous_load"] == "unfinished"


def test_asking_again_from_the_tab_brings_the_base_back_at_the_next_start(host):
    host.write_base(AGENT, BASE)
    host.prompt_answer = False
    host.load()
    host.prompt_answer = True
    host.activate()
    host.page_message({"op": "ask_again"})
    assert wait_for(lambda: any(p.get("recorded") is True for p in host.page_posts))
    host.unload()
    assert host.load()
    assert "https://farm.example" in host.requests[-1]["network"]
    assert ui_entry(host).blocked is None


def test_a_restart_after_recovery_keeps_the_hold(host):
    host.write_base(AGENT, BASE)
    host.prompt_answer = False
    host.load()
    host.prompt_answer = True
    host.activate()
    ui_entry(host)
    host.unload()
    assert host.load()
    assert "https://farm.example" not in host.requests[-1]["network"]


def test_unload_raises_the_fence_and_reactivation_imports_a_new_module_and_runtime(host):
    host.write_base(AGENT, BASE)
    assert host.load()
    first_module, first_runtime = adapter(host), adapter(host).RUNTIME
    host.unload()
    assert first_runtime.context.closed and first_runtime.stopping
    assert host.activate()
    second = adapter(host)
    assert second is not first_module and second.RUNTIME is not first_runtime
    assert second.RUNTIME.context.closed is False


def test_the_page_state_carries_no_secret(host):
    host.write_base(AGENT, {**BASE, "headers": [["CF-Access-Client-Secret", "hdr_SENTINEL"]]})
    assert host.load()
    host.page_message({"op": "state"})
    text = json.dumps(host.page_posts)
    assert host.page_posts and KEY not in text and "hdr_SENTINEL" not in text


def test_the_page_never_waits_for_the_state_file_lock(host):
    # H1 review R4: another OrcaSlicer process holding state.lock must not stall the UI thread.
    host.write_base(AGENT, BASE)
    assert host.load()
    held, release = threading.Event(), threading.Event()

    def hold():
        with FileLock(str(host.storage_dir / "state.lock"), 5):
            held.set()
            release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert held.wait(5)
        host.set_override(AGENT, {"server_url": "https://farm.example/changed"})
        for op in ({"op": "state"}, {"op": "ask_again"}):
            started = time.monotonic()
            host.page_message(op)
            assert time.monotonic() - started < 0.3, op
        assert not any(p.get("recorded") is True for p in host.page_posts)  # not shown as saved before it is
    finally:
        release.set()
        holder.join(5)
    assert wait_for(lambda: any(p.get("recorded") is True for p in host.page_posts))


def test_under_hold_everything_the_tab_asks_again_only_for_the_current_settings(host):
    # H1 fix review F2: an older refused address that is no current setting stays held, even while pending.
    host.write_base(AGENT, BASE)
    assert host.load()
    farm, other = "network:https://farm.example", "network:https://other.example"
    holds = [other] + [f"network:https://h{i}.example" for i in range(MAX_HELD - 1)]

    def overflow(state):
        state = {
            **state,
            "held_targets": holds,
            "pending_targets": [other],
            "load_marker": {"id": "refused", "targets": [farm], "completed": False},
        }
        return begin_load(state, "recovered", {farm})[0]

    adapter(host).RUNTIME.update_state(overflow)
    host.page_message({"op": "ask_again"})
    assert wait_for(lambda: any(p.get("recorded") is True for p in host.page_posts))
    host.unload()
    assert host.load()
    network = host.requests[-1]["network"]
    assert "https://farm.example" in network and "https://other.example" not in network


def test_the_import_repairs_an_unreadable_state_and_the_tab_says_so(host):
    # The import is the one deliberate repair (H1 fix3 review F1-R2); every later write refuses an unreadable state.
    host.write_base(AGENT, BASE)
    host.storage_dir.mkdir(parents=True, exist_ok=True)
    (host.storage_dir / "state.json").write_text("{not json", encoding="utf-8")
    assert host.load()
    assert json.loads((host.storage_dir / "state.json").read_text(encoding="utf-8"))["load_marker"]["completed"]
    assert adapter(host).RUNTIME.view()["state_repaired"] is True


def test_saving_the_base_in_the_tab_writes_it_and_is_an_explicit_retry(host):
    host.write_base(AGENT, BASE)
    host.prompt_answer = False
    host.load()
    host.prompt_answer = True
    host.activate()
    host.page_message({"op": "save_settings", "edit": {"server_url": "https://farm.example"}})
    assert host.read_base(AGENT)["server_url"] == "https://farm.example"
    assert host.read_base(AGENT)["api_key"] == KEY  # kept, never sent back by the page
    assert wait_for(lambda: any(p.get("saved") and p.get("recorded") for p in host.page_posts))
    host.unload()
    assert host.load()
    assert "https://farm.example" in host.requests[-1]["network"]


def test_a_refused_edit_is_reported_by_code_and_writes_nothing(host):
    host.write_base(AGENT, BASE)
    assert host.load()
    host.page_message({"op": "save_settings", "edit": {"server_url": "ftp://farm.example"}})
    assert host.read_base(AGENT) == BASE
    assert host.page_posts[-1] == {"kind": "error", "op": "save_settings", "code": "url_scheme"}


def test_the_config_ui_never_hands_the_stock_editor_a_secret(host):
    host.write_base(AGENT, BASE)
    assert host.load()
    agent = host.capability(AGENT)
    assert agent.has_config_ui() is True  # the stock JSON editor would show the key (§8.1 ⟨R23-A1⟩)
    html = agent.get_config_ui()
    assert "getContext" in html and KEY not in html


def test_a_notification_click_after_unload_does_nothing(host):
    host.write_base(AGENT, BASE)
    assert host.load()
    runtime = adapter(host).RUNTIME
    clicks = []
    runtime.notify("Warning", "refused", on_click=lambda: clicks.append(1) or True, hyper_text="Open")
    assert wait_for(lambda: host.notifications)
    record = host.notifications[0]
    host.unload()
    record["active"] = True  # as if it registered after the host's teardown (⟨R23.1-A1⟩)
    assert record["on_click"]() is True and clicks == []


def test_the_agent_answers_verify_job_without_io(host):
    host.write_base(AGENT, BASE)
    assert host.load()
    params = types.SimpleNamespace(project_name="verify_job", dev_id="x")
    assert host.agent_call("start_send_gcode_to_sdcard", params, None, None, None) == 0


def test_saving_in_the_tab_changes_the_base_and_never_the_presets_override(host):
    # spec r2 §8.1 ⟨R23-A1⟩: the tab saves base; the override belongs to the preset's own editor.
    host.write_base(AGENT, BASE)
    override = {"server_url": "https://farm.example:8443/lan"}
    host.set_override(AGENT, override)
    assert host.load()
    ui_entry(host)
    host.page_message({"op": "save_settings", "edit": {"export_dir": str(host.storage_dir)}})
    assert wait_for(lambda: (host.read_base(AGENT) or {}).get("export_dir") == str(host.storage_dir))
    (entry,) = [e for e in host.preset_overrides if e["capability"] == AGENT]
    assert entry["cap_config"] == override
