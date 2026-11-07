"""The pages' own script run in Node against a small DOM (``page_probe.cjs``, from the H4 review): the config page
with OrcaSlicer's real config bridge (rc ``plugin-config-ui.js``, read from the reference checkout at the release
tag), and the tab's farm view as the person sees it. Skipped where Node or the reference checkout is absent."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from orca_bamdude import i18n, ui
from orca_bamdude.tab import fleet_rows

HERE = Path(__file__).resolve().parent
REFERENCE = Path("D:/Development/bamdude/references/OrcaSlicer")
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def probe(payload):
    result = subprocess.run(
        [NODE, str(HERE / "page_probe.cjs")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=20,
    )
    return json.loads(result.stdout)


def _bridge():
    if not REFERENCE.is_dir():
        pytest.skip("the OrcaSlicer reference checkout is not here")
    shown = subprocess.run(
        ["git", "show", "v2.5.0-rc:resources/web/dialog/js/plugin-config-ui.js"],
        cwd=REFERENCE,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if shown.returncode != 0:
        pytest.skip("the reference checkout has no v2.5.0-rc tag")
    return shown.stdout


@pytest.mark.parametrize("initial_override", [False, True])
def test_the_config_page_follows_the_context_the_bridge_sends_after_save_or_restore(initial_override):
    # H4 review R1: after Save the preset has an override, after Restore it has none; the bridge replaces its context.
    base = {"server_url": "http://127.0.0.1/base"}
    override = {"server_url": "http://127.0.0.2/farm"}
    result = probe(
        {
            "mode": "config",
            "bridge": _bridge(),
            "html": ui.config_ui(lambda k: i18n.say("en", k), held=False),
            "initial": override if initial_override else base,
            "initialOverride": initial_override,
            "next": base if initial_override else override,
        }
    )
    assert result["displayed"]["url"] == ("" if initial_override else override["server_url"])
    assert result["outbound"][-1]["__orca"] == ("restore" if initial_override else "save")


def _cells(age_seconds=0, **changes):
    status = {"connected": True, "state": "RUNNING", "progress": 42, "remaining_time": 120}
    status.update(changes)
    rows = fleet_rows([{"id": 1, "name": "Test", "model_code": "C12"}], {1: status})
    page = ui.render(lambda k: i18n.say("en", k))
    return probe({"mode": "fleet", "html": page, "rows": rows, "ageSeconds": age_seconds})["cells"]


def test_the_remaining_time_is_minutes_already():
    # H4 review R2: the server's remaining_time is the printer's mc_remaining_time, in minutes.
    assert _cells()[4] == i18n.say("en", "tab.minutes").replace("{n}", "120")


@pytest.mark.parametrize("mode", ["printer_disconnected", "farm_silent_for_an_hour"])
def test_a_printer_that_is_not_known_now_shows_no_state_and_no_numbers(mode):
    # H4 review R3: a disconnected printer's cached RUNNING, or a view nobody refreshed for an hour, is not current.
    cells = _cells(connected=False) if mode == "printer_disconnected" else _cells(age_seconds=3600)
    assert cells[2] == i18n.say("en", "tab.unknown")
    assert cells[3:5] == ["", ""]
