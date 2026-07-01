"""Test isolation: without this, every test Vault (default db_dir) writes its
index into the REAL ~/.cache/cogvault/ (hundreds of MB of leaked agentA-*/tmp*
dbs) and pollutes the production query-log.jsonl, skewing `cogvault analyze`."""
import pytest

import cogvault.core as core


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path, monkeypatch):
    # DEFAULT_DB_DIR is resolved at import time from XDG_CACHE_HOME, so patch
    # the module attribute for THIS process, and the env var for child
    # processes (the multiprocessing concurrency tests re-import cogvault).
    monkeypatch.setattr(core, "DEFAULT_DB_DIR", str(tmp_path / "cogvault-cache"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    monkeypatch.setenv("COGVAULT_LOG", str(tmp_path / "query-log.jsonl"))
