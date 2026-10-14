"""
cogvault.daemon — one long-lived process that holds the models for every tenant.

Each agent session starts its own `cogvault mcp`. Before this module every one of
them loaded the embedding model on its own (~1.2 GB each, 5-7 sessions), and the
cross-encoder reranker (~2 GB) could not be added at all. The daemon loads each
model once; `cogvault mcp` and `cogvault search` forward to it over a Unix socket
and fall back to in-process work (without the reranker) when it is down.

Protocol: one JSON object per line, one JSON reply per line.
  {"proto": 1, "op": "ping|search|reindex", "tenant": <dir>, "cfg": {...}, "args": {...}}
The client sends its fully resolved Config, so the daemon searches exactly as the
client would have. Replies are {"ok": true, ...} or {"ok": false, "error", "message"}.
"""
from __future__ import annotations
import dataclasses, json, os, socket, socketserver, sys, threading, time

from . import __version__

PROTO = 1

# Unload a model after this long without a search or reindex. Recall is sparse
# (a few calls per session hour), and the models held 2.6 GB around the clock;
# a reload costs ~1 s on the next recall. $COGVAULT_IDLE_UNLOAD overrides it,
# 0 keeps the models loaded.
DEFAULT_IDLE_UNLOAD = 600.0


def idle_unload_from_env() -> float:
    try:
        return max(0.0, float(os.environ.get("COGVAULT_IDLE_UNLOAD") or DEFAULT_IDLE_UNLOAD))
    except ValueError:
        return DEFAULT_IDLE_UNLOAD


def default_socket() -> str:
    """$COGVAULT_SOCKET, else ~/.cache/cogvault/daemon.sock (next to the indexes)."""
    env = os.environ.get("COGVAULT_SOCKET")
    if env:
        return os.path.expanduser(env)
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "cogvault", "daemon.sock")


def cfg_to_dict(cfg) -> dict:
    d = dataclasses.asdict(cfg)
    d["ignore_globs"] = list(d.get("ignore_globs") or ())
    return d


def cfg_from_dict(d: dict):
    from .core import Config
    names = {f.name for f in dataclasses.fields(Config)}
    kw = {k: v for k, v in (d or {}).items() if k in names}   # newer client: drop unknown
    if "ignore_globs" in kw:
        kw["ignore_globs"] = tuple(kw["ignore_globs"] or ())
    return Config(**kw)


class DaemonUnavailable(Exception):
    """No daemon answered, or it answered with an error. Callers fall back."""


class DaemonClient:
    def __init__(self, path: str | None = None):
        self.path = path or default_socket()

    def call(self, op: str, tenant: str | None = None, cfg=None, args: dict | None = None,
             timeout: float = 60.0) -> dict:
        req = {"proto": PROTO, "op": op, "tenant": tenant,
               "cfg": cfg_to_dict(cfg) if cfg is not None else None, "args": args or {}}
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(0.5)                    # connect fast or fall back
            s.connect(self.path)
            s.settimeout(timeout)
            with s, s.makefile("rwb") as f:
                f.write((json.dumps(req) + "\n").encode())
                f.flush()
                line = f.readline()
        except OSError as e:
            raise DaemonUnavailable(f"{type(e).__name__}: {e}") from e
        if not line:
            raise DaemonUnavailable("daemon closed the connection")
        resp = json.loads(line)
        if not resp.get("ok"):
            raise DaemonUnavailable(f"{resp.get('error')}: {resp.get('message')}")
        return resp

    def ping(self) -> dict:
        return self.call("ping", timeout=5.0)

    def search(self, tenant: str, cfg, query: str, k: int = 5,
               card_type: str | None = None) -> list[dict]:
        # A first search after a daemon restart loads both models (a few s).
        # A hung daemon must not stall recall longer than this: the caller
        # then falls back in-process.
        return self.call("search", tenant, cfg,
                         {"query": query, "k": k, "type": card_type},
                         timeout=30.0)["results"]

    def reindex(self, tenant: str, cfg, wait: bool = True) -> dict:
        return self.call("reindex", tenant, cfg, {"wait": wait},
                         timeout=1800.0 if wait else 10.0).get("stats") or {}


