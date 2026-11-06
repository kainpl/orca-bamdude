"""BamDude facts in the shapes OrcaSlicer 2.5.0-rc parses (host spec r2.3.2 §5.1-§5.2).

Every key and its JSON type below is the one the rc parser reads (``MachineObject::parse_json``,
``DevFilaSystemParser::ParseV1_0``, ``MachineObject::parse_vt_tray``, ``DevBed``, ``DevChamber``, ``DevStorage``,
``DevNozzleSystemParser::ParseV1_0``, the ``get_version`` branch): a wrong type throws inside the host's parser, so the
tests pin types as well as values. What the server does not know is not sent at all.
"""

import json

import pytest

from orca_bamdude.devices import Binding, Device
from orca_bamdude.dialect import push_status, ssdp, version_reply

BINDING = Binding("https://farm.example:8443/bamdude", "s", "e", "a")
DEVICE = Device("bamdude-0123456789abcdef0123456789abcdef", 7, "identity-7", "X1C left", "BL-P001")


def status(**overrides):
    """One printer of ``GET /api/v1/printers/status/batch``, as BamDude's PrinterStatus serialises it."""
    base = {
        "id": 7,
        "name": "X1C left",
        "connected": True,
        "state": "RUNNING",
        "subtask_name": "bracket_v3",
        "progress": 41.6,
        "remaining_time": 37,
        "layer_num": 12,
        "total_layers": 220,
        "temperatures": {"bed": 55.0, "bed_target": 55.0, "nozzle": 219.6, "nozzle_target": 220.0, "chamber": 31.0},
        "nozzles": [{"nozzle_type": "hardened_steel", "nozzle_flow": "standard", "nozzle_diameter": "0.4"}],
        "ams": [
            {
                "id": 0,
                "humidity": 18,
                "temp": 26.5,
                "is_ams_ht": False,
                "tray": [
                    {
                        "id": 0,
                        "tray_type": "PLA",
                        "tray_color": "FF0000FF",
                        "tray_info_idx": "GFA00",
                        "tray_sub_brands": "PLA Basic",
                        "remain": 80,
                        "nozzle_temp_min": 190,
                        "nozzle_temp_max": 230,
                    },
                    {
                        "id": 1,
                        "tray_type": "PETG",
                        "tray_color": "000000FF",
                        "tray_info_idx": "GFG00",
                        "tray_sub_brands": None,
                        "remain": -1,
                        "nozzle_temp_min": None,
                        "nozzle_temp_max": None,
                    },
                    {"id": 2, "tray_type": None, "tray_color": None, "tray_info_idx": None, "remain": 0},
                    {"id": 3, "tray_type": "", "tray_color": "", "tray_info_idx": "", "remain": 0},
                ],
            }
        ],
        "ams_exists": True,
        "vt_tray": [
            {
                "id": 254,
                "tray_type": "TPU",
                "tray_color": "FFFFFFFF",
                "tray_info_idx": "GFU01",
                "remain": 0,
                "nozzle_temp_min": 200,
                "nozzle_temp_max": 250,
            }
        ],
        "tray_now": 1,
        "sdcard": False,
        "firmware_version": "01.08.02.00",
    }
    base.update(overrides)
    return base


def pushed(**overrides) -> dict:
    return json.loads(push_status(status(**overrides), seq=5))["print"]


# -- the announcement ------------------------------------------------------------------------------------------------


def test_ssdp_has_every_key_the_host_requires_as_a_string():
    payload = json.loads(ssdp(DEVICE, BINDING))
    assert payload == {
        "dev_id": DEVICE.dev_id,
        "dev_name": "X1C left",
        "dev_ip": "farm.example",  # the server's host: Orca shows "name (host)"
        "dev_type": "BL-P001",
        "dev_signal": "0",
        "connect_type": "lan",
        "bind_state": "free",
    }


# -- the version -----------------------------------------------------------------------------------------------------


def test_a_known_firmware_is_the_ota_module():
    reply = json.loads(version_reply("01.08.02.00"))
    assert reply == {
        "info": {"command": "get_version", "result": "success", "module": [{"name": "ota", "sw_ver": "01.08.02.00"}]}
    }


@pytest.mark.parametrize("unknown", [None, "", 5, "   "])
def test_an_unknown_firmware_sends_no_ota_module(unknown):
    # Orca merges the model's per-firmware settings (support_print_all and others) by this version; inventing one would
    # claim features the printer may not have.
    reply = json.loads(version_reply(unknown))
    assert reply["info"]["module"] == []
    assert reply["info"]["result"] == "success"


# -- the status ------------------------------------------------------------------------------------------------------


def test_every_push_is_a_full_status_with_storage_and_send():
    p = pushed()
    assert p["command"] == "push_status" and p["msg"] == 0 and p["sequence_id"] == "5"
    # The plugin's "card" is the server: Orca refuses Print/Send for NO_SDCARD, and a missing key means NO_SDCARD.
    assert p["sdcard"] is True and p["support_send_to_sd"] is True


