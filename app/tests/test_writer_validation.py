"""Task 1: deterministic delivery preflight and privacy-safe manifest."""

from __future__ import annotations

from types import SimpleNamespace

from stata_agent.domain.models import Claim, EvidenceCard
from stata_agent.writer.validation import preflight_delivery


def _projection() -> SimpleNamespace:
    card = EvidenceCard(
        card_id="card-r1-coef",
        kind="numeric",
        locator={
            "run_id": "r1",
            "stat_type": "coef",
            "target_term": "mpg",
            "machine_hash": "ignored",
            "path": r"C:\private\secret.do",
        },
        value={"value": -1.25},
        machine_hash="machine-hash",
        signed_by="validator",
    )
    claim = Claim(
        claim_id="claim-r1",
        statement="系数为负。",
        cards=[card.card_id],
        written_by="evidence_builder",
    )
    return SimpleNamespace(
        idea_id="i1",
        cards={card.card_id: card},
        claims={claim.claim_id: claim},
    )


def test_preflight_manifest_is_stable_and_reachable_only() -> None:
    projection = _projection()
    first = preflight_delivery(
        projection,
        objects=[{"object_type": "table_cell", "object_id": "t1:r1:coef", "card_ids": ["card-r1-coef"]}],
    )
    second = preflight_delivery(
        projection,
        objects=[{"object_type": "table_cell", "object_id": "t1:r1:coef", "card_ids": ["card-r1-coef"]}],
    )

    assert first.ok and second.ok
    assert first.manifest is not None
    assert first.manifest.delivery_digest == second.manifest.delivery_digest
    data = first.manifest.to_dict()
    assert data["kind"] == "evidence-manifest.v1"
    assert data["cards"][0]["card_id"] == "card-r1-coef"
    assert data["objects"][0]["object_type"] == "table_cell"
    assert "path" not in str(data)
    assert "secret.do" not in str(data)


def test_preflight_collects_stable_safe_failures() -> None:
    projection = _projection()
    projection.claims["bad"] = Claim(
        claim_id="bad",
        statement="bad",
        cards=["missing", "missing"],
        written_by="model",
    )

    result = preflight_delivery(projection)

    assert not result.ok
    assert result.manifest is None
    assert [issue.code for issue in result.issues] == [
        "card_missing",
        "card_missing",
        "claim_card_duplicate",
        "claim_writer_invalid",
    ]
    assert all(len(issue.detail) <= 240 for issue in result.issues)


def test_preflight_rejects_unbound_numeric_claim_text() -> None:
    projection = _projection()
    result = preflight_delivery(
        projection,
        claim_texts=[{
            "object_id": "claim-r1",
            "text": "系数为 999。",
            "card_ids": ["card-r1-coef"],
        }],
    )
    assert not result.ok
    assert any(issue.code == "claim_numeric_unbound" for issue in result.issues)
