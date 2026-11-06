"""BamDude facts in the shapes OrcaSlicer 2.5.0-rc reads from a printer (host spec r2.3.2 §5.1-§5.2).

Three messages: the SSDP announcement of a device, the ``get_version`` reply and the ``push_status`` report. Each key
and its JSON type is the one the rc parser reads -- ids, AMS temperatures and the exist bits are strings there, the
temperatures numbers -- because a wrong type throws inside the host's parser and loses the whole report.

What BamDude does not know is not sent: no state, temperature or filament is invented, and a key left out keeps
Orca's previous value or its default. Three keys are always sent because the host needs them to offer Print and
Send at all, and they are true of the plugin rather than of the machine: a full report (``msg: 0``), a card
(``sdcard``; the plugin's card is the server) and sending to it (``support_send_to_sd``). A report never carries a
``job_id``: that would make Orca wait for the printer to echo an id BamDude's queue never gives it.

Only the single-extruder fields are used. A printer with a second nozzle gets no nozzle temperature at all: Orca's
single fields name its main (right) extruder while BamDude's first nozzle is the left one, and the two-extruder object
(``device.extruder``) is read by the host key by key without checks, so a partial one would throw. For the same reason
nothing is placed on a side BamDude did not name: on two nozzles an AMS unit is sent only with the extruder the printer
reported for it (``info``), and the external spools not at all -- a single ``vt_tray`` is always Orca's main holder.

The AMS block follows rc ``DevFilaSystemParser::ParseV1_0``. A unit's type is ``info`` bits 0-3 (AMS 1, AMS 2 Pro 3,
AMS HT 4; with no ``info`` Orca keeps its own default, AMS or AMS Lite), its extruder bits 8-11 (0 = main). A plain
unit's presence bit is its id, a slot's ``id * 4 + slot``; an AMS HT's are ``4 + (id - 128)`` and ``16 + (id - 128)``.
A slot is present by the firmware's own bit (BamDude's ``exists``), as the printer would send it; only without it does
BamDude's own fallback decide. A filament profile never decides presence, and a present slot without one gets none.
"""

from __future__ import annotations

import json
import math
from urllib.parse import urlsplit

from .devices import Binding, Device

KNOWN_STATES = frozenset({"IDLE", "PREPARE", "RUNNING", "PAUSE", "FINISH", "FAILED", "SLICING"})
PLAIN_UNITS = 4  # four-slot units 0-3: unit bit id, slot bit id * 4 + slot (rc ParseV1_0, type < 4)
HT_FIRST, HT_UNITS = 128, 8  # AMS HT 128-135: unit bit 4 + (id - 128), slot bit 16 + (id - 128)
AMS_TYPES = {"ams": 1, "n3f": 3, "n3s": 4}  # BamDude's module_type -> rc DevAms type (DevDefs.h)
EXTRUDERS = (0, 1)  # the printer's own ids, which BamDude's ams_extruder_map keeps: 0 = right/main, 1 = left
EMPTY_STATES = (9, 10)  # BamDude's fallback without a presence bit (frontend amsHelpers.getEmptySlotKind)


def ssdp(device: Device, binding: Binding) -> str:
    """The announcement ``DeviceManager::on_machine_alive`` requires: every key a string."""
    return json.dumps(
        {
            "dev_id": device.dev_id,
            "dev_name": device.name,
            "dev_ip": urlsplit(binding.address).hostname or "",
            "dev_type": device.model_code or "",
            "dev_signal": "0",
            "connect_type": "lan",
            "bind_state": "free",
        }
    )


def version_reply(firmware) -> str:
    """The ``get_version`` answer: the ``ota`` module only with the firmware BamDude actually read from the printer."""
    module = []
    if isinstance(firmware, str) and firmware.strip():
        module.append({"name": "ota", "sw_ver": firmware.strip()})
    return json.dumps({"info": {"command": "get_version", "result": "success", "module": module}})


