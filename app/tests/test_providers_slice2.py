"""切片 2：能力画像 / codec / chat_structured 重试 / deepseek 配置 / 契约。"""

from __future__ import annotations

import json
import os

import pytest

from stata_agent.domain.action import ActionProposal
from stata_agent.providers.capabilities import deepseek_chat_profile, from_file, to_file
from stata_agent.providers.codec import StructuredOutputError, parse_proposal
from stata_agent.providers.contract import check_all, run_checks
from stata_agent.providers.deepseek import DeepSeekProvider, MissingApiKey, StreamProtocolError
from stata_agent.providers.fake import FakeChat
from stata_agent.providers.llm import chat_proposal
from stata_agent.providers.mock import MockFixedProvider, MockReplayProvider


def test_deepseek_profile_fields_and_meets():
    p = deepseek_chat_profile()
    assert p.provider == "deepseek" and p.model == "deepseek-chat"
    assert p.json_mode and not p.strict_schema
    assert p.meets({"tools": True, "json_mode": True})
    assert not p.meets({"strict_schema": True})


def test_profile_json_roundtrip(tmp_path):
    path = tmp_path / "profile.json"
    to_file(deepseek_chat_profile(), path)
    p = from_file(path)
    assert p.model == "deepseek-chat" and p.json_mode


def test_parse_proposal_valid_and_fence_stripping():
    raw = '```json\n{"decision_summary":"看数据","acts":[{"act_type":"inspect_data","target":{},"reason":"r"}],"ask_user":null,"stop_reason":null}\n```'
    prop = parse_proposal(raw)
    assert prop.acts[0].act_type == "inspect_data"
    assert isinstance(prop, ActionProposal)


def test_parse_proposal_invalid_raises():
    with pytest.raises(StructuredOutputError):
        parse_proposal("这不是 json")
    with pytest.raises(StructuredOutputError):
        parse_proposal('{"acts": ["不是对象"]}')  # acts 元素类型错 → schema 校验拦


def test_chat_structured_retries_then_succeeds():
    good = json.dumps({"decision_summary": "ok", "acts": [], "ask_user": None})
    chat = FakeChat(["not json", good])
    prop = chat_proposal(chat, "ctx")
    assert prop.decision_summary == "ok"
    assert len(chat.calls) == 2  # 第一次失败后带提示重试


def test_chat_structured_raises_after_retries():
    chat = FakeChat(["bad"] * 3)  # max_retries=2 -> 3 次调用
    with pytest.raises(StructuredOutputError):
        chat_proposal(chat, "ctx", max_retries=2)
    assert len(chat.calls) == 3


def test_deepseek_missing_key_raises(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(MissingApiKey) as raised:
        DeepSeekProvider(api_key=None)
    assert raised.value.code == "credentials_missing"


def test_unknown_privacy_mode_is_fail_closed(monkeypatch):
    from stata_agent.providers.registry import PrivacyBlock, live_available, privacy_mode

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("STATA_AGENT_PRIVACY", "typo_that_must_not_enable_remote")
    assert privacy_mode() == "local_strict"
    assert live_available() is False
    with pytest.raises(PrivacyBlock):
        from stata_agent.providers.registry import default_provider

        default_provider()


def test_deepseek_propose_adapts_chat_dict_to_structured_text():
    provider = DeepSeekProvider(api_key="test-key")
    provider.chat = lambda messages, **kwargs: {
        "content": {"decision_summary": "ok", "acts": [], "ask_user": None},
        "tool_calls": None,
    }
    proposal = provider.propose("phase=IDEA")
    assert isinstance(proposal, ActionProposal)
    assert proposal.decision_summary == "ok"


def test_stream_protocol_rejects_abnormal_eof(monkeypatch):
    provider = DeepSeekProvider(api_key="test-key")

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def __iter__(self):
            # No [DONE] and no finish_reason: this is a truncated response.
            yield b'data: {"choices":[{"delta":{"content":"half"},"finish_reason":null}]}\n'

    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: Response())
    with pytest.raises(StreamProtocolError):
        list(provider.stream_chat([{"role": "user", "content": "x"}]))


def test_contract_checks_on_mock():
    prov = MockFixedProvider(ActionProposal(decision_summary="x", acts=[]))
    res = run_checks(prov)
    assert all(res.values()), res
    check_all(prov)  # 不抛


def test_legacy_mock_providers_support_modern_chat_without_fabricating_tools():
    fixed = MockFixedProvider(ActionProposal(decision_summary="记录", ask_user="请配置模型"))
    assert fixed.chat([{"role": "user", "content": "你好"}], tools=[]) == {
        "content": "请配置模型",
        "tool_calls": [],
    }

    replay = MockReplayProvider([ActionProposal(decision_summary="演示已就绪")])
    assert replay.chat([], tools=[{"type": "function"}]) == {
        "content": "演示已就绪",
        "tool_calls": [],
    }
    assert replay.consumed == 1


@pytest.mark.skipif(
    not (os.environ.get("DEEPSEEK_API_KEY") and os.environ.get("DEEPSEEK_LIVE") == "1"),
    reason="需 DEEPSEEK_API_KEY 且显式 DEEPSEEK_LIVE=1 才跑 live",
)
def test_deepseek_live_propose():
    prov = DeepSeekProvider()
    prop = prov.propose("phase=IDEA\n只测一次：给我一个 inspect_data 提议")
    assert isinstance(prop, ActionProposal)
