"""Shared daemon + the thin `cogvault mcp` shim that forwards to it."""
import json
import os
import shutil
import tempfile
import threading

import pytest

import cogvault.core as core
from cogvault.core import Vault, Config
from cogvault.daemon import DaemonClient, DaemonUnavailable, make_server
from cogvault.mcp_server import MCPServer


@pytest.fixture
def tmpvault():
    d = tempfile.mkdtemp()
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def sock(monkeypatch):
    # macOS caps AF_UNIX paths at 104 bytes; pytest's tmp_path is longer.
    d = tempfile.mkdtemp(dir="/tmp")
    path = os.path.join(d, "d.sock")
    monkeypatch.setenv("COGVAULT_SOCKET", path)
    yield path
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def daemon(sock):
    srv = make_server(sock)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def _write(d, name, text):
    with open(os.path.join(d, name), "w") as f:
        f.write(text)


def _recall(srv, query):
    resp = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "cogvault_recall", "arguments": {"query": query}}})
    return resp["result"]["content"][0]["text"]


def test_socket_is_private(daemon, sock):
    assert oct(os.stat(sock).st_mode & 0o777) == "0o600"


def test_ping(daemon, sock):
    r = DaemonClient(sock).ping()
    assert r["ok"] and r["proto"] == 1 and r["pid"] == os.getpid()


def test_search_and_reindex_round_trip(daemon, sock, tmpvault):
    _write(tmpvault, "project_pump.md", "The heat pump compressor trips when the sensor freezes.")
    c = DaemonClient(sock)
    cfg = Config()
    c.reindex(tmpvault, cfg, wait=True)
    assert c.search(tmpvault, cfg, "compressor trips")[0]["file"] == "project_pump.md"
    _write(tmpvault, "project_bread.md", "Sourdough needs a twelve hour proof.")
    c.reindex(tmpvault, cfg, wait=True)
    assert c.search(tmpvault, cfg, "sourdough proof")[0]["file"] == "project_bread.md"
    assert os.path.realpath(tmpvault) in c.ping()["tenants"]


def test_daemon_vaults_allow_rerank(daemon, sock, tmpvault):
    v = daemon.daemon.vault(tmpvault, {})
    assert v.allow_rerank is True


def test_concurrent_searches(daemon, sock, tmpvault):
    for i, word in enumerate(["alpha", "bravo", "charlie", "delta"]):
        _write(tmpvault, f"project_{word}.md", f"The {word} pipeline runs nightly job {word}.")
    c = DaemonClient(sock)
    cfg = Config()
    c.reindex(tmpvault, cfg)
    got, errs = {}, []

    def worker(w):
        try:
            got[w] = c.search(tmpvault, cfg, f"{w} pipeline")[0]["file"]
        except Exception as e:                       # pragma: no cover
            errs.append(e)
    ts = [threading.Thread(target=worker, args=(w,)) for w in ["alpha", "bravo", "charlie", "delta"] * 3]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs
    assert got == {w: f"project_{w}.md" for w in ["alpha", "bravo", "charlie", "delta"]}


def test_no_daemon_raises_unavailable(sock):
    with pytest.raises(DaemonUnavailable):
        DaemonClient(sock).ping()


def test_stale_socket_is_replaced(sock):
    open(sock, "w").close()                    # left by a crashed daemon
    srv = make_server(sock)
    srv.server_close()


def test_second_daemon_refuses(daemon, sock):
    with pytest.raises(RuntimeError):
        make_server(sock)


def test_shim_recall_goes_to_daemon(daemon, sock, tmpvault, monkeypatch):
    _write(tmpvault, "project_pump.md", "The heat pump compressor trips when the sensor freezes.")
    DaemonClient(sock).reindex(tmpvault, Config())
    srv = MCPServer(tmpvault, Config())

    def no_local(*a, **k):
        raise AssertionError("shim searched in-process while the daemon was up")
    monkeypatch.setattr(srv.vault, "search", no_local)
    assert "project_pump.md" in _recall(srv, "compressor trips")


def test_shim_falls_back_without_daemon(sock, tmpvault, monkeypatch):
    _write(tmpvault, "project_pump.md", "The heat pump compressor trips when the sensor freezes.")
    monkeypatch.setattr(core, "_reranker",
                        lambda m: (_ for _ in ()).throw(AssertionError("fallback loaded the reranker")))
    srv = MCPServer(tmpvault, Config(rerank="fake/model"))
    srv.vault.reindex()
    assert "project_pump.md" in _recall(srv, "compressor trips")
    assert srv.vault.allow_rerank is False


def test_shim_fallback_is_logged_once(sock, tmpvault, monkeypatch):
    _write(tmpvault, "project_pump.md", "The heat pump compressor trips.")
    srv = MCPServer(tmpvault, Config())
    srv.vault.reindex()
    _recall(srv, "compressor")
    _recall(srv, "heat pump")
    log = os.environ["COGVAULT_LOG"]
    rows = [json.loads(l) for l in open(log)] if os.path.exists(log) else []
    assert sum(1 for r in rows if r.get("op") == "daemon-fallback") == 1


def test_shim_record_reindexes_through_daemon(daemon, sock, tmpvault):
    srv = MCPServer(tmpvault, Config())
    resp = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                       "params": {"name": "cogvault_record", "arguments": {
                           "content": "The staging deploy needs the VPN up first.",
                           "title": "staging deploy needs vpn", "type": "feedback"}}})
    out = json.loads(resp["result"]["content"][0]["text"])
    assert out["status"] == "ok"
    assert os.path.exists(os.path.join(tmpvault, out["file"]))
    hits = DaemonClient(sock).search(tmpvault, Config(), "staging deploy VPN")
    assert hits[0]["file"] == out["file"]


def test_cli_search_uses_daemon(daemon, sock, tmpvault, monkeypatch, capsys):
    from cogvault.cli import main
    _write(tmpvault, "project_pump.md", "The heat pump compressor trips when the sensor freezes.")
    DaemonClient(sock).reindex(tmpvault, Config())
    calls = []
    real = DaemonClient.search
    monkeypatch.setattr(DaemonClient, "search",
                        lambda self, *a, **k: calls.append(a) or real(self, *a, **k))
    assert main(["search", "--tenant", tmpvault, "compressor trips"]) == 0
    assert "project_pump.md" in capsys.readouterr().out
    assert len(calls) == 1


def test_cli_daemon_status(sock, capsys):
    from cogvault.cli import main
    assert main(["daemon", "--status"]) == 1
    srv = make_server(sock)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        assert main(["daemon", "--status"]) == 0
        assert '"proto": 1' in capsys.readouterr().out
    finally:
        srv.shutdown()
        srv.server_close()
