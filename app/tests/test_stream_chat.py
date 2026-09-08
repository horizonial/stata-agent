"""deepseek.stream_chat 的流式解析与工具参数分块拼接（mock urllib，不联网）。"""

from __future__ import annotations

import io
import json

import pytest

from stata_agent.providers.capabilities import deepseek_chat_profile
from stata_agent.providers.deepseek import DeepSeekProvider


class _FakeResp:
    def __init__(self, events):
        # events: list[dict] 每条是 OpenAI chunk 的 choice.delta
        self._lines = []
        for ev in events:
            self._lines.append(f"data: {json.dumps(ev)}\n\n")
        self._lines.append("data: [DONE]\n\n")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter([l.encode() for l in self._lines])


def _mk(text_deltas=None, tool_deltas=None, finish="stop"):
    """构造一个符合协议的 chunk 序列：末尾带 finish_reason，随后 [DONE]。
    text_deltas: 文本增量；tool_deltas: [(name_parts, arg_parts), ...]。"""
    events = []
    for t in text_deltas or []:
        events.append({"choices": [{"delta": {"content": t}}]})
    for idx, (name_parts, arg_parts) in enumerate(tool_deltas or []):
        for i, chunk in enumerate(name_parts):
            d = {"index": idx, "function": {"name": chunk}}
            if i == 0:
                d["id"] = f"call_{idx}"
            events.append({"choices": [{"delta": {"tool_calls": [d]}}]})
        for chunk in arg_parts:
            events.append({"choices": [{"delta": {
                "tool_calls": [{"index": idx, "function": {"arguments": chunk}}]}}]})
    # 末尾：finish_reason（文本→stop；工具→tool_calls）
    events.append({"choices": [{"finish_reason": finish, "delta": {}}]})
    return events


def test_stream_text_only(tmp_path, monkeypatch):
    import urllib.request

    prov = DeepSeekProvider(api_key="k", profile=deepseek_chat_profile())
    resp = _FakeResp(_mk(text_deltas=["I", "'m", " an agent"]))
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: resp)

    out = list(prov.stream_chat([{"role": "user", "content": "hi"}]))
    texts = [e for e in out if e["type"] == "text_delta"]
    done = out[-1]
    assert "".join(e["text"] for e in texts) == "I'm an agent"
    assert done["type"] == "done" and done["tool_calls"] is None


def test_stream_tool_arguments_stitched(tmp_path, monkeypatch):
    """工具参数分多块（{"path": "da → ta/ → panel.dta"}）必须拼完再一次性解析。"""
    import urllib.request

    prov = DeepSeekProvider(api_key="k", profile=deepseek_chat_profile())
    # 工具名分 2 块、参数分 3 块
    resp = _FakeResp(_mk(tool_deltas=[(["inspect_", "dataset"], ['{"data":', '"D:/da', 'ta/panel.dta"}'])]))
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: resp)

    out = list(prov.stream_chat([{"role": "user", "content": "look"}], tools=[{}]))
    done = out[-1]
    assert done["type"] == "done"
    call = done["tool_calls"][0]
    assert call["name"] == "inspect_dataset"
    assert call["arguments"] == {"data": "D:/data/panel.dta"}
    # 分块期间不产出 tool_calls（只在 done 给完整 arguments）
    assert all(e["type"] == "done" or e["type"] == "text_delta" for e in out)


def test_stream_bad_arguments_fail_closed(tmp_path, monkeypatch):
    import urllib.request

    from stata_agent.providers.deepseek import StreamProtocolError

    prov = DeepSeekProvider(api_key="k", profile=deepseek_chat_profile())
    resp = _FakeResp(_mk(tool_deltas=[(["run_stata"], ["this is not json"])], finish="tool_calls"))
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: resp)

    with pytest.raises(StreamProtocolError):
        list(prov.stream_chat([{"role": "user", "content": "x"}], tools=[{}]))
