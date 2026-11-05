"""Build the plugin wheel with the standard library only: ``python tools/build_wheel.py [out_dir]``.

Writes ``dist/orca_bamdude_any.whl`` -- the file users install through OrcaSlicer's Plugins dialog (Install local
plugin) and the file a release attaches. OrcaSlicer reads the plugin's name, version and summary from METADATA and
imports the package named in top_level.txt; it does not parse the file name.

The name deliberately carries no version: OrcaSlicer keys a locally installed plugin by its file name, and that key
labels the plugin's saved settings, storage folder and granted permissions, so a versioned name would lose them on
every update. "_any" says the wheel is pure Python and fits any OS. (pip will not install a file named like this;
copy it to ``orca_bamdude-<version>-py3-none-any.whl`` for that.)

Reproducible: members are sorted, every timestamp and mode is fixed, RECORD is written last from the exact bytes, so
the same sources and the same Python give the same file, byte for byte.
"""

from __future__ import annotations

import base64
import hashlib
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "orca_bamdude"
WHEEL_NAME = "orca_bamdude_any.whl"
TIMESTAMP = (1980, 1, 1, 0, 0, 0)  # the earliest a ZIP can hold
FILE_MODE = 0o100644 << 16  # a regular, non-executable file

SUMMARY = "Send sliced plates from OrcaSlicer to a BamDude print farm and watch its printers."


def package_version() -> str:
    text = (PACKAGE / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "([^"]+)"$', text, re.MULTILINE)
    if match is None:
        raise SystemExit("no __version__ in src/orca_bamdude/__init__.py")
    return match.group(1)


def _metadata(version: str) -> str:
    return (
        "Metadata-Version: 2.1\n"
        "Name: orca-bamdude\n"
        f"Version: {version}\n"
        f"Summary: {SUMMARY}\n"
        "Home-page: https://github.com/kainpl/orca-bamdude\n"
        "Author: Valeriy V. Pushkar\n"
        "License: AGPL-3.0-only\n"
        "Requires-Python: >=3.12\n"
    )


def _wheel() -> str:
    return (
        "Wheel-Version: 1.0\nGenerator: orca-bamdude tools/build_wheel.py\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    )


def _record_line(name: str, data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"{name},sha256={digest},{len(data)}"


def _sources() -> dict[str, bytes]:
    files = sorted(p for p in PACKAGE.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    files = [p for p in files if p.suffix not in (".pyc", ".pyo")]
    return {f"orca_bamdude/{p.relative_to(PACKAGE).as_posix()}": p.read_bytes() for p in files}


def build(out_dir: Path | str | None = None) -> Path:
    version = package_version()
    dist_info = f"orca_bamdude-{version}.dist-info"
    files = _sources()
    files[f"{dist_info}/METADATA"] = _metadata(version).encode()
    files[f"{dist_info}/WHEEL"] = _wheel().encode()
    files[f"{dist_info}/top_level.txt"] = b"orca_bamdude\n"
    files[f"{dist_info}/licenses/LICENSE"] = (ROOT / "LICENSE").read_bytes()
    record_name = f"{dist_info}/RECORD"
    record = "".join(_record_line(name, files[name]) + "\n" for name in sorted(files)) + f"{record_name},,\n"

    out_dir = Path(out_dir) if out_dir is not None else ROOT / "dist"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / WHEEL_NAME
    partial = out.with_suffix(".whl.part")
    with zipfile.ZipFile(partial, "w") as archive:
        for name in [*sorted(files), record_name]:
            info = zipfile.ZipInfo(name, date_time=TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = FILE_MODE
            info.create_system = 3  # Unix, so the mode above is what readers see on every OS
            archive.writestr(info, files[name] if name != record_name else record.encode(), compresslevel=9)
    partial.replace(out)
    return out


if __name__ == "__main__":
    print(build(sys.argv[1] if len(sys.argv) > 1 else None))
