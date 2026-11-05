"""The wheel users install: a fixed name, the package and its licence, nothing else, the same bytes every time."""

import base64
import hashlib
import importlib.util
import zipfile
from pathlib import Path

from orca_bamdude import __version__

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("build_wheel", ROOT / "tools" / "build_wheel.py")
build_wheel = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_wheel)

DIST_INFO = f"orca_bamdude-{__version__}.dist-info"


def _members(path):
    with zipfile.ZipFile(path) as archive:
        return {info.filename: archive.read(info) for info in archive.infolist()}


def test_the_wheel_has_a_fixed_name_and_the_package_version(tmp_path):
    out = build_wheel.build(tmp_path)

    assert out.name == "orca_bamdude_any.whl"
    metadata = _members(out)[f"{DIST_INFO}/METADATA"].decode()
    assert "Name: orca-bamdude\n" in metadata
    assert f"Version: {__version__}\n" in metadata
    assert "License: AGPL-3.0-only\n" in metadata


def test_two_builds_are_byte_identical(tmp_path):
    first = build_wheel.build(tmp_path / "a").read_bytes()
    second = build_wheel.build(tmp_path / "b").read_bytes()

    assert first == second


def test_the_wheel_holds_the_package_and_its_licence_and_nothing_else(tmp_path):
    members = _members(build_wheel.build(tmp_path))
    package = {
        f"orca_bamdude/{p.relative_to(ROOT / 'src' / 'orca_bamdude').as_posix()}"
        for p in (ROOT / "src" / "orca_bamdude").rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    }
    dist_info = {f"{DIST_INFO}/{name}" for name in ("METADATA", "WHEEL", "top_level.txt", "RECORD", "licenses/LICENSE")}

    assert set(members) == package | dist_info
    assert members[f"{DIST_INFO}/licenses/LICENSE"] == (ROOT / "LICENSE").read_bytes()
    assert members[f"{DIST_INFO}/top_level.txt"] == b"orca_bamdude\n"
    assert b"Tag: py3-none-any" in members[f"{DIST_INFO}/WHEEL"]


def test_record_lists_every_member_with_its_hash(tmp_path):
    members = _members(build_wheel.build(tmp_path))
    record = members.pop(f"{DIST_INFO}/RECORD").decode()
    rows = [line.split(",") for line in record.splitlines() if line]

    listed = {name: (digest, size) for name, digest, size in rows}
    assert listed.pop(f"{DIST_INFO}/RECORD") == ("", "")
    assert set(listed) == set(members)
    for name, data in members.items():
        expected = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        assert listed[name] == (f"sha256={expected}", str(len(data)))


def test_every_member_has_the_same_timestamp_and_mode(tmp_path):
    with zipfile.ZipFile(build_wheel.build(tmp_path)) as archive:
        infos = archive.infolist()

    names = [info.filename for info in infos]
    assert names[-1] == f"{DIST_INFO}/RECORD"  # last, as the wheel spec recommends
    assert names[:-1] == sorted(names[:-1])
    assert {info.date_time for info in infos} == {(1980, 1, 1, 0, 0, 0)}
    assert {info.external_attr >> 16 for info in infos} == {0o100644}
