"""The export folder (spec r2 §8.4): the plugin lists it itself, the page gets one-time tokens, never a path, and a
token opens only the file it was issued for -- unchanged, inside the folder, in the same connection generation."""

import os
import sys

import pytest

from orca_bamdude.exports import ExportError, Exports


def _tree(tmp_path):
    root = tmp_path / "exports"
    (root / "jobs" / "deep" / "deeper" / "deepest").mkdir(parents=True)
    (root / "a.3mf").write_bytes(b"PK one")
    (root / "jobs" / "b.gcode.3mf").write_bytes(b"PK two")
    (root / "jobs" / "deep" / "deeper" / "deepest" / "c.3mf").write_bytes(b"PK too deep")
    (root / "notes.txt").write_text("not a plate")
    return root


def _clock():
    now = [100.0]
    return now, lambda: now[0]


def test_the_listing_holds_only_plain_3mf_files_within_its_depth(tmp_path):
    root = _tree(tmp_path)
    listed = Exports().list(str(root), generation=1)
    assert sorted(item.relative for item in listed) == ["a.3mf", os.path.join("jobs", "b.gcode.3mf")]
    assert all(len(item.token) >= 20 for item in listed) and len({i.token for i in listed}) == 2


def test_the_listing_is_bounded(tmp_path):
    root = tmp_path / "many"
    root.mkdir()
    for index in range(30):
        (root / f"{index:02}.3mf").write_bytes(b"PK")
    assert len(Exports(max_files=10).list(str(root), generation=1)) == 10


def test_a_token_opens_its_file_once(tmp_path):
    root = _tree(tmp_path)
    exports = Exports()
    (item,) = [i for i in exports.list(str(root), generation=1) if i.relative == "a.3mf"]
    assert exports.resolve(item.token, str(root), generation=1) == os.path.realpath(root / "a.3mf")
    with pytest.raises(ExportError) as again:
        exports.resolve(item.token, str(root), generation=1)
    assert again.value.code == "token_used"


@pytest.mark.parametrize(
    "spoil,code",
    [
        (lambda e, t, root, now: ("forged-token", str(root), 1), "token_unknown"),
        (lambda e, t, root, now: (t, str(root), 2), "token_stale"),  # a new connection generation
        (lambda e, t, root, now: (t, str(root.parent), 1), "token_stale"),  # the folder setting changed
        (lambda e, t, root, now: now.__setitem__(0, 100.0 + 3600) or (t, str(root), 1), "token_expired"),
    ],
)
def test_a_token_that_is_not_this_ones_opens_nothing(tmp_path, spoil, code):
    root = _tree(tmp_path)
    now, clock = _clock()
    exports = Exports(clock=clock)
    (item,) = [i for i in exports.list(str(root), generation=1) if i.relative == "a.3mf"]
    with pytest.raises(ExportError) as refused:
        exports.resolve(*spoil(exports, item.token, root, now))
    assert refused.value.code == code


def test_a_file_changed_after_the_listing_is_refused(tmp_path):
    root = _tree(tmp_path)
    exports = Exports()
    (item,) = [i for i in exports.list(str(root), generation=1) if i.relative == "a.3mf"]
    (root / "a.3mf").write_bytes(b"PK replaced with other bytes")
    with pytest.raises(ExportError) as refused:
        exports.resolve(item.token, str(root), generation=1)
    assert refused.value.code == "changed"


def _link_or_skip(link, target, directory=False):
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (OSError, NotImplementedError):
        pytest.skip("this account may not create symbolic links")


def test_a_symlink_in_the_folder_is_not_listed(tmp_path):
    root = _tree(tmp_path)
    outside = tmp_path / "outside.3mf"
    outside.write_bytes(b"PK secret")
    _link_or_skip(root / "link.3mf", outside)
    assert "link.3mf" not in [i.relative for i in Exports().list(str(root), generation=1)]


def test_a_file_swapped_for_a_link_outside_after_the_listing_is_refused(tmp_path):
    root = _tree(tmp_path)
    exports = Exports()
    (item,) = [i for i in exports.list(str(root), generation=1) if i.relative == "a.3mf"]
    outside = tmp_path / "outside.3mf"
    outside.write_bytes(b"PK one")  # the same size, so only the link check can catch it
    os.unlink(root / "a.3mf")
    _link_or_skip(root / "a.3mf", outside)
    with pytest.raises(ExportError) as refused:
        exports.resolve(item.token, str(root), generation=1)
    assert refused.value.code in ("outside_root", "not_a_file")


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows'")
def test_a_junction_to_another_folder_is_not_followed(tmp_path):
    import _winapi

    root = _tree(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "secret.3mf").write_bytes(b"PK secret")
    _winapi.CreateJunction(str(elsewhere), str(root / "junction"))
    listed = [i.relative for i in Exports().list(str(root), generation=1)]
    assert not any(name.startswith("junction") for name in listed)


def test_a_folder_that_does_not_exist_lists_nothing(tmp_path):
    assert Exports().list(str(tmp_path / "missing"), generation=1) == []


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows'")
def test_a_folder_swapped_for_a_junction_outside_after_the_listing_is_refused(tmp_path):
    import _winapi
    import shutil

    root = _tree(tmp_path)
    exports = Exports()
    (item,) = [i for i in exports.list(str(root), generation=1) if i.relative.endswith("b.gcode.3mf")]
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    shutil.copy2(root / "jobs" / "b.gcode.3mf", elsewhere / "b.gcode.3mf")  # same bytes, same time
    shutil.rmtree(root / "jobs")
    _winapi.CreateJunction(str(elsewhere), str(root / "jobs"))
    with pytest.raises(ExportError) as refused:
        exports.resolve(item.token, str(root), generation=1)
    assert refused.value.code == "outside_root"
