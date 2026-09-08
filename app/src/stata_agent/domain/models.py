"""领域对象最小模型（DD-01 §2）：Claim / EvidenceCard / RunRecord / ResearchState。

切片 0 只建字段与构造；完整校验(reducer 签发)见 reducers.py。
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class EvidenceCard(BaseModel):
    """由 validator/evidence_builder 签发、不可变（DD-01 §2.6）。"""

    card_id: str
    kind: str = "numeric"  # numeric|citation|sample|model|figure
    locator: dict[str, Any] = Field(default_factory=dict)
    value: Optional[dict[str, Any]] = None
    machine_hash: Optional[str] = None
    signed_by: str = "validator"
    verified_at: Optional[str] = None


class Claim(BaseModel):
    """研究级命题；由 evidence_builder 从已签发卡片合成（写入权分离）。"""

    claim_id: str
    statement: str
    kind: str = "effect"
    cards: list[str] = Field(default_factory=list)
    status: str = "supported"  # draft|supported|retracted
    written_by: str = "evidence_builder"
    superseded_by: Optional[str] = None


class RunRecord(BaseModel):
    """一次不可变执行的最小记录（DD-01 §2.5）。"""

    run_id: str
    operation_id: Optional[str] = None
    attempt_id: int = 0
    semantic_input_hash: Optional[str] = None
    side_effect: str = "read"  # read|write
    status: str = "pending"  # pending|running|succeeded|failed|uncertain|cancelled
    provenance: dict[str, Any] = Field(default_factory=dict)
    machine: dict[str, Any] = Field(default_factory=dict)


class ResearchState(BaseModel):
    """内容层研究状态最小版（DD-01 §4.3.1/§2.2）。切片 0 先留骨架字段。"""

    idea_id: str
    phase: Optional[str] = None
    current_spec_id: Optional[str] = None
    current_family_id: Optional[str] = None
    sample_sig: Optional[str] = None
    evidence_refs: list[str] = Field(default_factory=list)