class Daemon:
    def __init__(self, idle_unload: float = 0.0):
        self._vaults: dict = {}
        self._vaults_lock = threading.Lock()
        self._reindex_locks: dict[str, threading.Lock] = {}
        self.started = time.time()
        self.idle_unload = idle_unload
        self._stop = threading.Event()

    def start_reaper(self) -> None:
        """Unload models idle for `idle_unload` seconds (no-op when 0)."""
        if self.idle_unload <= 0:
            return

        def run():
            from .core import release_models
            # Check often enough that a model lives at most ~1.25x the limit.
            step = max(1.0, self.idle_unload / 4)
            while not self._stop.wait(step):
                try:
                    dropped = release_models(self.idle_unload)
                except Exception as e:           # pragma: no cover
                    from .obs import log_error
                    log_error("-", "daemon-unload", e)
                    continue
                if dropped:
                    print(f"cogvault daemon: unloaded idle {', '.join(dropped)}",
                          file=sys.stderr, flush=True)
        threading.Thread(target=run, name="cogvault-reaper", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def vault(self, tenant: str, cfg_dict: dict):
        from .core import Vault
        key = (os.path.realpath(os.path.expanduser(tenant)),
               json.dumps(cfg_dict or {}, sort_keys=True))
        with self._vaults_lock:
            v = self._vaults.get(key)
            if v is None:
                v = Vault(key[0], cfg_from_dict(cfg_dict))
                v.allow_rerank = True
                self._vaults[key] = v
                self._reindex_locks.setdefault(v.dir, threading.Lock())
            return v

    def _reindex(self, v, wait: bool) -> dict | None:
        lock = self._reindex_locks[v.dir]
        if wait:
            with lock:
                return v.reindex()
        if not lock.acquire(blocking=False):
            return None                          # one already running: nothing to add

        def run():
            try:
                v.reindex()
                self._warm(v)
            except Exception as e:
                from .obs import log_error
                log_error(v.dir, "daemon-reindex", e)
            finally:
                lock.release()
        threading.Thread(target=run, daemon=True).start()
        return None

    @staticmethod
    def _warm(v) -> None:
        """Load this tenant's models now, so the agent's first recall is warm."""
        from .core import _embedder, _reranker
        _embedder(v.cfg.model)
        if v.cfg.rerank:
            _reranker(v.cfg.rerank)

    def dispatch(self, req: dict) -> dict:
        from .core import loaded_models
        op = req.get("op")
        try:
            if op == "ping":
                return {"ok": True, "version": __version__, "proto": PROTO,
                        "pid": os.getpid(), "uptime_s": round(time.time() - self.started),
                        "tenants": sorted({k[0] for k in self._vaults}),
                        "idle_unload_s": self.idle_unload,
                        **loaded_models()}
            tenant = req.get("tenant")
            if not tenant:
                return {"ok": False, "error": "BadRequest", "message": "tenant required"}
            v = self.vault(tenant, req.get("cfg"))
            a = req.get("args") or {}
            if op == "search":
                q = a.get("query")
                if not isinstance(q, str) or not q.strip():
                    return {"ok": False, "error": "BadRequest", "message": "query required"}
                return {"ok": True, "results": v.search(q, k=int(a.get("k") or 5),
                                                         card_type=a.get("type"))}
            if op == "reindex":
                return {"ok": True, "stats": self._reindex(v, bool(a.get("wait", True)))}
            return {"ok": False, "error": "BadRequest", "message": f"unknown op {op!r}"}
        except Exception as e:
            from .obs import log_error
            log_error(req.get("tenant") or "-", f"daemon-{op}", e)
            return {"ok": False, "error": type(e).__name__, "message": str(e)}


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        for line in self.rfile:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except json.JSONDecodeError:
                resp = {"ok": False, "error": "BadRequest", "message": "invalid JSON"}
            else:
                resp = self.server.daemon.dispatch(req)
            self.wfile.write((json.dumps(resp, ensure_ascii=False) + "\n").encode())
            self.wfile.flush()


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    # socketserver's default listen backlog is 5: twelve agents recalling at
    # once got ECONNREFUSED and silently fell back to in-process search.
    request_queue_size = 128


def make_server(path: str | None = None, idle_unload: float = 0.0) -> _Server:
    """Bind the socket (mode 600). Refuses to start over a live daemon; removes
    a stale socket file left by a crash."""
    path = path or default_socket()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        try:
            DaemonClient(path).ping()
        except DaemonUnavailable:
            os.unlink(path)
        else:
            raise RuntimeError(f"a cogvault daemon already serves {path}")
    old = os.umask(0o177)
    try:
        srv = _Server(path, _Handler)
    finally:
        os.umask(old)
    srv.daemon = Daemon(idle_unload)
    return srv


def serve(path: str | None = None, idle_unload: float | None = None) -> None:
    import signal
    srv = make_server(path, idle_unload_from_env() if idle_unload is None else idle_unload)
    srv.daemon.start_reaper()
    # launchd stops the daemon with SIGTERM; exit through `finally` so the
    # socket file goes away with the process.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    print(f"cogvault daemon {__version__} (pid {os.getpid()}) on {srv.server_address}",
          file=sys.stderr, flush=True)
    try:
        srv.serve_forever()
    finally:
        try:
            os.unlink(srv.server_address)
        except OSError:
            pass
