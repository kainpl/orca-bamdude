"""Which OrcaSlicer device is which BamDude printer (host spec r2.3.2 §5.1)."""

import hashlib
import json

import pytest

from orca_bamdude.api import Capabilities
from orca_bamdude.devices import Binding, dev_id, device_map

SERVER = "3f6c2a1e-7d4b-4c1a-9b8e-2f0d5c6a7e81"
EPOCH = "e4b1c9d2-5a6f-4e3b-8c7d-1a2b3c4d5e6f"
ACTOR = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"
PRINTER = "0b1c2d3e-4f5a-4b6c-8d7e-9f0a1b2c3d4e"
BINDING = Binding("https://farm.example/bamdude", SERVER, EPOCH, ACTOR)


def caps(server=SERVER, epoch=EPOCH, actor=ACTOR):
    return Capabilities(server, epoch, actor, "0.7.3", True, {}, {})


def printer(pid=1, identity=PRINTER, name="X1C left", model_code="BL-P001", **extra):
    return {"id": pid, "name": name, "connector_identity": identity, "model_code": model_code, **extra}


# -- the formula ---------------------------------------------------------------------------------------------------


def test_dev_id_is_the_spec_formula():
    # The same vectors come from the preflight probe's dev_id, which the real host accepted (run 2).
    assert dev_id(BINDING, PRINTER) == "bamdude-2688d26359a7b4ba6081a07cd746c56e"
    other = Binding("http://127.0.0.1:8000", "s", "e", "a")
    assert dev_id(other, "принтер") == "bamdude-b87937d8dd8f4385dc9cd3df3939bc3e"


def test_dev_id_hashes_a_compact_json_array_of_the_binding_and_the_printer():
    encoded = json.dumps(["https://farm.example/bamdude", SERVER, EPOCH, ACTOR, PRINTER], separators=(",", ":"))
    assert dev_id(BINDING, PRINTER) == "bamdude-" + hashlib.sha256(encoded.encode()).hexdigest()[:32]


@pytest.mark.parametrize(
    "changed",
    [
        Binding("https://farm.example/other", SERVER, EPOCH, ACTOR),  # another base path
        Binding("https://lan.example/bamdude", SERVER, EPOCH, ACTOR),  # the same server at another address
        Binding("https://farm.example/bamdude", "another-server", EPOCH, ACTOR),
        Binding("https://farm.example/bamdude", SERVER, "a-restore", ACTOR),  # a restore rotates the epoch
        Binding("https://farm.example/bamdude", SERVER, EPOCH, "another-key"),
    ],
)
def test_every_part_of_the_binding_makes_another_device(changed):
    assert dev_id(changed, PRINTER) != dev_id(BINDING, PRINTER)


def test_another_printer_is_another_device():
    assert dev_id(BINDING, "another-printer") != dev_id(BINDING, PRINTER)


def test_the_binding_is_the_canonical_address_and_the_capabilities_namespace():
    binding = Binding.of("HTTPS://Farm.Example/bamdude/", caps())
    assert binding == BINDING


def test_a_ca_or_proxy_headers_are_no_part_of_the_binding():
    # Binding.of takes the address and the namespace only: the same target under another CA or proxy header is the
    # same device (spec §4.4), so a callback after such a switch still belongs to it.
    assert Binding.of("https://farm.example/bamdude", caps()) == BINDING


# -- the map -------------------------------------------------------------------------------------------------------


def test_a_printer_with_a_model_code_is_announced():
    devices = device_map(BINDING, [printer()])
    (device,) = devices.announced()
    assert device.dev_id == dev_id(BINDING, PRINTER)
    assert (device.printer_id, device.identity, device.name, device.model_code) == (1, PRINTER, "X1C left", "BL-P001")
    assert devices.get(device.dev_id) is device


def test_a_printer_without_a_model_code_is_listed_with_a_reason_and_not_announced():
    devices = device_map(BINDING, [printer(model_code=None)])
    assert devices.announced() == []
    ((device, reason),) = devices.unannounced()
    assert (device.printer_id, reason) == (1, "model_unknown")
    assert devices.get(device.dev_id) is None  # nothing can connect to a device Orca was never shown


def test_a_dev_id_of_another_binding_is_not_found():
    old = device_map(BINDING, [printer()])
    new = device_map(Binding("https://farm.example/bamdude", SERVER, "a-restore", ACTOR), [printer()])
    (device,) = old.announced()
    assert new.get(device.dev_id) is None
    assert new.get("") is None
    assert new.get("bamdude-not-a-device") is None


@pytest.mark.parametrize(
    "bad",
    [
        {"id": True, "name": "n", "connector_identity": PRINTER, "model_code": "C12"},  # bool is no id
        {"id": "1", "name": "n", "connector_identity": PRINTER, "model_code": "C12"},
        {"id": 0, "name": "n", "connector_identity": PRINTER, "model_code": "C12"},
        {"id": 1, "name": "n", "connector_identity": "", "model_code": "C12"},
        {"id": 1, "name": "n", "connector_identity": 5, "model_code": "C12"},
        {"id": 1, "name": "n", "model_code": "C12"},
        {"id": 1, "name": 7, "connector_identity": PRINTER, "model_code": "C12"},
        {"id": 1, "name": "n", "connector_identity": PRINTER, "model_code": 12},
        "not an object",
    ],
)
def test_a_printer_that_is_not_well_formed_is_left_out(bad):
    devices = device_map(BINDING, [bad, printer(pid=2, identity="good")])
    assert [d.printer_id for d in devices.announced()] == [2]
    assert devices.unannounced() == []


def test_two_printers_claiming_one_identity_are_both_left_out():
    # The server keeps identities unique; if an answer ever repeats one, neither printer can be told apart.
    devices = device_map(BINDING, [printer(pid=1), printer(pid=2), printer(pid=3, identity="other")])
    assert [d.printer_id for d in devices.announced()] == [3]


def test_an_empty_model_code_is_unknown():
    devices = device_map(BINDING, [printer(model_code="")])
    assert devices.announced() == [] and devices.unannounced()[0][1] == "model_unknown"


def test_a_missing_name_is_shown_as_the_printer_number():
    devices = device_map(BINDING, [printer(name="")])
    assert devices.announced()[0].name == "#1"


def test_the_map_keeps_the_servers_order():
    devices = device_map(BINDING, [printer(pid=5, identity="e"), printer(pid=2, identity="b")])
    assert [d.printer_id for d in devices.announced()] == [5, 2]


def test_a_device_found_by_printer_identity():
    devices = device_map(BINDING, [printer()])
    assert devices.by_identity(PRINTER).printer_id == 1
    assert devices.by_identity("other") is None
