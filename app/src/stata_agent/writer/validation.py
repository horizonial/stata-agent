"""Deterministic evidence delivery preflight.

The ledger projection remains the source of truth. This module only builds a
bounded, privacy-safe derived manifest and reports stable validation issues; it
never appends events or calls a provider/executor.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from ..events.schema import ACTOR_EVIDENCE, ACTOR_VALIDATOR

MANIFEST_SCHEMA_VERSION = 1
MANIFEST_KIND = "evidence-manifest.v1"
_ALLOWED_CARD_SIGNERS = frozenset({ACTOR_VALIDATOR})
_ALLOWED_CLAIM_WRITERS = frozenset({ACTOR_EVIDENCE})
_SAFE_LOCATOR_KEYS = frozenset(
    {
        "run_id",
        "stat_type",
        "target_term",
        "contract_hash",
        "verification_schema_version",
        "provenance_kind",
        "chunk_id",
        "doc_id",
        "page",
        "source_role",
        "artifact_sha256",
        "byte_size",
        "kind",
    }
)


def _canonical(value: Any) -> str:
    """Return stable JSON for trusted, bounded metadata only."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _safe_locator(locator: Any) -> dict[str, Any]:
    """Keep only non-path locator metadata suitable for a manifest."""

    if not isinstance(locator, Mapping):
        return {}
    safe: dict[str, Any] = {}
    for key in sorted(_SAFE_LOCATOR_KEYS):
        value = locator.get(key)
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            safe[key] = value
    return safe


@dataclass(frozen=True)
class DeliveryIssue:
    """Stable, safe description of one delivery blocker."""

    code: str
    object_type: str
    object_id: str
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        result = {
            "code": self.code,
            "object_type": self.object_type,
            "object_id": self.object_id,
        }
        if self.detail:
            result["detail"] = self.detail[:240]
        return result


@dataclass(frozen=True)
class DeliveryManifestV1:
    """Bounded derived index of evidence objects used for one delivery."""

    idea_id: str
    claims: tuple[dict[str, Any], ...]
    cards: tuple[dict[str, Any], ...]
    objects: tuple[dict[str, Any], ...] = ()
    schema_version: int = MANIFEST_SCHEMA_VERSION
    kind: str = MANIFEST_KIND

    @property
    def delivery_digest(self) -> str:
        return _digest(self.to_dict(include_digest=False))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "kind": self.kind,
            "schema_version": self.schema_version,
            "idea_id": self.idea_id,
            "claims": [dict(item) for item in self.claims],
            "cards": [dict(item) for item in self.cards],
            "objects": [dict(item) for item in self.objects],
        }
        if include_digest:
            data["delivery_digest"] = self.delivery_digest
        return data


@dataclass(frozen=True)
class DeliveryPreflight:
    """Either a complete manifest or deterministic blocking issues."""

    issues: tuple[DeliveryIssue, ...] = ()
    manifest: DeliveryManifestV1 | None = None

    @property
    def ok(self) -> bool:
        return not self.issues and self.manifest is not None


def _issue(code: str, object_type: str, object_id: Any, detail: str = "") -> DeliveryIssue:
    safe_id = str(object_id)[:160] if object_id is not None else ""
    return DeliveryIssue(code=code, object_type=object_type, object_id=safe_id, detail=detail)


