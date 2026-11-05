"""What a release tag must agree with before anything is published."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("release", ROOT / "tools" / "release.py")
release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release)

CHANGELOG = """# Changelog

## [Unreleased]

- Something not yet released.

## [0.2.0b1] - 2026-11-20

- The printer agent.
- The camera.

## [0.1.0] - 2026-11-10

- The core.
"""


def test_the_tag_must_name_the_package_version():
    release.check_tag("v0.2.0b1", "0.2.0b1")
    for tag in ("0.2.0b1", "v0.2.0", "v0.2.0b1-fix", "orca-plugin-0.2.0b1"):
        with pytest.raises(release.ReleaseError):
            release.check_tag(tag, "0.2.0b1")


@pytest.mark.parametrize(
    "version,pre", [("0.1.0", False), ("1.2.3", False), ("0.2.0b1", True), ("0.2.0rc2", True), ("0.2.0a1", True)]
)
def test_alpha_beta_and_rc_are_prereleases(version, pre):
    assert release.is_prerelease(version) is pre


def test_a_version_outside_the_scheme_is_refused():
    for version in ("0.2", "0.2.0-beta", "v0.2.0", "0.2.0.dev1"):
        with pytest.raises(release.ReleaseError):
            release.is_prerelease(version)


def test_the_title_follows_the_bamdude_naming():
    assert release.title("0.1.0") == "OrcaBamDude v0.1.0"
    assert release.title("0.2.0b1") == "OrcaBamDude v0.2.0b1 (pre-release)"


def test_the_notes_are_the_versions_changelog_section():
    assert release.notes(CHANGELOG, "0.2.0b1") == "- The printer agent.\n- The camera."
    assert release.notes(CHANGELOG, "0.1.0") == "- The core."


def test_a_version_without_its_changelog_section_is_refused():
    with pytest.raises(release.ReleaseError):
        release.notes(CHANGELOG, "0.3.0")
    with pytest.raises(release.ReleaseError):
        release.notes("# Changelog\n\n## [0.3.0] - 2026-12-01\n\n## [0.2.0]\n", "0.3.0")  # empty section


def test_the_package_version_is_read_from_the_source():
    from orca_bamdude import __version__

    assert release.package_version(ROOT) == __version__
