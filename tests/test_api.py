"""The connector contract as the plugin sends it."""

from fakes import json_answer

from orca_bamdude.api import ConnectorClient

MJPEG = "multipart/x-mixed-replace; boundary=frame"


def test_a_camera_is_opened_for_one_printer_and_its_identity(serve):
    server, transport = serve(lambda m, p: (200, {"Content-Type": MJPEG}, iter([b"--frame\r\n"])))

    stream = ConnectorClient(transport).open_camera(7, "uuid-7")
    try:
        assert stream.content_type == MJPEG
    finally:
        stream.close()

    seen = server.seen[0]
    assert seen["path"] == "/base/api/v1/printers/7/camera/stream?fps=5"
    assert seen["headers"]["X-BamDude-Printer-Identity"] == "uuid-7"


def test_status_is_asked_for_at_most_a_hundred_printers(serve):
    server, transport = serve(lambda m, p: json_answer(200, {"7": {"state": "IDLE"}}))

    answer = ConnectorClient(transport).status_batch(list(range(1, 150)))

    assert answer == {7: {"state": "IDLE"}}
    assert server.seen[0]["path"].count("ids=") == 100
