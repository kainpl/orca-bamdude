"""What a release tag must agree with before anything is published: ``python tools/release.py prepare --tag vX``.

A release is a ``v<version>`` tag whose version equals ``__version__`` in the package and has its own section in
CHANGELOG.md (``## [<version>] - <date>``); that section becomes the release notes. ``a``/``b``/``rc`` versions are
pre-releases and are titled so, as BamDude's are: ``OrcaBamDude v0.2.0b1 (pre-release)``. ``prepare`` writes the
notes to a file and prints ``key=value`` lines for ``$GITHUB_OUTPUT``; anything that does not agree is an error and
nothing is released.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:(a|b|rc)\d+)?$")


class ReleaseError(Exception):
    pass


def package_version(root: Path = ROOT) -> str:
    text = (root / "src" / "orca_bamdude" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "([^"]+)"$', text, re.MULTILINE)
    if match is None:
        raise ReleaseError("no __version__ in src/orca_bamdude/__init__.py")
    return match.group(1)


def is_prerelease(version: str) -> bool:
    match = _VERSION.match(version)
    if match is None:
        raise ReleaseError(f"version {version!r} is not X.Y.Z or X.Y.Z(a|b|rc)N")
    return match.group(1) is not None


def check_tag(tag: str, version: str) -> None:
    if tag != f"v{version}":
        raise ReleaseError(f"tag {tag!r} does not name the package version {version!r} (expected v{version})")


def title(version: str) -> str:
    return f"OrcaBamDude v{version}" + (" (pre-release)" if is_prerelease(version) else "")


def notes(changelog: str, version: str) -> str:
    heading = re.compile(rf"^## \[{re.escape(version)}\](?: - .*)?$", re.MULTILINE)
    match = heading.search(changelog)
    if match is None:
        raise ReleaseError(f"CHANGELOG.md has no section for {version}")
    following = re.compile(r"^## \[", re.MULTILINE).search(changelog, match.end())
    body = changelog[match.end() : following.start() if following else len(changelog)].strip()
    if not body:
        raise ReleaseError(f"CHANGELOG.md's section for {version} is empty")
    return body


def prepare(tag: str, notes_path: Path) -> dict[str, str]:
    version = package_version()
    check_tag(tag, version)
    text = notes(Path(ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), version)
    notes_path.write_text(text + "\n", encoding="utf-8")
    return {"version": version, "title": title(version), "prerelease": str(is_prerelease(version)).lower()}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("version", help="print the package version")
    prep = sub.add_parser("prepare", help="check a tag and write its release notes")
    prep.add_argument("--tag", required=True)
    prep.add_argument("--notes", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.command == "version":
        print(package_version())
        return 0
    try:
        outputs = prepare(args.tag, args.notes)
    except ReleaseError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    for key, value in outputs.items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
