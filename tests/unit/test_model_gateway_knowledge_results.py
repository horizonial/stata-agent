from __future__ import annotations

import pytest

from stata_research_agent.persistence.model_gateway_store import (
    _successful_knowledge_tool_payload,
)


def test_failed_knowledge_tool_result_is_feedback_not_retrieval_provenance() -> None:
    assert (
        _successful_knowledge_tool_payload(
            "error",
            '{"error_type":"ValueError","message":"unknown node"}',
        )
        is None
    )


def test_successful_knowledge_tool_result_still_requires_hits_schema() -> None:
    with pytest.raises(ValueError, match="payload is malformed"):
        _successful_knowledge_tool_payload("success", '{"message":"missing hits"}')


def test_successful_knowledge_tool_result_returns_retrieval_payload() -> None:
    payload = _successful_knowledge_tool_payload(
        "success",
        '{"retrieval_session_id":"session-1","hits":[]}',
    )

    assert payload == {"retrieval_session_id": "session-1", "hits": []}
