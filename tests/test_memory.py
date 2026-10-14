"""Memory footprint: ONNX options, idle unload in the daemon, model release in the shim."""
import os
import shutil
import tempfile
import threading
import time

import pytest

import cogvault.core as core
from cogvault.core import Config
from cogvault.daemon import Daemon, DaemonClient, make_server
from cogvault.mcp_server import MCPServer, _boot_reindex


@pytest.fixture
def tmpvault():
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "project_pump.md"), "w") as f:
        f.write("The heat pump compressor trips when the sensor freezes.")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def sock(monkeypatch):
    d = tempfile.mkdtemp(dir="/tmp")                # macOS AF_UNIX path limit
    path = os.path.join(d, "d.sock")
    monkeypatch.setenv("COGVAULT_SOCKET", path)
    yield path
    shutil.rmtree(d, ignore_errors=True)


def _serve(path):
    srv = make_server(path)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def fake_models():
    """Two fake models in the process caches, removed again after the test."""
    names = {"fake/embedder": core._EMBEDDERS, "fake/reranker": core._RERANKERS}
    for name, cache in names.items():
        cache[name] = object()
        core._LAST_USED[name] = time.monotonic()
    yield names
    for name, cache in names.items():
        cache.pop(name, None)
        core._LAST_USED.pop(name, None)


def test_ort_kwargs_default(monkeypatch):
    monkeypatch.delenv("COGVAULT_THREADS", raising=False)
    assert core._ort_kwargs() == {"enable_cpu_mem_arena": False, "threads": 4}


def test_ort_kwargs_threads_env(monkeypatch):
    monkeypatch.setenv("COGVAULT_THREADS", "0")
    assert "threads" not in core._ort_kwargs()
    monkeypatch.setenv("COGVAULT_THREADS", "2")
    assert core._ort_kwargs()["threads"] == 2
    monkeypatch.setenv("COGVAULT_THREADS", "many")
    assert core._ort_kwargs()["threads"] == 4


def test_embed_uses_small_batch(monkeypatch):
    seen = {}

    class Fake:
        def embed(self, texts, batch_size=256):
            seen["batch"] = batch_size
            return [[0.0] for _ in texts]
    monkeypatch.setattr(core, "_embedder", lambda model: Fake())
    core.embed(["a", "b"], "fake/embedder")
    assert seen["batch"] == core.EMBED_BATCH <= 32


def test_release_models_idle_only(fake_models):
    core._LAST_USED["fake/reranker"] = time.monotonic() - 100
    assert core.release_models(50) == ["fake/reranker"]
    assert "fake/embedder" in core._EMBEDDERS
    assert "fake/reranker" not in core._RERANKERS


def test_release_models_all(fake_models):
    dropped = core.release_models()
    assert {"fake/embedder", "fake/reranker"} <= set(dropped)
    assert not any(n in core._EMBEDDERS or n in core._RERANKERS for n in fake_models)


def test_daemon_reaper_unloads_idle_models(fake_models):
    core._LAST_USED["fake/embedder"] = time.monotonic() - 100
    core._LAST_USED["fake/reranker"] = time.monotonic() - 100
    d = Daemon(idle_unload=0.5)
    d.start_reaper()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (
                "fake/embedder" in core._EMBEDDERS or "fake/reranker" in core._RERANKERS):
            time.sleep(0.1)
        assert "fake/embedder" not in core._EMBEDDERS
        assert "fake/reranker" not in core._RERANKERS
    finally:
        d.stop()


def test_daemon_reaper_off_by_zero():
    def reapers():
        return sum(1 for t in threading.enumerate() if t.name == "cogvault-reaper")
    before = reapers()
    Daemon(idle_unload=0).start_reaper()
    assert reapers() == before


def test_ping_reports_idle_unload(sock):
    srv = make_server(sock, idle_unload=123)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert DaemonClient(sock).ping()["idle_unload_s"] == 123
    finally:
        srv.shutdown()
        srv.server_close()


def test_shim_drops_local_models_when_daemon_answers(sock, tmpvault, fake_models):
    srv = _serve(sock)
    try:
        DaemonClient(sock).reindex(tmpvault, Config())
        shim = MCPServer(tmpvault, Config())
        assert shim._search("compressor trips", 5, None)[0]["file"] == "project_pump.md"
        assert "fake/embedder" not in core._EMBEDDERS
        assert "fake/reranker" not in core._RERANKERS
    finally:
        srv.shutdown()
        srv.server_close()


def test_boot_waits_for_daemon_instead_of_loading_models(sock, tmpvault, monkeypatch):
    shim = MCPServer(tmpvault, Config())
    monkeypatch.setattr(shim.vault, "reindex",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("shim indexed in-process while waiting")))
    holder = {}
    threading.Timer(0.3, lambda: holder.setdefault("srv", _serve(sock))).start()
    try:
        _boot_reindex(shim, wait_s=10, step=0.1)
        assert "srv" in holder
    finally:
        time.sleep(0.4)                              # let the daemon's reindex thread end
        if "srv" in holder:
            holder["srv"].shutdown()
            holder["srv"].server_close()


def test_boot_falls_back_after_wait(sock, tmpvault, monkeypatch):
    shim = MCPServer(tmpvault, Config())
    calls = []
    monkeypatch.setattr(shim.vault, "reindex", lambda *a, **k: calls.append(1))
    _boot_reindex(shim, wait_s=0.2, step=0.05)
    assert calls == [1]