def test_no_push_claims_a_job_id():
    # A job_id makes Orca's wait_fn wait for a matching id (PRINT_JOB_SENDING_TIMEOUT); BamDude's job is not the
    # printer's.
    assert "job_id" not in pushed()


def test_the_print_task_is_the_servers():
    p = pushed()
    assert p["gcode_state"] == "RUNNING"
    assert p["subtask_name"] == "bracket_v3"
    assert p["mc_percent"] == 42 and isinstance(p["mc_percent"], int)
    assert p["mc_remaining_time"] == 37  # minutes, as the printer reports and Orca multiplies by 60
    assert (p["layer_num"], p["total_layer_num"]) == (12, 220)


@pytest.mark.parametrize("state", ["IDLE", "PREPARE", "RUNNING", "PAUSE", "FINISH", "FAILED", "SLICING"])
def test_known_states_pass_through(state):
    assert pushed(state=state)["gcode_state"] == state


@pytest.mark.parametrize("state", [None, "", "unknown", "OFFLINE", 3])
def test_an_unknown_state_is_not_sent(state):
    assert "gcode_state" not in pushed(state=state)


def test_temperatures_have_the_types_the_host_reads():
    p = pushed()
    assert (p["bed_temper"], p["bed_target_temper"]) == (55.0, 55.0)
    assert (p["nozzle_temper"], p["nozzle_target_temper"]) == (219.6, 220.0)
    assert p["chamber_temper"] == 31.0
    assert all(isinstance(p[k], float) for k in ("bed_temper", "nozzle_temper", "chamber_temper"))
    assert (p["nozzle_diameter"], p["nozzle_type"]) == ("0.4", "hardened_steel")


def test_a_missing_temperature_is_not_sent():
    p = pushed(temperatures={"bed": 55.0})
    assert p["bed_temper"] == 55.0
    for key in ("bed_target_temper", "nozzle_temper", "nozzle_target_temper", "chamber_temper", "ctt"):
        assert key not in p


def test_a_second_nozzle_means_no_nozzle_temperature_at_all():
    # Orca's single-extruder fields name its MAIN (right) extruder; BamDude's index 0 is the left one, and the
    # two-extruder object is read field by field without checks (ParseV2_0). Neither is guessed.
    two = [
        {"nozzle_type": "hardened_steel", "nozzle_flow": "standard", "nozzle_diameter": "0.4"},
        {"nozzle_type": "stainless_steel", "nozzle_flow": "high_flow", "nozzle_diameter": "0.4"},
    ]
    temps = {"bed": 60.0, "nozzle": 220.0, "nozzle_target": 220.0, "nozzle_2": 30.0, "nozzle_2_target": 0.0}
    p = pushed(nozzles=two, temperatures=temps)
    for key in ("nozzle_temper", "nozzle_target_temper", "nozzle_diameter", "nozzle_type", "device"):
        assert key not in p
    assert p["bed_temper"] == 60.0


def test_a_second_nozzle_temperature_alone_also_means_two_nozzles():
    p = pushed(nozzles=[], temperatures={"nozzle": 220.0, "nozzle_2": 30.0})
    assert "nozzle_temper" not in p


def test_progress_is_clamped_and_rounded():
    assert pushed(progress=100.4)["mc_percent"] == 100
    assert pushed(progress=-3)["mc_percent"] == 0
    assert "mc_percent" not in pushed(progress=None)
    assert "mc_percent" not in pushed(progress=True)


@pytest.mark.parametrize("value", [None, -1, "37", 3.5, True])
def test_a_remaining_time_that_is_not_a_whole_number_of_minutes_is_not_sent(value):
    assert "mc_remaining_time" not in pushed(remaining_time=value)


def test_layers_are_sent_only_as_whole_numbers():
    p = pushed(layer_num=None, total_layers="220")
    assert "layer_num" not in p and "total_layer_num" not in p


# -- the AMS ---------------------------------------------------------------------------------------------------------


def test_ams_units_and_trays_are_strings_where_the_host_reads_strings():
    ams = pushed()["ams"]
    (unit,) = ams["ams"]
    assert unit["id"] == "0" and unit["temp"] == "26.5"
    assert unit["humidity_raw"] == "18"  # BamDude's humidity is a percentage: Orca's "humidity" is the 1-5 index
    assert "humidity" not in unit
    first, second, empty, blank = unit["tray"]
    assert first == {
        "id": "0",
        "tray_type": "PLA",
        "tray_color": "FF0000FF",
        "tray_info_idx": "GFA00",
        "tray_sub_brands": "PLA Basic",
        "remain": 80,
        "nozzle_temp_min": "190",
        "nozzle_temp_max": "230",
    }
    assert second == {"id": "1", "tray_type": "PETG", "tray_color": "000000FF", "tray_info_idx": "GFG00"}
    assert empty == {"id": "2"} and blank == {"id": "3"}  # an empty slot is its id alone
    assert ams["tray_now"] == "1"


