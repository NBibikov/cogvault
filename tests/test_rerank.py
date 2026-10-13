"""Cross-encoder rerank step. A fake cross-encoder stands in for the real model,
so these tests download nothing."""
import os
import tempfile
import shutil

import pytest

import cogvault.core as core
from cogvault.core import Vault, Config, apply_tenant_config


class FakeCrossEncoder:
    """Scores a doc by how many times it says WINNER; records every call."""

    def __init__(self):
        self.calls = []

    def rerank(self, query, documents, **kw):
        docs = list(documents)
        self.calls.append((query, docs))
        for d in docs:
            yield float(d.count("WINNER"))


@pytest.fixture
def tmpvault():
    d = tempfile.mkdtemp()
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def fake_ce(monkeypatch):
    ce = FakeCrossEncoder()
    monkeypatch.setattr(core, "_reranker", lambda model: ce)
    return ce


def _write(d, name, text):
    with open(os.path.join(d, name), "w") as f:
        f.write(text)


def _seed(d):
    # The plain hybrid ranking prefers the "pump" cards; only the reranker
    # knows project_winner.md is the answer.
    _write(d, "project_pump_a.md", "---\nname: project-pump-a\nmetadata:\n  type: project\n---\n\n"
           "heat pump compressor pump pump compressor trips.\n")
    _write(d, "project_pump_b.md", "---\nname: project-pump-b\nmetadata:\n  type: project\n---\n\n"
           "heat pump compressor pump notes.\n")
    _write(d, "project_winner.md", "---\nname: project-winner\nmetadata:\n  type: project\n---\n\n"
           "WINNER WINNER the compressor fix. See [[project-pump-b]].\n")


def _vault(d, **cfg):
    v = Vault(d, Config(rerank="fake/model", rerank_top=10, rerank_chars=800, **cfg))
    v.reindex()
    return v


def test_rerank_reorders_and_truncates(tmpvault, fake_ce):
    _seed(tmpvault)
    v = _vault(tmpvault)
    v.allow_rerank = True
    res = v.search("heat pump compressor", k=2)
    assert [r["file"] for r in res][0] == "project_winner.md"
    assert len(res) == 2
    assert res[0]["rerank"] == 2.0
    # The cross-encoder saw every candidate, not just the top k.
    assert len(fake_ce.calls[0][1]) == 3


def test_rerank_recomputes_related_for_new_top(tmpvault, fake_ce):
    _seed(tmpvault)
    v = _vault(tmpvault)
    v.allow_rerank = True
    res = v.search("heat pump compressor", k=3)
    assert res[0]["file"] == "project_winner.md"
    assert res[0]["related"] == ["project_pump_b.md"]
    assert not any(r.get("related") for r in res[1:])


def test_rerank_doc_is_card_head(tmpvault, fake_ce):
    _seed(tmpvault)
    v = Vault(tmpvault, Config(rerank="fake/model", rerank_top=10, rerank_chars=20))
    v.reindex()
    v.allow_rerank = True
    v.search("heat pump compressor", k=1)
    assert all(len(doc) <= 20 for doc in fake_ce.calls[0][1])


def test_rerank_off_without_allow(tmpvault, fake_ce):
    """A CLI one-shot or a fallback MCP process must never load the 2 GB model."""
    _seed(tmpvault)
    v = _vault(tmpvault)
    v.search("heat pump compressor", k=2)
    assert fake_ce.calls == []


def test_rerank_off_when_not_configured(tmpvault, fake_ce):
    _seed(tmpvault)
    v = Vault(tmpvault, Config())
    v.reindex()
    v.allow_rerank = True
    v.search("heat pump compressor", k=2)
    assert fake_ce.calls == []


def test_rerank_failure_keeps_hybrid_order(tmpvault, monkeypatch):
    """A broken reranker degrades to the plain ranking; recall never fails."""
    _seed(tmpvault)

    def boom(model):
        raise RuntimeError("model file missing")
    monkeypatch.setattr(core, "_reranker", boom)
    v = _vault(tmpvault)
    v.allow_rerank = True
    res = v.search("heat pump compressor", k=2)
    assert len(res) == 2
    assert "rerank" not in res[0]


def test_tenant_toml_sets_rerank(tmpvault):
    _write(tmpvault, ".cogvault.toml",
           'rerank = "BAAI/bge-reranker-v2-m3-int8"\nrerank_top = 12\nrerank_chars = 600\n')
    cfg = apply_tenant_config(Config(), tmpvault)
    assert (cfg.rerank, cfg.rerank_top, cfg.rerank_chars) == (
        "BAAI/bge-reranker-v2-m3-int8", 12, 600)


def test_rerank_config_does_not_rebuild_index(tmpvault, fake_ce):
    _seed(tmpvault)
    Vault(tmpvault, Config()).reindex()
    st = Vault(tmpvault, Config(rerank="fake/model")).reindex()
    assert st["embedded"] == 0