def _int(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _text(value) -> str | None:
    return value if isinstance(value, str) and value else None


def _put(out: dict, key: str, value) -> None:
    if value is not None:
        out[key] = value


def _tray(entry) -> dict | None:
    """One slot as the AMS and virtual-tray parsers read it; an empty slot is its id alone."""
    if not isinstance(entry, dict):
        return None
    tray_id = _int(entry.get("id"))
    if tray_id is None or tray_id < 0:
        return None
    out: dict = {"id": str(tray_id)}
    kind = _text(entry.get("tray_type"))
    if kind is None:
        return out
    out["tray_type"] = kind
    out["tray_info_idx"] = _text(entry.get("tray_info_idx")) or ""  # Orca reads the type only beside the profile
    _put(out, "tray_color", _text(entry.get("tray_color")))
    _put(out, "tray_sub_brands", _text(entry.get("tray_sub_brands")))
    remain = _int(entry.get("remain"))
    if remain is not None and remain >= 0:
        out["remain"] = remain
    for key in ("nozzle_temp_min", "nozzle_temp_max"):
        value = _int(entry.get(key))
        if value is not None:
            out[key] = str(value)
    return out


def _present(entry: dict) -> bool:
    """A slot holds a spool: the firmware's bit when BamDude has it, else BamDude's own fallback in its UI's order -- a
    type is a loaded slot, state 9 or 10 an empty one, anything else a spool it cannot name ("?")."""
    exists = entry.get("exists")
    if isinstance(exists, bool):
        return exists
    if _text(entry.get("tray_type")) is not None:
        return True
    return _int(entry.get("state")) not in EMPTY_STATES


def _unit_type(entry: dict, unit_id: int) -> int | None:
    if HT_FIRST <= unit_id < HT_FIRST + HT_UNITS:
        return AMS_TYPES["n3s"]
    module = entry.get("module_type")
    return AMS_TYPES.get(module.lower()) if isinstance(module, str) else None


def _ams(units, tray_now, extruders, two_nozzles: bool) -> dict | None:
    found = []
    unit_bits = tray_bits = 0
    for entry in units if isinstance(units, list) else []:
        if not isinstance(entry, dict):
            continue
        unit_id = _int(entry.get("id"))
        if unit_id is None or unit_id < 0:
            continue
        ht = HT_FIRST <= unit_id < HT_FIRST + HT_UNITS
        kind = _unit_type(entry, unit_id)
        unit: dict = {"id": str(unit_id)}
        if two_nozzles:
            extruder = extruders.get(str(unit_id)) if isinstance(extruders, dict) else None
            if kind is None or isinstance(extruder, bool) or extruder not in EXTRUDERS:
                continue  # which extruder it feeds, or what it is, is not known: it is not put on Orca's main one
            unit["info"] = format(kind | (extruder << 8), "x")
        elif kind is not None and kind != AMS_TYPES["ams"]:
            unit["info"] = format(kind, "x")  # on the main extruder; a plain AMS keeps Orca's own default
        temp = _number(entry.get("temp"))
        if temp is not None:
            unit["temp"] = str(temp)
        humidity = _int(entry.get("humidity"))
        if humidity is not None:
            unit["humidity_raw"] = str(humidity)  # a percentage; Orca's "humidity" is the 1-5 index
        slots = [(raw, _tray(raw)) for raw in entry.get("tray") or []]
        slots = [(raw, tray) for raw, tray in slots if tray is not None]
        unit["tray"] = [tray for _raw, tray in slots]
        if ht:
            unit_bits |= 1 << (4 + unit_id - HT_FIRST)
            if any(int(tray["id"]) == 0 and _present(raw) for raw, tray in slots):
                tray_bits |= 1 << (16 + unit_id - HT_FIRST)
        elif unit_id < PLAIN_UNITS:
            unit_bits |= 1 << unit_id
            for raw, tray in slots:
                slot = int(tray["id"])
                if slot < 4 and _present(raw):
                    tray_bits |= 1 << (unit_id * 4 + slot)
        found.append(unit)
    if not found:
        return None
    out = {"ams": found, "ams_exist_bits": format(unit_bits, "x"), "tray_exist_bits": format(tray_bits, "x")}
    now = _int(tray_now)
    if now is not None and now >= 0:
        out["tray_now"] = str(now)
    return out


def _single_nozzle(status: dict) -> dict | None:
    """The one nozzle's facts, or ``None`` when the printer has two (or says nothing)."""
    temps = status.get("temperatures") if isinstance(status.get("temperatures"), dict) else {}
    nozzles = status.get("nozzles") if isinstance(status.get("nozzles"), list) else []
    if len(nozzles) > 1 or "nozzle_2" in temps or "nozzle_2_target" in temps:
        return None
    return nozzles[0] if nozzles and isinstance(nozzles[0], dict) else {}


def push_status(status, *, seq: int) -> str:
    """One full report of a printer as BamDude's status describes it."""
    p: dict = {"command": "push_status", "msg": 0, "sequence_id": str(seq), "sdcard": True, "support_send_to_sd": True}
    if not isinstance(status, dict) or status.get("connected") is not True:
        return json.dumps({"print": p})  # nothing about the machine is known: nothing about it is said

    state = status.get("state")
    if isinstance(state, str) and state in KNOWN_STATES:
        p["gcode_state"] = state
    _put(p, "subtask_name", status.get("subtask_name") if isinstance(status.get("subtask_name"), str) else None)
    progress = _number(status.get("progress"))
    if progress is not None:
        p["mc_percent"] = max(0, min(100, round(progress)))
    remaining = _int(status.get("remaining_time"))
    if remaining is not None and remaining >= 0:
        p["mc_remaining_time"] = remaining  # minutes, as the printer reports them
    layer, layers = _int(status.get("layer_num")), _int(status.get("total_layers"))
    _put(p, "layer_num", layer if layer is not None and layer >= 0 else None)
    _put(p, "total_layer_num", layers if layers is not None and layers >= 0 else None)

    temps = status.get("temperatures") if isinstance(status.get("temperatures"), dict) else {}
    _put(p, "bed_temper", _number(temps.get("bed")))
    _put(p, "bed_target_temper", _number(temps.get("bed_target")))
    _put(p, "chamber_temper", _number(temps.get("chamber")))
    _put(p, "ctt", _number(temps.get("chamber_target")))
    nozzle = _single_nozzle(status)
    if nozzle is not None:
        _put(p, "nozzle_temper", _number(temps.get("nozzle")))
        _put(p, "nozzle_target_temper", _number(temps.get("nozzle_target")))
        _put(p, "nozzle_diameter", _text(nozzle.get("nozzle_diameter")))
        _put(p, "nozzle_type", _text(nozzle.get("nozzle_type")))

    holders = status.get("vt_tray") if isinstance(status.get("vt_tray"), list) else []
    two_nozzles = nozzle is None or len(holders) > 1  # two external holders are two extruders' feeds
    _put(p, "ams", _ams(status.get("ams"), status.get("tray_now"), status.get("ams_extruder_map"), two_nozzles))
    if not two_nozzles and holders:
        external = _tray(holders[0])
        _put(p, "vt_tray", external if external is not None and "tray_type" in external else None)
    return json.dumps({"print": p})


__all__ = ["KNOWN_STATES", "push_status", "ssdp", "version_reply"]
