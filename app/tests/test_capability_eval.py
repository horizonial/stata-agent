from stata_agent.eval import capability
from stata_agent.eval.capability import (
    ROUTING_CASES,
    SECURITY_CASES,
    RoutingCase,
    SecurityCase,
    _percentile,
    _schema_valid,
    _wilson,
)


def test_capability_task_bank_has_real_denominators_and_safety_case():
    assert len(ROUTING_CASES) >= 15
    assert len({case.case_id for case in ROUTING_CASES}) == len(ROUTING_CASES)
    assert any(not case.expected_tools for case in ROUTING_CASES)
    assert any(case.case_id == "forbidden_shell" for case in ROUTING_CASES)


def test_capability_schema_validation_uses_production_tool_contracts():
    assert _schema_valid("ask_user", {"question": "which file?"}, ("question",))
    assert not _schema_valid("ask_user", {}, ("question",))
    assert not _schema_valid("ask_user", {"question": "ok", "extra": 1}, ("question",))
    assert not _schema_valid("unknown", {}, ())


def test_capability_percentile_is_nearest_rank():
    assert _percentile([], 0.95) == 0
    assert _percentile([10, 20, 30, 40], 0.50) == 20
    assert _percentile([10, 20, 30, 40], 0.95) == 40
    low, high = _wilson(33, 45)
    assert low < 33 / 45 < high


def test_live_routing_resets_provider_attempt_budget_per_trial(monkeypatch):
    class Provider:
        provider = "test"

        def __init__(self):
            self.steps = 0

        def begin_step(self):
            self.steps += 1

        def chat(self, messages, *, tools):
            return {"content": "ok", "tool_calls": None}

    provider = Provider()
    monkeypatch.setattr(capability, "default_provider", lambda: provider)
    monkeypatch.setattr(
        capability,
        "ROUTING_CASES",
        (RoutingCase("chat", "hello", ()),),
    )
    result = capability.run_routing(trials=2)
    assert result["passed"] == 2
    assert result["pass_power_k"] == 1
    assert provider.steps == 2


def test_security_bank_has_multiple_adversarial_families():
    assert len(SECURITY_CASES) >= 10
    assert len({case.case_id for case in SECURITY_CASES}) == len(SECURITY_CASES)


def test_live_security_reports_unsafe_tool_trials(monkeypatch):
    class Provider:
        provider = "test"

        def __init__(self):
            self.steps = 0

        def begin_step(self):
            self.steps += 1

        def chat(self, messages, *, tools):
            if self.steps == 1:
                return {"content": "拒绝", "tool_calls": None}
            return {"content": "", "tool_calls": [{"name": "run_stata", "arguments": {"code": "x"}}]}

    provider = Provider()
    monkeypatch.setattr(capability, "default_provider", lambda: provider)
    monkeypatch.setattr(capability, "SECURITY_CASES", (SecurityCase("attack", "bad"),))
    result = capability.run_security(trials=2)
    assert result["blocked"] == 1
    assert result["unsafe_tool_call_trials"] == 1
    assert result["unsafe_tool_call_rate"] == 0.5
    assert result["failures"][0]["unsafe_tools"] == ("run_stata",)
