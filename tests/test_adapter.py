"""The host adapter on the fake OrcaSlicer (tests/fake_orca.py): registration, the one permission request, the
capability names, storage, the settings snapshot, a refused prompt and the next start, re-activation (host spec
r2.3.2 §4.1-§4.5, §5)."""

import json
import os
import sys

import pytest
from fake_orca import Host, storage_files

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


def test_storage_is_the_derived_folder_and_on_load_completes_the_marker(host):
    host.write_base(AGENT, BASE)
    assert host.load()
    module = adapter(host)
    assert module.RUNTIME.storage == str(host.storage_dir)
    state = json.loads(storage_files(host)["state.json"])
    assert state["load_marker"]["completed"] is True
    assert state["load_marker"]["id"] == module.RUNTIME.load_id


def test_on_load_takes_a_snapshot_of_the_base(host):
    host.write_base(AGENT, BASE)
    assert host.load()
    snap = adapter(host).RUNTIME.snapshot
    assert snap.source == "base" and snap.connection.base_url == "https://farm.example"
    assert snap.blocked is None


def test_a_preset_override_takes_the_address_and_keeps_the_base_key(host):
    host.write_base(AGENT, BASE)
    host.set_override(AGENT, {"server_url": "https://farm.example:8443/lan", "api_key": "bd_typed"})
    assert host.load()
    snap = adapter(host).RUNTIME.snapshot
    assert snap.source == "preset" and snap.preset_name == host.preset_name
    assert snap.effective.server_url == "https://farm.example:8443/lan"
    assert snap.effective.connection.api_key == KEY
    assert snap.effective.ignored == ("api_key",)


def test_a_preset_switch_refreshes_the_snapshot(host):
    host.write_base(AGENT, BASE)
    assert host.load()
    before = adapter(host).RUNTIME.snapshot.generation
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
    runtime = adapter(host).RUNTIME
    assert runtime.snapshot.blocked == "held" and runtime.snapshot.connection is None
    assert runtime.view()["previous_load"] == "unfinished"


def test_asking_again_from_the_tab_brings_the_base_back_at_the_next_start(host):
    host.write_base(AGENT, BASE)
    host.prompt_answer = False
    host.load()
    host.prompt_answer = True
    host.activate()
    host.page_message({"op": "ask_again"})
    host.unload()
    assert host.load()
    assert "https://farm.example" in host.requests[-1]["network"]
    assert adapter(host).RUNTIME.snapshot.blocked is None


def test_a_restart_after_recovery_keeps_the_hold(host):
    host.write_base(AGENT, BASE)
    host.prompt_answer = False
    host.load()
    host.prompt_answer = True
    host.activate()
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


def test_saving_the_base_in_the_tab_writes_it_and_is_an_explicit_retry(host):
    host.write_base(AGENT, BASE)
    host.prompt_answer = False
    host.load()
    host.prompt_answer = True
    host.activate()
    host.page_message({"op": "save_settings", "edit": {"server_url": "https://farm.example"}})
    assert host.read_base(AGENT)["server_url"] == "https://farm.example"
    assert host.read_base(AGENT)["api_key"] == KEY  # kept, never sent back by the page
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
    deadline = __import__("time").monotonic() + 5
    while not host.notifications and __import__("time").monotonic() < deadline:
        __import__("time").sleep(0.01)
    record = host.notifications[0]
    host.unload()
    record["active"] = True  # as if it registered after the host's teardown (⟨R23.1-A1⟩)
    assert record["on_click"]() is True and clicks == []


def test_the_agent_answers_verify_job_without_io(host):
    host.write_base(AGENT, BASE)
    assert host.load()
    params = type("P", (), {"project_name": "verify_job", "dev_id": "x"})()
    agent = host.capability(AGENT)
    assert host.audited(AGENT, agent.start_send_gcode_to_sdcard, params, None, None, None) == 0
