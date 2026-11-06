"""The plugin's sentences in English and Ukrainian (host spec r2.3.2 §10)."""

import json
from pathlib import Path

import pytest

from orca_bamdude import i18n

LOCALES = Path(__file__).resolve().parents[1] / "src" / "orca_bamdude" / "locales"


def _flat(data, prefix=""):
    out = {}
    for key, value in data.items():
        if isinstance(value, dict):
            out.update(_flat(value, f"{prefix}{key}."))
        else:
            out[f"{prefix}{key}"] = value
    return out


def test_both_locales_have_the_same_keys_and_no_empty_sentence():
    en = _flat(json.loads((LOCALES / "en.json").read_text(encoding="utf-8")))
    uk = _flat(json.loads((LOCALES / "uk.json").read_text(encoding="utf-8")))
    assert set(en) == set(uk)
    assert all(isinstance(v, str) and v.strip() for v in [*en.values(), *uk.values()])


@pytest.mark.parametrize(
    ("code", "language"),
    [("uk_UA", "uk"), ("uk", "uk"), ("en_US", "en"), ("de_DE", "en"), ("", "en"), (None, "en"), (5, "en")],
)
def test_orcas_language_code_picks_a_locale(code, language):
    assert i18n.language(code) == language


def test_a_sentence_in_each_language():
    assert i18n.say("uk", "connect.foreign") != i18n.say("en", "connect.foreign")
    assert "BamDude" in i18n.say("en", "connect.foreign")


def test_an_unknown_key_is_the_key_itself_never_an_error():
    assert i18n.say("uk", "no.such.key") == "no.such.key"


@pytest.mark.parametrize("key", ["connect.foreign", "connect.setup", "connect.restart", "connect.refused"])
def test_a_connect_refusal_is_a_sentence_not_a_code(key):
    # OrcaSlicer shows local_connect's message as it is; "5" erases the saved access code, "-1" adds Bambu hints (RF-4).
    for language in ("en", "uk"):
        text = i18n.say(language, key)
        assert text not in ("5", "-1") and len(text) > 20
