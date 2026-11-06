"""Host spec r2.3.2 §4.4 ⟨R23-A1⟩ and §8.1: the stored base, the preset override, what the page may see."""

import json
from pathlib import Path

import pytest

from orca_bamdude.config import ConfigError
from orca_bamdude.settings import (
    apply_edit,
    dashboard_url,
    effective,
    masked_view,
    origin,
    parse_base,
    slot_targets,
    user_targets,
)

KEY = "bd_secret_key_SENTINEL"
HEADER_VALUE = "header_secret_SENTINEL"
CA = (Path(__file__).parent / "data" / "tls" / "ca.pem").read_text(encoding="ascii").strip()


def base_raw(**over):
    raw = {
        "server_url": "https://Farm.Example:8443/bamdude/",
        "api_key": KEY,
        "headers": [["CF-Access-Client-Id", HEADER_VALUE]],
        "export_dir": "",
    }
    raw.update(over)
    return raw


# -- the stored base ---------------------------------------------------------------------------------------------


def test_a_valid_base_is_canonical_and_complete():
    base = parse_base(base_raw())
    assert base.server_url == "https://farm.example:8443/bamdude"
    assert base.api_key == KEY
    assert base.headers == (("CF-Access-Client-Id", HEADER_VALUE),)
    assert base.problems == ()


def test_an_empty_base_has_no_connection_and_no_problems():
    base = parse_base({})
    assert (base.server_url, base.api_key, base.ca_pem, base.headers, base.export_dir) == (None, None, None, (), None)
    assert base.problems == ()
    assert effective(base, None).connection is None
    assert effective(base, None).missing == ("server_url", "api_key")


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("server_url", "ftp://farm.example", "server_url:url_scheme"),
        ("api_key", "has space", "api_key:api_key"),
        ("headers", [["Authorization", "x"]], "headers:header_reserved"),
        ("ca_pem", "-----BEGIN PRIVATE KEY-----\nAA\n-----END PRIVATE KEY-----", "ca_pem:ca_pem_block"),
        ("export_dir", "relative/path", "export_dir:export_dir_relative"),
    ],
)
def test_a_broken_stored_field_is_a_problem_never_a_guess(field, value, code):
    base = parse_base(base_raw(**{field: value}))
    assert code in base.problems
    assert getattr(base, field) in (None, ())


def test_a_base_that_is_not_an_object_is_one_problem():
    assert parse_base("not an object").problems == ("base:not_an_object",)


# -- the preset override (address and CA only) -------------------------------------------------------------------


def test_without_an_override_the_base_is_effective():
    eff = effective(parse_base(base_raw()), None)
    assert eff.source == "base"
    assert eff.connection.base_url == "https://farm.example:8443/bamdude"
    assert eff.connection.api_key == KEY


def test_an_override_replaces_only_the_address_and_ca():
    eff = effective(parse_base(base_raw()), {"server_url": "http://192.168.1.5:8000"}, preset_name="X1 LAN")
    assert eff.source == "preset"
    assert eff.preset_name == "X1 LAN"
    assert eff.connection.base_url == "http://192.168.1.5:8000"
    # the key and the proxy headers come from base alone
    assert eff.connection.api_key == KEY
    assert eff.connection.extra_headers == (("CF-Access-Client-Id", HEADER_VALUE),)
    assert eff.ignored == ()


def test_a_key_typed_into_an_override_is_ignored_and_reported():
    override = {"server_url": "http://192.168.1.5:8000", "api_key": "bd_other", "headers": [["X-A", "b"]]}
    eff = effective(parse_base(base_raw()), override)
    assert eff.connection.api_key == KEY
    assert eff.ignored == ("api_key", "headers")


def test_an_override_without_a_ca_field_keeps_the_base_ca_and_an_empty_one_means_system_trust():
    base = parse_base(base_raw(ca_pem=CA))
    assert effective(base, {"server_url": "http://192.168.1.5:8000"}).connection.ca_pem == CA
    assert effective(base, {"server_url": "http://192.168.1.5:8000", "ca_pem": ""}).connection.ca_pem is None


def test_a_broken_override_refuses_the_connection_instead_of_falling_back_to_base():
    eff = effective(parse_base(base_raw()), {"server_url": "ftp://farm.example"}, preset_name="X1")
    assert eff.source == "preset"
    assert eff.connection is None
    assert eff.problems == ("override:server_url:url_scheme",)


@pytest.mark.parametrize(
    "field,value", [("ca_pem", "invalid PEM"), ("headers", [["Authorization", "x"]]), ("api_key", "has space")]
)
def test_a_broken_base_field_the_connection_uses_refuses_it(field, value):
    # H1 review R1: a CA or header the user meant to apply is never replaced by the system's trust or by nothing.
    eff = effective(parse_base(base_raw(**{field: value})), None)
    assert eff.connection is None
    assert any(p.startswith(f"{field}:") for p in eff.problems)


def test_a_broken_base_ca_does_not_matter_when_the_override_sets_its_own():
    base = parse_base(base_raw(ca_pem="invalid PEM"))
    assert effective(base, {"server_url": "http://192.168.1.5:8000", "ca_pem": ""}).connection is not None
    assert effective(base, {"server_url": "http://192.168.1.5:8000"}).connection is None


def test_a_broken_export_folder_does_not_block_the_connection():
    eff = effective(parse_base(base_raw(export_dir="relative")), None)
    assert eff.connection is not None and eff.problems == ()


