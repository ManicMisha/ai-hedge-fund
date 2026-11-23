"""The desk page: local only, read-only, and honest about what is filled in."""

import http.client
import json
import threading

import pytest

from desk.web import server


@pytest.fixture()
def running():
    srv = server.make_server(port=0)
    port = srv.server_address[1]
    # make_server built its Host allow-list for port 0; rebuild it for the real port.
    srv.RequestHandlerClass = server.make_handler(port)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield port
    srv.shutdown()
    srv.server_close()


def get(port, path, host=None, method="GET"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request(method, path, headers={"Host": host or f"localhost:{port}"})
    r = c.getresponse()
    return r.status, r.read()


def test_binds_to_this_computer_only():
    srv = server.make_server(port=0)
    try:
        assert srv.server_address[0] == "127.0.0.1"
    finally:
        srv.server_close()


def test_state_reports_the_empty_config(running):
    status, body = get(running, "/api/state")
    assert status == 200
    st = json.loads(body)
    assert st["ok"] and st["filled"] == 0 and st["total"] == 32
    assert len(st["params"]) == 32 and all(p["status"] == "empty" for p in st["params"])
    assert [a["name"] for a in st["agents"]][0] == "Desk Lead"
    assert sum(a["order_tools"] for a in st["agents"]) == 1


def test_page_and_font_are_served(running):
    status, body = get(running, "/")
    assert status == 200 and b"Paper trading" in body and b"Simulated money only" in body
    assert get(running, "/static/fonts/PixelifySans-latin.woff2")[0] == 200


def test_foreign_host_is_refused(running):
    assert get(running, "/api/state", host="evil.example")[0] == 403


def test_no_path_escapes_the_static_folder(running):
    assert get(running, "/static/../server.py")[0] == 404
    assert get(running, "/static/%2e%2e/server.py")[0] == 404


def test_page_is_read_only(running):
    assert get(running, "/api/state", method="POST")[0] == 501
