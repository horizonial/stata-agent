from __future__ import annotations

import asyncio

from stata_research_agent.application.model_gateway import ProviderResponseDelta
from stata_research_agent.application.streaming import EphemeralModelDeltaHub
from stata_research_agent.domain.identifiers import ProviderAttemptId


def test_ephemeral_delta_hub_is_scoped_and_drops_oldest_under_backpressure() -> None:
    async def scenario() -> None:
        hub = EphemeralModelDeltaHub(queue_capacity=1)
        stream = hub.subscribe("ws_one", "turn_one")
        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        await hub.publish(
            "ws_other",
            "turn_one",
            ProviderResponseDelta(
                ProviderAttemptId("providerattempt_other"), 1, "provider_content", "wrong"
            ),
        )
        await hub.publish(
            "ws_one",
            "turn_one",
            ProviderResponseDelta(
                ProviderAttemptId("providerattempt_one"), 1, "provider_content", "first"
            ),
        )
        event = await asyncio.wait_for(pending, timeout=1)
        assert event.content == "first"
        assert event.workspace_id == "ws_one"
        await stream.aclose()

    asyncio.run(scenario())