def test_an_override_that_could_not_be_read_refuses_instead_of_falling_back():
    eff = effective(parse_base(base_raw()), None, preset_name="X1", override_error="unreadable")
    assert eff.source == "preset" and eff.connection is None
    assert eff.problems == ("override:unreadable",)


def test_the_masked_view_names_each_problem_once():
    base = parse_base(base_raw(ca_pem="invalid PEM"))
    view = masked_view(base, effective(base, None))
    assert view["problems"] == ["ca_pem:ca_pem_invalid"]


def test_an_override_that_is_not_an_object_is_a_problem():
    eff = effective(parse_base(base_raw()), "garbage")
    assert eff.connection is None
    assert eff.problems == ("override:not_an_object",)


def test_an_override_naming_no_address_still_takes_the_base_address():
    eff = effective(parse_base(base_raw()), {"ca_pem": ""})
    assert eff.source == "preset"
    assert eff.connection.base_url == "https://farm.example:8443/bamdude"


# -- what the page may see ---------------------------------------------------------------------------------------


def test_the_masked_view_never_carries_a_secret():
    base = parse_base(base_raw())
    view = masked_view(base, effective(base, {"server_url": "http://192.168.1.5:8000", "api_key": "bd_typed"}))
    text = json.dumps(view)
    assert KEY not in text and HEADER_VALUE not in text and "bd_typed" not in text
    assert view["api_key_set"] is True
    assert view["headers"] == [{"name": "CF-Access-Client-Id", "value_set": True}]
    assert view["source"] == "preset"
    # under an override, the fields it sets are read-only in the tab; the key and headers stay editable
    assert view["read_only"] == ["server_url", "ca_pem"]
    assert view["ignored"] == ["api_key"]


# -- an edit from the tab: keep / replace / clear ----------------------------------------------------------------


def test_an_edit_keeps_the_key_unless_told_otherwise():
    raw = apply_edit(base_raw(), {"server_url": "https://farm.example"})
    assert raw["api_key"] == KEY and raw["server_url"] == "https://farm.example"
    raw = apply_edit(base_raw(), {"api_key": {"action": "keep"}})
    assert raw["api_key"] == KEY


def test_an_edit_replaces_or_clears_the_key():
    assert apply_edit(base_raw(), {"api_key": {"action": "replace", "value": " bd_new "}})["api_key"] == "bd_new"
    assert "api_key" not in apply_edit(base_raw(), {"api_key": {"action": "clear"}})


def test_a_header_value_is_kept_by_name_replaced_or_dropped_by_omission():
    edit = {"headers": [{"name": "CF-Access-Client-Id", "action": "keep"}, {"name": "X-Extra", "value": "v"}]}
    assert apply_edit(base_raw(), edit)["headers"] == [["CF-Access-Client-Id", HEADER_VALUE], ["X-Extra", "v"]]
    assert apply_edit(base_raw(), {"headers": []})["headers"] == []


@pytest.mark.parametrize(
    "edit,code",
    [
        ({"server_url": "ftp://x"}, "url_scheme"),
        ({"api_key": {"action": "replace", "value": ""}}, "api_key"),
        ({"api_key": {"action": "invent"}}, "edit_action"),
        ({"headers": [{"name": "X-Unknown", "action": "keep"}]}, "header_keep_unknown"),
        ({"headers": [{"name": "Authorization", "value": "x"}]}, "header_reserved"),
        ({"export_dir": "relative"}, "export_dir_relative"),
        ({"unknown_field": 1}, "edit_field"),
    ],
)
def test_a_bad_edit_is_refused_with_a_stable_code(edit, code):
    with pytest.raises(ConfigError) as caught:
        apply_edit(base_raw(), edit)
    assert caught.value.code == code


def test_an_edit_error_never_carries_the_value():
    with pytest.raises(ConfigError) as caught:
        apply_edit(base_raw(), {"api_key": {"action": "replace", "value": "bad key SENTINEL"}})
    assert "SENTINEL" not in str(caught.value)


# -- permission targets ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://Farm.Example:8443/bamdude", "https://farm.example"),
        ("http://192.168.1.5:8000", "http://192.168.1.5"),
        ("http://[::1]:8000/x", "http://[::1]"),
        ("ftp://farm.example", None),
        ("", None),
        (None, None),
    ],
)
def test_an_origin_is_what_orca_stores_for_a_network_grant(url, expected):
    assert origin(url) == expected


def test_the_dashboard_target_is_the_exact_base_url_with_a_slash():
    assert dashboard_url("https://farm.example:8443/bamdude") == "https://farm.example:8443/bamdude/"
    assert dashboard_url(None) is None


def test_each_setting_has_its_own_slot(tmp_path):
    base = parse_base(base_raw(export_dir=str(tmp_path)))
    assert slot_targets(base, effective(base, None)) == {
        "base_network": "network:https://farm.example",
        "effective_network": "network:https://farm.example",
        "dashboard": "process:https://farm.example:8443/bamdude/",
        "export_dir": f"fs_read:{tmp_path}",
    }
    assert slot_targets(parse_base({}), effective(parse_base({}), None)) == {}


def test_user_targets_are_typed_and_carry_no_secret(tmp_path):
    base = parse_base(base_raw(export_dir=str(tmp_path)))
    eff = effective(base, {"server_url": "http://192.168.1.5:8000"})
    targets = user_targets(base, eff)
    assert targets == {
        "network:https://farm.example",
        "network:http://192.168.1.5",
        "process:http://192.168.1.5:8000/",
        f"fs_read:{tmp_path}",
    }
    assert not any(KEY in t or HEADER_VALUE in t for t in targets)