def _object_refs(objects: Iterable[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for item in objects or ():
        if not isinstance(item, Mapping):
            continue
        object_type = str(item.get("object_type") or "object")
        object_id = str(item.get("object_id") or "")
        if not object_id:
            continue
        card_ids = sorted({str(value) for value in item.get("card_ids", ()) if value})
        ref: dict[str, Any] = {"object_type": object_type, "object_id": object_id}
        if card_ids:
            ref["card_ids"] = card_ids
        refs.append(ref)
    return sorted(refs, key=lambda ref: (ref["object_type"], ref["object_id"]))


def _validate_projection(proj: Any) -> list[DeliveryIssue]:
    issues: list[DeliveryIssue] = []
    claims = getattr(proj, "claims", {}) or {}
    cards = getattr(proj, "cards", {}) or {}
    for claim_id in sorted(claims):
        claim = claims[claim_id]
        if getattr(claim, "status", None) != "supported":
            continue
        if str(getattr(claim, "claim_id", "")) != str(claim_id):
            issues.append(_issue("claim_id_mismatch", "claim", claim_id))
        if getattr(claim, "written_by", None) not in _ALLOWED_CLAIM_WRITERS:
            issues.append(_issue("claim_writer_invalid", "claim", claim_id))
        if not str(getattr(claim, "statement", "") or "").strip():
            issues.append(_issue("claim_statement_missing", "claim", claim_id))
        refs = list(getattr(claim, "cards", []) or [])
        if not refs:
            issues.append(_issue("claim_cards_missing", "claim", claim_id))
            continue
        seen: set[str] = set()
        for card_id in refs:
            card_key = str(card_id)
            if card_key in seen:
                issues.append(_issue("claim_card_duplicate", "claim", claim_id, card_key))
            seen.add(card_key)
            card = cards.get(card_key)
            if card is None:
                issues.append(_issue("card_missing", "claim", claim_id, card_key))
                continue
            if str(getattr(card, "card_id", "")) != card_key:
                issues.append(_issue("card_id_mismatch", "card", card_key))
            if getattr(card, "signed_by", None) not in _ALLOWED_CARD_SIGNERS:
                issues.append(_issue("card_signer_invalid", "card", card_key))
            if not isinstance(getattr(card, "locator", None), dict):
                issues.append(_issue("card_locator_invalid", "card", card_key))
            if getattr(card, "kind", None) == "numeric":
                value = getattr(card, "value", None)
                raw = value.get("value") if isinstance(value, dict) else None
                try:
                    import math

                    valid_value = (
                        isinstance(raw, (int, float))
                        and not isinstance(raw, bool)
                        and math.isfinite(float(raw))
                    )
                except (TypeError, ValueError, OverflowError):
                    valid_value = False
                if not valid_value:
                    issues.append(_issue("card_value_invalid", "card", card_key))
    return sorted(issues, key=lambda item: (item.code, item.object_type, item.object_id, item.detail))


def _validate_object_refs(proj: Any, refs: list[dict[str, Any]]) -> list[DeliveryIssue]:
    issues: list[DeliveryIssue] = []
    cards = getattr(proj, "cards", {}) or {}
    seen: set[tuple[str, str]] = set()
    for ref in refs:
        key = (str(ref["object_type"]), str(ref["object_id"]))
        if key in seen:
            issues.append(_issue("object_duplicate", key[0], key[1]))
        seen.add(key)
        for card_id in ref.get("card_ids", []):
            if card_id not in cards:
                issues.append(_issue("object_card_missing", key[0], key[1], str(card_id)))
    return issues


def _validate_tables(proj: Any, tables: Iterable[Any] | None) -> list[DeliveryIssue]:
    if tables is None:
        return []
    from .table import numeric_cells, validate_cell

    issues: list[DeliveryIssue] = []
    for index, model in enumerate(tables):
        title = str(getattr(model, "title", "") or "")
        by_id = getattr(proj, "cards", {}) or {}
        for row_index, col_index, cell in numeric_cells(model):
            object_id = f"{title or 'table'}:{row_index}:{col_index}"
            error = validate_cell(cell, by_id, require_card_id=True)
            if error:
                issues.append(_issue("table_cell_invalid", "table_cell", object_id, error))
        if not getattr(model, "rows", None):
            issues.append(_issue("table_empty", "table", title or str(index)))
    return issues


def _validate_citations(
    proj: Any,
    citation_texts: Iterable[Mapping[str, Any]] | None,
    library: Any = None,
) -> list[DeliveryIssue]:
    if citation_texts is None:
        return []
    from .citation import validate_citations

    cards = list((getattr(proj, "cards", {}) or {}).values())
    issues: list[DeliveryIssue] = []
    for index, item in enumerate(citation_texts):
        text = str(item.get("text") or "")
        object_id = str(item.get("object_id") or f"citation:{index}")
        card_ids = [str(value) for value in item.get("card_ids", ()) if value]
        selected = [card for card in cards if card.card_id in card_ids] if card_ids else cards
        if any(card.kind == "citation" for card in selected) and library is None:
            issues.append(_issue("citation_library_missing", "citation", object_id))
            continue
        for detail in validate_citations(
            text,
            selected,
            library=library,
            claim_card_ids=card_ids or None,
        ):
            issues.append(_issue("citation_invalid", "citation", object_id, detail))
    return issues


def _validate_claim_texts(
    proj: Any,
    claim_texts: Iterable[Mapping[str, Any]] | None,
) -> list[DeliveryIssue]:
    """Reject numeric tokens in rendered claim text that are not card-backed."""

    if claim_texts is None:
        return []
    from .ground import validate_sentence

    claims = getattr(proj, "claims", {}) or {}
    cards = getattr(proj, "cards", {}) or {}
    issues: list[DeliveryIssue] = []
    for index, item in enumerate(claim_texts):
        object_id = str(item.get("object_id") or f"claim:{index}")
        claim_id = object_id
        claim = claims.get(claim_id)
        if claim is None or getattr(claim, "status", None) != "supported":
            issues.append(_issue("claim_not_supported", "claim", object_id))
            continue
        expected = [str(card_id) for card_id in getattr(claim, "cards", []) or []]
        selected_ids = [str(card_id) for card_id in item.get("card_ids", ()) if card_id]
        if selected_ids and selected_ids != expected:
            issues.append(_issue("claim_text_cards_mismatch", "claim", object_id))
        selected = [cards[card_id] for card_id in expected if card_id in cards]
        for token in validate_sentence(str(item.get("text") or ""), selected):
            issues.append(_issue("claim_numeric_unbound", "claim", object_id, token))
    return issues


def _validate_figures(proj: Any, figure_items: Iterable[Mapping[str, Any]] | None) -> list[DeliveryIssue]:
    if figure_items is None:
        return []
    from .figure import validate_figure_item

    cards = getattr(proj, "cards", {}) or {}
    issues: list[DeliveryIssue] = []
    for index, item in enumerate(figure_items):
        object_id = str(item.get("object_id") or f"figure:{index}")
        error = validate_figure_item(item, cards, require_card=True)
        if error:
            issues.append(_issue("figure_invalid", "figure", object_id, error))
    return issues


def build_manifest(
    proj: Any,
    *,
    objects: Iterable[Mapping[str, Any]] | None = None,
) -> DeliveryManifestV1:
    """Build a manifest from validated projection data.

    Only cards reachable from supported claims or explicit object references
    are included, preventing unrelated ledger cards from becoming deliverables.
    Callers must run preflight_delivery first.
    """

    claims_map = getattr(proj, "claims", {}) or {}
    cards_map = getattr(proj, "cards", {}) or {}
    refs = _object_refs(objects)
    reachable = {card_id for ref in refs for card_id in ref.get("card_ids", [])}
    claims: list[dict[str, Any]] = []
    for claim_id in sorted(claims_map):
        claim = claims_map[claim_id]
        if getattr(claim, "status", None) != "supported":
            continue
        card_ids = [str(card_id) for card_id in getattr(claim, "cards", []) or []]
        reachable.update(card_ids)
        claims.append(
            {
                "claim_id": str(claim.claim_id),
                "kind": str(claim.kind),
                "card_ids": sorted(card_ids),
            }
        )
    card_entries: list[dict[str, Any]] = []
    for card_id in sorted(reachable):
        card = cards_map.get(card_id)
        if card is None:
            continue
        safe_locator = _safe_locator(getattr(card, "locator", {}))
        card_entries.append(
            {
                "card_id": str(card.card_id),
                "kind": str(card.kind),
                "locator_digest": _digest(safe_locator),
                "machine_hash": getattr(card, "machine_hash", None),
            }
        )
    return DeliveryManifestV1(
        idea_id=str(getattr(proj, "idea_id", "")),
        claims=tuple(claims),
        cards=tuple(card_entries),
        objects=tuple(refs),
    )


def preflight_delivery(
    proj: Any,
    *,
    objects: Iterable[Mapping[str, Any]] | None = None,
    tables: Iterable[Any] | None = None,
    claim_texts: Iterable[Mapping[str, Any]] | None = None,
    citation_texts: Iterable[Mapping[str, Any]] | None = None,
    library: Any = None,
    figure_items: Iterable[Mapping[str, Any]] | None = None,
) -> DeliveryPreflight:
    """Validate projection and optional rendered objects, then build a manifest."""

    refs = _object_refs(objects)
    issues = _validate_projection(proj)
    issues.extend(_validate_object_refs(proj, refs))
    issues.extend(_validate_tables(proj, tables))
    issues.extend(_validate_claim_texts(proj, claim_texts))
    issues.extend(_validate_citations(proj, citation_texts, library))
    issues.extend(_validate_figures(proj, figure_items))
    issues = sorted(issues, key=lambda item: (item.code, item.object_type, item.object_id, item.detail))
    if issues:
        return DeliveryPreflight(issues=tuple(issues))
    manifest = build_manifest(proj, objects=refs)
    return DeliveryPreflight(manifest=manifest)


__all__ = [
    "MANIFEST_KIND",
    "MANIFEST_SCHEMA_VERSION",
    "DeliveryIssue",
    "DeliveryManifestV1",
    "DeliveryPreflight",
    "build_manifest",
    "preflight_delivery",
]
