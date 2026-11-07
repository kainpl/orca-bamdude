"""The tab's handlers end to end on the fake OrcaSlicer, against a scripted BamDude on 127.0.0.1 (spec r2 §8; plan r2
H4): the page asks, the UI thread answers at once, the plugin's threads read and post back -- and no secret reaches
the page."""

import base64
import json
import sys
from urllib.parse import urlsplit

import pytest
from fake_orca import Host
from fakes import json_answer
from test_agent import AGENT, connected, wait_for
from test_agent_send import SendFarm

KEY = "bd_test_key_SENTINEL"
JPEG = b"\xff\xd8\xff\xe0 a picture \xff\xd9"


class TabFarm(SendFarm):
    def route(self, method, path):
        parts = urlsplit(path)
        if parts.path == "/base/api/v1/connector/capabilities":
            status, headers, payload = super().route(method, path)
            body = json.loads(payload)
            body["features"].update(camera_snapshot=True)
            return json_answer(200, body)
        if parts.path == "/base/api/v1/printers/1/camera/snapshot":
            return 200, {"Content-Type": "image/jpeg"}, JPEG
        if parts.path.startswith("/base/api/v1/connector/submissions/") and method == "GET":
            return json_answer(200, {"operation_id": parts.path.rsplit("/", 1)[1], "state": "complete", "entries": []})
        return super().route(method, path)


@pytest.fixture
def farm():
    farm = TabFarm()
    yield farm
    farm.server.close()


@pytest.fixture
def host(tmp_path, farm):
    host = Host(tmp_path / "data")
    host.install()
    exports = tmp_path / "exports"
    exports.mkdir()
    (exports / "bracket.gcode.3mf").write_bytes(b"PK\x03\x04 not a real project")
    host.write_base(AGENT, {"server_url": farm.server.url + "/base", "api_key": KEY, "export_dir": str(exports)})
    yield host
    host.close()


def adapter():
    return sys.modules["orca_bamdude.orca_adapter"]


def posts(host, kind):
    return [p for p in host.page_posts if p.get("kind") == kind]


def test_ready_tells_the_page_its_state_and_the_lease_without_a_secret(host, farm):
    connected(host)
    host.page_message({"op": "ready"})
    assert wait_for(lambda: posts(host, "state") and posts(host, "lease"))
    assert posts(host, "lease")[-1]["on"] is False
    assert KEY not in json.dumps(host.page_posts)


def test_live_monitoring_reads_the_fleet_until_it_is_switched_off(host, farm):
    connected(host)
    host.page_message({"op": "live", "on": True})
    assert wait_for(lambda: posts(host, "fleet"))
    names = {row["name"] for row in posts(host, "fleet")[-1]["rows"]}
    assert "X1C left" in names
    host.page_message({"op": "live", "on": False})
    assert posts(host, "lease")[-1]["on"] is False


def test_an_action_while_live_monitoring_is_off_does_not_switch_it_on(host, farm):
    connected(host)
    host.page_message({"op": "activity"})
    host.page_message({"op": "pulse"})
    assert all(p["on"] is False for p in posts(host, "lease"))


def test_refresh_reads_the_fleet_once(host, farm):
    connected(host)
    host.page_message({"op": "refresh"})
    assert wait_for(lambda: posts(host, "fleet"))


def test_the_connection_check_reports_each_step(host, farm):
    connected(host)
    host.page_message({"op": "test"})
    assert wait_for(lambda: posts(host, "test"))
    steps = posts(host, "test")[-1]["steps"]
    assert [s["step"] for s in steps] == ["permission", "reach", "protocol", "key", "printers", "features"]
    assert all(s["ok"] for s in steps)