def test_ams_exist_bits_are_hex_strings_of_the_units_and_loaded_trays():
    ams = pushed()["ams"]
    assert ams["ams_exist_bits"] == "1"
    assert ams["tray_exist_bits"] == "3"  # trays 0 and 1 hold filament


def test_a_second_ams_unit_sets_its_bits():
    unit = {"id": 1, "humidity": None, "temp": None, "tray": [{"id": 2, "tray_type": "PLA", "tray_info_idx": "GFA00"}]}
    ams = pushed(ams=[unit], tray_now=255)["ams"]
    assert ams["ams_exist_bits"] == "2"
    assert ams["tray_exist_bits"] == "40"  # unit 1 tray 2 -> bit 6
    (only,) = ams["ams"]
    assert only == {"id": "1", "tray": [{"id": "2", "tray_type": "PLA", "tray_info_idx": "GFA00"}]}
    assert ams["tray_now"] == "255"


def test_a_tray_without_a_filament_profile_still_names_its_type():
    # Orca takes the type only together with tray_info_idx; an empty profile id keeps the type it can show.
    unit = {"id": 0, "tray": [{"id": 0, "tray_type": "PLA", "tray_info_idx": None, "tray_color": "112233FF"}]}
    (tray,) = pushed(ams=[unit])["ams"]["ams"][0]["tray"]
    assert tray == {"id": "0", "tray_type": "PLA", "tray_info_idx": "", "tray_color": "112233FF"}


def test_an_ams_ht_unit_keeps_its_id_and_sets_no_four_slot_bits():
    unit = {"id": 128, "is_ams_ht": True, "tray": [{"id": 0, "tray_type": "PETG", "tray_info_idx": "GFG00"}]}
    ams = pushed(ams=[unit], tray_now=255)["ams"]
    assert ams["ams"][0]["id"] == "128"
    assert ams["ams_exist_bits"] == "0" and ams["tray_exist_bits"] == "0"


def test_no_ams_means_no_ams_object():
    assert "ams" not in pushed(ams=[], ams_exists=False)


def test_a_malformed_unit_or_tray_is_left_out():
    units = [{"id": "x"}, "nope", {"id": 0, "tray": [{"id": None}, "t", {"id": 1, "tray_type": "PLA"}]}]
    ams = pushed(ams=units)["ams"]
    assert ams["ams"] == [{"id": "0", "tray": [{"id": "1", "tray_type": "PLA", "tray_info_idx": ""}]}]


# -- the external spool ----------------------------------------------------------------------------------------------


def test_the_external_spool_is_the_virtual_tray():
    vt = pushed()["vt_tray"]
    assert vt == {
        "id": "254",
        "tray_type": "TPU",
        "tray_color": "FFFFFFFF",
        "tray_info_idx": "GFU01",
        "remain": 0,
        "nozzle_temp_min": "200",
        "nozzle_temp_max": "250",
    }


def test_no_external_spool_means_no_virtual_tray():
    assert "vt_tray" not in pushed(vt_tray=[])
    assert "vt_tray" not in pushed(vt_tray=[{"id": 254, "tray_type": None}])


# -- a printer BamDude cannot reach ----------------------------------------------------------------------------------


def test_a_printer_the_server_cannot_reach_pushes_only_what_is_not_its_state():
    # The plugin's transport is fresh, so the device stays connected in Orca and can still be scheduled; nothing
    # about the machine itself is known, so nothing about it is said -- not IDLE, not a temperature.
    p = json.loads(push_status(status(connected=False), seq=9))["print"]
    assert p == {"command": "push_status", "msg": 0, "sequence_id": "9", "sdcard": True, "support_send_to_sd": True}


@pytest.mark.parametrize("value", [None, "yes", 1])
def test_connected_must_be_true_itself(value):
    p = json.loads(push_status(status(connected=value), seq=1))["print"]
    assert "gcode_state" not in p and "bed_temper" not in p


def test_a_missing_status_pushes_only_what_is_not_its_state():
    p = json.loads(push_status(None, seq=1))["print"]
    assert set(p) == {"command", "msg", "sequence_id", "sdcard", "support_send_to_sd"}


def test_wrongly_typed_fields_are_dropped_not_converted():
    p = pushed(
        subtask_name=5,
        temperatures={"bed": "55", "bed_target": None, "nozzle": True, "chamber": float("nan")},
        nozzles=[{"nozzle_type": 3, "nozzle_diameter": None}],
        tray_now="1",
    )
    for key in ("subtask_name", "bed_temper", "bed_target_temper", "nozzle_temper", "chamber_temper"):
        assert key not in p
    assert "nozzle_type" not in p and "nozzle_diameter" not in p
    assert "tray_now" not in p["ams"]
