"""Deterministic policy for turning stable Project Memory into reviewable Skills.

The Curator may suggest content, but it cannot define a Skill package or decide that a
candidate is safe.  This module deliberately supports only instruction-only Skills: no
scripts, executables, network resources, or system-prompt replacement.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from .memory_curator import SkillEvolutionProposal

POLICY_REVISION = "memory-to-skill-v1"
EVALUATION_CHANGE_POLICY_REVISION = "evaluation-to-skill-v1"
ELIGIBLE_MEMORY_KINDS = frozenset({"user_preference", "feedback", "project_procedure"})
_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_BLOCKED_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "system_prompt_override",
        re.compile(r"ignore\s+(all\s+)?previous|override\s+system\s+prompt", re.I),
    ),
    (
        "audit_bypass",
        re.compile(
            r"bypass\s+(trace|evidence|confirmation|approval)|disable\s+(trace|audit)", re.I
        ),
    ),
    (
        "fabrication_instruction",
        re.compile(r"fabricat(e|ion)|invent\s+(results?|numbers?|evidence)", re.I),
    ),
    (
        "credential_request",
        re.compile(
            r"(reveal|print|exfiltrate|collect).{0,32}"
            r"(api[-_ ]?key|password|credential|secret)",
            re.I,
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class ValidatedSkillCandidate:
    skill_name: str
    proposed_version: str
    description: str
    instruction_body: str
    skill_markdown: str
    skill_sha256: str
    rationale: str
    policy_revision: str
    validation_status: str
    validation_findings: tuple[str, ...]


def validate_and_render_skill(
    proposal: SkillEvolutionProposal,
    *,
    proposed_version: str,
    source_kinds: tuple[str, ...],
) -> ValidatedSkillCandidate:
    validated = _validate_and_render(
        skill_name=proposal.skill_name,
        description=proposal.description,
        instruction_body=proposal.instruction_body,
        rationale=proposal.rationale,
        proposed_version=proposed_version,
        source="memory-evolution",
        policy_revision=POLICY_REVISION,
    )
    findings = list(validated.validation_findings)
    if len(set(proposal.source_memory_item_ids)) < 2:
        findings.append("insufficient_distinct_memory_sources")
    if any(kind not in ELIGIBLE_MEMORY_KINDS for kind in source_kinds):
        findings.append("ineligible_memory_source_kind")
    unique_findings = tuple(dict.fromkeys(findings))
    return ValidatedSkillCandidate(
        validated.skill_name,
        validated.proposed_version,
        validated.description,
        validated.instruction_body,
        validated.skill_markdown,
        validated.skill_sha256,
        validated.rationale,
        validated.policy_revision,
        "blocked" if unique_findings else "passed",
        unique_findings,
    )


def validate_and_render_evaluation_change(
    *,
    skill_name: str,
    description: str,
    instruction_body: str,
    rationale: str,
    proposed_version: str,
) -> ValidatedSkillCandidate:
    """Render a review-only Skill change without Memory-source requirements."""

    return _validate_and_render(
        skill_name=skill_name,
        description=description,
        instruction_body=instruction_body,
        rationale=rationale,
        proposed_version=proposed_version,
        source="evaluation-proposal",
        policy_revision=EVALUATION_CHANGE_POLICY_REVISION,
    )


def _validate_and_render(
    *,
    skill_name: str,
    description: str,
    instruction_body: str,
    rationale: str,
    proposed_version: str,
    source: str,
    policy_revision: str,
) -> ValidatedSkillCandidate:
    name = skill_name.strip()
    description = " ".join(description.split())
    body = instruction_body.strip()
    rationale = rationale.strip()
    findings: list[str] = []
    if not _NAME.fullmatch(name) or len(name) > 64:
        findings.append("invalid_skill_name")
    if name == "research-main":
        findings.append("reserved_main_skill_name")
    if len(description) > 500:
        findings.append("description_too_long")
    if len(body.encode("utf-8")) > 16 * 1024:
        findings.append("instruction_body_too_large")
    combined = f"{description}\n{body}\n{rationale}"
    findings.extend(code for code, pattern in _BLOCKED_PATTERNS if pattern.search(combined))
    metadata = json.dumps(
        {
            "version": proposed_version,
            "source": source,
            "policy": policy_revision,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    markdown = (
        "---\n"
        f"name: {name}\n"
        f"description: {json.dumps(description, ensure_ascii=False)}\n"
        f"metadata: {metadata}\n"
        "---\n\n"
        f"{body}\n"
    )
    if len(markdown.encode("utf-8")) > 64 * 1024:
        findings.append("skill_package_too_large")
    unique_findings = tuple(dict.fromkeys(findings))
    return ValidatedSkillCandidate(
        name,
        proposed_version,
        description,
        body,
        markdown,
        hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
        rationale,
        policy_revision,
        "blocked" if unique_findings else "passed",
        unique_findings,
    )