def test_the_history_lists_records_and_reconciles_one(host, farm):
    from orca_bamdude.journal import Journal

    connected(host)
    journal = Journal(str(host.storage_dir), ("server-1", "epoch-1", "actor-1"))
    record = journal.create("part.gcode.3mf", "a" * 64, origin="native", plates=[1])
    journal.update(record.operation_id, lambda r: (setattr(r, "phase", "unknown"), setattr(r, "body", {"x": 1})))
    host.page_message({"op": "history"})
    assert wait_for(lambda: posts(host, "history"))
    (row,) = [r for r in posts(host, "history")[-1]["rows"] if r["operation_id"] == record.operation_id]
    assert row["actions"] == ["reconcile"]
    host.page_message({"op": "history_action", "operation_id": record.operation_id, "action": "reconcile"})
    assert wait_for(lambda: journal.load(record.operation_id).phase == "accepted")


def test_the_export_folder_is_listed_by_tokens(host, farm, tmp_path):
    connected(host)
    host.page_message({"op": "exports"})
    assert wait_for(lambda: posts(host, "exports"))
    (row,) = posts(host, "exports")[-1]["rows"]
    assert row["relative"] == "bracket.gcode.3mf" and row["token"]
    assert str(tmp_path) not in json.dumps(posts(host, "exports"))


def test_a_camera_snapshot_comes_to_the_page_as_a_data_url(host, farm):
    connected(host)
    host.page_message({"op": "snapshot", "printer_id": 1})
    assert wait_for(lambda: posts(host, "snapshot"))
    shot = posts(host, "snapshot")[-1]
    assert shot["printer_id"] == 1
    assert shot["data_url"] == "data:image/jpeg;base64," + base64.b64encode(JPEG).decode()


def test_open_bamdude_opens_exactly_the_declared_dashboard(host, farm, monkeypatch):
    connected(host)
    opened = []
    monkeypatch.setattr(adapter(), "_launch", opened.append)
    host.page_message({"op": "open_bamdude"})
    assert opened == [farm.server.url + "/base/"]


def test_a_dashboard_not_yet_allowed_is_not_opened(host, farm, monkeypatch):
    connected(host)
    opened = []
    monkeypatch.setattr(adapter(), "_launch", opened.append)
    monkeypatch.setitem(adapter().REQUEST, "process", [])
    host.page_message({"op": "open_bamdude"})
    assert opened == [] and posts(host, "notice")[-1]["code"] == "dashboard_needs_restart"


def test_on_an_unsupported_orcaslicer_the_history_neither_offers_nor_runs_a_writer(tmp_path):
    # H4 review R4 (Codex's reproduction): spec r2 §10 -- resume and "send as new" are writers, for 2.5.0-rc only.
    import uuid

    from orca_bamdude.journal import Journal

    farm = TabFarm()
    host = Host(tmp_path / "data")
    try:
        host.install()
        host.write_base(AGENT, {"server_url": farm.server.url + "/base", "api_key": KEY})
        real_info = host.orca.host.app_info
        host.orca.host.app_info = lambda: {**real_info(), "version": "2.5.1"}
        connected(host)
        assert adapter().RUNTIME.host_writable is False
        journal = Journal(str(host.storage_dir), ("server-1", "epoch-1", "actor-1"))
        record = journal.create("part.gcode.3mf", "a" * 64, origin="native", plates=[1])

        def saved(r):
            r.phase, r.library_file_id = "uploaded", 41
            r.body = {"operation_id": r.operation_id, "entries": [{"entry_id": str(uuid.uuid4()), "plate_id": 1}]}

        journal.update(record.operation_id, saved)
        host.page_message({"op": "history"})
        assert wait_for(lambda: posts(host, "history"))
        history = posts(host, "history")[-1]
        (row,) = [r for r in history["rows"] if r["operation_id"] == record.operation_id]
        assert "resume" not in row["actions"] and "send_as_new" not in row["actions"]
        assert history["writable"] is False
        host.page_message({"op": "history_action", "operation_id": record.operation_id, "action": "resume"})
        assert wait_for(lambda: posts(host, "error"))
        assert posts(host, "error")[-1]["code"] == "host_version"
        assert not [r for r in farm.requests("/connector/submissions") if r["method"] == "POST"]
    finally:
        host.close()
        farm.server.close()
