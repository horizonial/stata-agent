"""切片 2：能力画像 / codec / chat_structured 重试 / deepseek 配置 / 契约。"""

from __future__ import annotations

import json
import os

import pytest

from stata_agent.domain.action import ActionProposal
from stata_agent.providers.capabilities import deepseek_chat_profile, from_file, to_file
from stata_agent.providers.codec import StructuredOutputError, parse_proposal
from stata_agent.providers.contract import check_all, run_checks
from stata_agent.providers.deepseek import DeepSeekProvider, MissingApiKey
from stata_agent.providers.fake import FakeChat
from stata_agent.providers.llm import chat_proposal
from stata_agent.providers.mock import MockFixedProvider


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
    with pytest.raises(MissingApiKey):
        DeepSeekProvider(api_key=None)


def test_contract_checks_on_mock():
    prov = MockFixedProvider(ActionProposal(decision_summary="x", acts=[]))
    res = run_checks(prov)
    assert all(res.values()), res
    check_all(prov)  # 不抛


@pytest.mark.skipif(
    not (os.environ.get("DEEPSEEK_API_KEY") and os.environ.get("DEEPSEEK_LIVE") == "1"),
    reason="需 DEEPSEEK_API_KEY 且显式 DEEPSEEK_LIVE=1 才跑 live",
)
def test_deepseek_live_propose():
    prov = DeepSeekProvider()
    prop = prov.propose("phase=IDEA\n只测一次：给我一个 inspect_data 提议")
    assert isinstance(prop, ActionProposal)
