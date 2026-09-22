"""Public two-gate API for evaluation-driven Skill changes."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import httpx
from fastapi import FastAPI

from stata_research_agent.domain.identifiers import WorkspaceId
from stata_research_agent.interfaces.api import WorkspaceHost, create_app


def request(app: FastAPI, method: str, url: str, **kwargs: object) -> httpx.Response:
    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as session:
            return await session.request(method, url, **kwargs)

    return asyncio.run(send())


def test_skill_change_materialization_and_activation_are_separate_public_commands(
    tmp_path: Path,
) -> None:
    host = WorkspaceHost(tmp_path / "host")
    app = create_app(host)
    workspace_id = "ws_skill_change_api"
    created = request(
        app,
        "POST",
        "/api/v1/commands",
        json={
            "schema_version": "1",
            "command_id": "cmd_skill_change_api_workspace",
            "workspace_id": workspace_id,
            "command_type": "workspace.create",
            "payload": {},
            "preconditions": {},
        },
    )
    assert created.status_code == 200

    database = host.database(WorkspaceId(workspace_id))
    markdown = (
        '---\nname: research-style\ndescription: "Research review guidance"\n---\n\n'
        "Discuss consequential analytical choices.\n"
    )
    digest = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    connection = database.open(writable=True)
    try:
        connection.execute(
            """
            INSERT INTO skill_versions VALUES (
                'skillversion_api_base', 'research-style', '1.0.0', ?, ?, NULL, NULL, 1
            )
            """,
            (digest, markdown),
        )
        connection.execute(
            """
            INSERT INTO skill_adoptions VALUES (
                'research-style', 'skillversion_api_base', 'active', 1, 1
            )
            """
        )
        evidence = json.dumps(
            {
                "arms": [],
                "available_active_skills": [],
                "relationship_note": "observational, not causal",
            }
        )
        connection.execute(
            """
            INSERT INTO skill_evaluation_runs VALUES (
                'skilleval_api_change', 'research-style', 'skillversion_api_base', NULL,
                'observational_single_version', 'test-v1', 1, 1, ?, 'completed', 1, 1
            )
            """,
            (evidence,),
        )
        connection.execute(
            """
            INSERT INTO skill_evaluation_reports VALUES (
                'skilleval_api_change', 'independent_model', 'test-evaluator-v1', 'mixed',
                'The guidance can be clearer.', '["Observational evidence only."]', 1
            )
            """
        )
        connection.execute(
            """
            INSERT INTO skill_improvement_proposals VALUES (
                'skillproposal_api_change', 'skilleval_api_change', 'revise',
                'skillversion_api_base', NULL, 'Clarify review',
                'Make research decisions easier to inspect.', ?, 1
            )
            """,
            ("Explain alternatives and ask the researcher to make the final decision.",),
        )
        connection.commit()
    finally:
        connection.close()
    skill_path = database.root / "skills" / "research-style" / "SKILL.md"
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    skill_path.write_text(markdown, encoding="utf-8", newline="\n")

    materialized = request(
        app,
        "POST",
        f"/api/v1/workspaces/{workspace_id}/skill-improvement-proposals/"
        "skillproposal_api_change/materialize",
        json={"command_id": "cmd_skill_change_api_materialize"},
    )
    assert materialized.status_code == 200
    candidate = materialized.json()
    assert candidate["lifecycle"] == "proposed"
    assert candidate["validation_status"] == "passed"
    connection = database.open(writable=False)
    try:
        assert (
            connection.execute("SELECT current_skill_version_id FROM skill_adoptions").fetchone()[0]
            == "skillversion_api_base"
        )
    finally:
        connection.close()

    index = request(app, "GET", f"/api/v1/workspaces/{workspace_id}/skill-evolution-candidates")
    assert index.status_code == 200
    assert index.json()["change_candidates"][0]["source_proposal_id"] == (
        "skillproposal_api_change"
    )

    activated = request(
        app,
        "POST",
        f"/api/v1/workspaces/{workspace_id}/skill-change-candidates/"
        f"{candidate['candidate_id']}/activate",
        json={
            "command_id": "cmd_skill_change_api_activate",
            "expected_pointer_revision": candidate["pointer_revision"],
        },
    )
    assert activated.status_code == 200
    assert activated.json()["lifecycle"] == "activated"
    assert activated.json()["activated_skill_version_id"].startswith("skillversion_")
