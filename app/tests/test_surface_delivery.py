from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import stata_agent.ui as ui
from stata_agent.rag.index import load_cached_chunks
from stata_agent.rag.ingest import (
    SOURCE_ROLE_CITABLE,
    SOURCE_ROLE_STYLE,
    _segment,
)
from stata_agent.skills.loader import SkillLoadError, load_skill_dir, match_skills
from stata_agent.storage.sqlite_store import SQLiteStore


def _make_pdf(path: Path, text: str = "Difference in differences estimation") -> None:
    import fitz

    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), text)
    document.save(str(path))
    document.close()


def test_rag_limits_identity_role_and_deleted_cache(tmp_path):
    assert all(len(chunk) <= 1200 for chunk in _segment("x" * 5000))
    source = tmp_path / "papers"
    source.mkdir()
    pdf = source / "paper.pdf"
    _make_pdf(pdf)
    cache = tmp_path / "rag-cache.json"

    chunks = load_cached_chunks(cache, source, source_role=SOURCE_ROLE_CITABLE)
    assert chunks and all(c.source_role == SOURCE_ROLE_CITABLE for c in chunks)
    assert len(chunks[0].doc_id.split("|", 1)[-1]) == 64

    default_chunks = load_cached_chunks(tmp_path / "style-cache.json", source)
    assert default_chunks and all(c.source_role == SOURCE_ROLE_STYLE for c in default_chunks)

    pdf.unlink()
    assert load_cached_chunks(cache, source, source_role=SOURCE_ROLE_CITABLE) == []
    assert json.loads(cache.read_text(encoding="utf-8")) == {}


def test_skill_loader_nested_frontmatter_errors_and_no_fallback(tmp_path):
    root = tmp_path / "skills"
    nested = root / "did"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text(
        "---\nname: did-route\ndescription: Difference in differences routing\n"
        "triggers:\n  - needs: [did]\nrequires:\n  ados: [reghdfe]\n---\n\n# Route\n",
        encoding="utf-8",
    )
    loaded = load_skill_dir(root)
    assert loaded["did-route"].triggers == ["did"]
    assert loaded["did-route"].requires_ados == ["reghdfe"]
    assert match_skills(loaded, "please do a did")
    assert match_skills(loaded, "unrelated small talk") == []

    (root / "broken.md").write_text("---\nname: broken\n", encoding="utf-8")
    with pytest.raises(SkillLoadError, match="missing closing"):
        load_skill_dir(root)


def test_ui_chat_hides_current_user_from_history_and_closes_executor(tmp_path, monkeypatch):
    monkeypatch.setattr(ui, "DEFAULT_DB", tmp_path / "ledger.sqlite3")
    monkeypatch.setenv("STATA_AGENT_WORKSPACES", str(tmp_path / "workspaces.json"))
    monkeypatch.delenv("STATA_AGENT_EXECUTOR", raising=False)
    monkeypatch.delenv("STATA_AGENT_DEMO", raising=False)
    assert ui._executor(SQLiteStore(str(tmp_path / "probe.sqlite3"), writer_id="probe")) is None

    class Provider:
        def __init__(self):
            self.messages = []

        def chat(self, messages, *, tools=None):
            self.messages = messages
            return {"content": "ok", "tool_calls": []}

    class Executor:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    provider = Provider()
    executor = Executor()
    monkeypatch.setattr(ui, "_provider", lambda: provider)
    monkeypatch.setattr(ui, "_executor", lambda store: executor)
    monkeypatch.setattr(ui, "_rag", lambda: None)
    monkeypatch.setattr(ui, "_memory", lambda: None)
    reply, ask, _ = ui._run_chat_sync("ui", "hello", "interactive")
    assert (reply, ask) == ("ok", None)
    assert executor.closed
    assert [m["content"] for m in provider.messages if m["role"] == "user"] == ["hello"]


def test_sse_tail_event_request_metadata_and_done_order(monkeypatch):
    monkeypatch.setattr(ui, "_resolve_workspace", lambda ws: "ui")

    def fake_run(*args, on_event=None, **kwargs):
        on_event({"type": "text_delta", "text": "tail"})
        return "done", None, {"events": 1}

    monkeypatch.setattr(ui, "_run_chat_sync", fake_run)
    response = asyncio.run(ui.chat_stream(ui.ChatIn(text="hello")))

    async def collect():
        return [item.decode() if isinstance(item, bytes) else item async for item in response.body_iterator]

    payload = "".join(asyncio.run(collect()))
    rows = [json.loads(part.split("data: ", 1)[1]) for part in payload.strip().split("\n\n")]
    assert [row["type"] for row in rows] == ["start", "token", "done"]
    assert rows[1]["text"] == "tail"
    assert rows[-1]["request_id"] == rows[0]["request_id"] == response.headers["x-request-id"]
    assert "no-cache" in response.headers["cache-control"]
