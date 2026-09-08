"""privacy/modes 扩展单测：enum/normalize/configured/fallback/remote/sanitize。"""

from __future__ import annotations

import pytest

from stata_agent.privacy import modes
from stata_agent.privacy.modes import (
    APPROVED_REMOTE,
    LOCAL_STRICT,
    MIXED_SANITIZED,
    PrivacyMode,
    PrivacyViolation,
    assert_fallback_allowed,
    configured_mode,
    fallback_allowed,
    is_remote,
    normalize_mode,
    remote_llm_allowed,
    sanitize_messages,
    sanitize_text,
)


def test_privacy_enum_values():
    assert PrivacyMode.LOCAL_STRICT.value == LOCAL_STRICT
    assert PrivacyMode.APPROVED_REMOTE.value == APPROVED_REMOTE
    assert PrivacyMode.MIXED_SANITIZED.value == MIXED_SANITIZED


def test_normalize_mode_valid_and_invalid():
    assert normalize_mode(LOCAL_STRICT) == LOCAL_STRICT
    assert normalize_mode(PrivacyMode.APPROVED_REMOTE) == APPROVED_REMOTE
    for bad in ("garbage", "", None):
        with pytest.raises(PrivacyViolation):
            normalize_mode(bad)


def test_configured_mode_fail_closed(monkeypatch):
    # 未设置 → local_strict；合法 → 读值；非法 → fail-closed 回 local_strict
    assert configured_mode(environ={}) == LOCAL_STRICT
    assert configured_mode(environ={"STATA_AGENT_PRIVACY": APPROVED_REMOTE}) == APPROVED_REMOTE
    assert configured_mode(environ={"STATA_AGENT_PRIVACY": "whatever"}) == LOCAL_STRICT


def test_fallback_allowed_ordering():
    assert fallback_allowed(APPROVED_REMOTE, MIXED_SANITIZED)   # 收严允许
    assert not fallback_allowed(MIXED_SANITIZED, APPROVED_REMOTE)  # 放松禁止
    assert not fallback_allowed(LOCAL_STRICT, APPROVED_REMOTE)
    assert fallback_allowed(LOCAL_STRICT, LOCAL_STRICT)
    assert fallback_allowed("garbage", LOCAL_STRICT) is False


def test_assert_fallback_allowed_raises():
    with pytest.raises(PrivacyViolation):
        assert_fallback_allowed(LOCAL_STRICT, MIXED_SANITIZED)


def test_remote_llm_allowed():
    assert not remote_llm_allowed(LOCAL_STRICT, "deepseek")
    assert remote_llm_allowed(APPROVED_REMOTE, "deepseek")
    assert remote_llm_allowed(MIXED_SANITIZED, "qwen")
    assert remote_llm_allowed(LOCAL_STRICT, "local-model")   # 本地模型总允许
    assert remote_llm_allowed("garbage", "deepseek") is False
    assert is_remote("deepseek") and is_remote("QWEN") and not is_remote("local")


def test_sanitize_redacts_secrets_and_paths():
    t = sanitize_text("key is sk-1234567890abcdef, bearer ABCDEFGH12345678, "
                      "use C:/Users/me/secret.dta, http://u:p@host/x")
    assert "[REDACTED" in t
    assert "sk-1234567890abcdef" not in t
    assert "ABCDEFGH12345678" not in t
    assert "secret.dta" not in t or "LOCAL_PATH" in t
    assert "sanitized sha256=" in t


def test_sanitize_deterministic_and_label():
    a = sanitize_text("hi sk-x" + "1" * 20, label="msg")
    b = sanitize_text("hi sk-x" + "1" * 20, label="msg")
    assert a == b
    assert a.startswith("[msg sanitized")


def test_sanitize_limit():
    long = "a" * 9000
    out = sanitize_text(long, label="x", limit=200)
    assert len(out) <= 200 + 80  # 前缀 + 截断体


def test_sanitize_messages_mixed_only_remote():
    msgs = [{"role": "user", "content": "研究内容 sk-x" + "0" * 16}]
    # mixed_sanitized + 远端 → 文本被替换/脱敏
    mixed = sanitize_messages(msgs, mode=MIXED_SANITIZED, provider="deepseek")
    assert "sk-x0000" not in mixed[0]["content"]
    assert "sanitized" in mixed[0]["content"]
    # approved_remote + 远端 → 原样
    approved = sanitize_messages(msgs, mode=APPROVED_REMOTE, provider="deepseek")
    assert approved == msgs
    # mixed + 本地模型 → 原样
    local = sanitize_messages(msgs, mode=MIXED_SANITIZED, provider="ollama")
    assert local == msgs
