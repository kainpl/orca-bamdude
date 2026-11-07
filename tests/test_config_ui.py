"""The agent's config UI (spec r2 §8.1 ⟨R23-A1⟩): one page for both of OrcaSlicer's dialogs -- in the Plugins dialog
only an explanation, in a preset's Capabilities a two-field editor of the override (address and CA) that never shows,
reads or saves the key or the proxy headers."""

import json
import shutil
import subprocess

import pytest

from orca_bamdude import i18n
from orca_bamdude.ui import CONFIG_KEYS, config_ui


def _say(key):
    return i18n.say("en", key)


@pytest.mark.parametrize("lang", ["en", "uk"])
def test_every_sentence_of_the_config_page_exists(lang):
    assert [key for key in CONFIG_KEYS if i18n.say(lang, key) == key] == []


def test_the_page_reads_only_the_two_fields_of_the_config():
    script = config_ui(lambda key: "-", held=False).split("<script>", 1)[1]  # the code, not the sentences
    assert "api_key" not in script and "headers" not in script
    assert "server_url" in script and "ca_pem" in script


def test_a_held_target_is_pointed_to_the_tabs_button():
    assert '{"held": true}' in config_ui(_say, held=True)
    assert '{"held": false}' in config_ui(_say, held=False)


def _payload(url, ca):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = config_ui(_say, held=False).split("<script>", 1)[1].split("</script>", 1)[0]
    function = script.split("/*payload*/", 2)[1]
    probe = f"{function}\nprocess.stdout.write(JSON.stringify(overridePayload({json.dumps(url)}, {json.dumps(ca)})));"
    out = subprocess.run([node, "-e", probe], capture_output=True, text=True, timeout=20, check=True)
    return json.loads(out.stdout)


def test_saving_the_override_sends_only_the_address_and_the_ca():
    assert _payload(" https://farm.example/base ", "") == {"server_url": "https://farm.example/base"}
    pem = "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----"
    assert _payload("", pem) == {"ca_pem": pem}
    assert _payload("", "") == {}
