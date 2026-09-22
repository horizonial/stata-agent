"""Controlled filesystem publication for user-approved instruction-only Skills."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from stata_research_agent.application.skill_change import SkillChangePublicationPlan
from stata_research_agent.application.skill_evolution import (
    DeactivatedSkillReceipt,
    PublishedSkillReceipt,
    SkillActivationPlan,
    SkillDeactivationPlan,
    SkillVersionAdoptionPlan,
)


class WorkspaceSkillPublisher:
    def publish_change(
        self, workspace_root: Path, plan: SkillChangePublicationPlan
    ) -> PublishedSkillReceipt:
        root = workspace_root.resolve(strict=True)
        target = root / "skills" / plan.skill_name / "SKILL.md"
        self._require_confined(root, target)
        if not target.exists():
            raise ValueError("current Skill payload is missing")
        self._require_regular(target)
        current_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        if current_hash not in {plan.base_skill_sha256, plan.skill_sha256}:
            raise ValueError("current Skill payload does not match the approved base")
        receipt = self._publish(
            workspace_root,
            skill_name=plan.skill_name,
            skill_markdown=plan.skill_markdown,
            skill_sha256=plan.skill_sha256,
            publication_key=plan.command_id.value,
        )
        if receipt.activation_kind != "idempotent_reconcile":
            return receipt
        history = root / ".stata-agent" / "skill-history" / plan.command_id.value / "SKILL.md"
        self._require_confined(root, history)
        if not history.exists():
            raise ValueError("Skill change reconciliation history is missing")
        self._require_regular(history)
        prior_hash = hashlib.sha256(history.read_bytes()).hexdigest()
        if prior_hash != plan.base_skill_sha256:
            raise ValueError("Skill change reconciliation predecessor is invalid")
        return PublishedSkillReceipt(
            receipt.relative_skill_path,
            receipt.installed_sha256,
            prior_hash,
            receipt.activation_kind,
        )

    def publish(self, workspace_root: Path, plan: SkillActivationPlan) -> PublishedSkillReceipt:
        return self._publish(
            workspace_root,
            skill_name=plan.skill_name,
            skill_markdown=plan.skill_markdown,
            skill_sha256=plan.skill_sha256,
            publication_key=plan.candidate_id.value,
        )

    def publish_version(
        self, workspace_root: Path, plan: SkillVersionAdoptionPlan
    ) -> PublishedSkillReceipt:
        return self._publish(
            workspace_root,
            skill_name=plan.skill_name,
            skill_markdown=plan.skill_markdown,
            skill_sha256=plan.skill_sha256,
            publication_key=plan.command_id.value,
        )

    def deactivate(
        self, workspace_root: Path, plan: SkillDeactivationPlan
    ) -> DeactivatedSkillReceipt:
        root = workspace_root.resolve(strict=True)
        relative = Path("skills") / plan.skill_name / "SKILL.md"
        target = root / relative
        history = (
            root
            / ".stata-agent"
            / "skill-history"
            / "deactivation"
            / plan.command_id.value
            / "SKILL.md"
        )
        self._require_confined(root, target)
        self._require_confined(root, history)
        if not target.exists():
            if not history.exists():
                raise ValueError("active Skill payload is missing")
            self._require_regular(history)
            archived_hash = hashlib.sha256(history.read_bytes()).hexdigest()
            if archived_hash != plan.current_sha256:
                raise ValueError("deactivated Skill history conflicts with current version")
            return DeactivatedSkillReceipt(
                relative.as_posix(), archived_hash, "idempotent_deactivation"
            )
        self._require_regular(target)
        payload = target.read_bytes()
        prior_hash = hashlib.sha256(payload).hexdigest()
        if prior_hash != plan.current_sha256:
            raise ValueError("active Skill payload changed before deactivation")
        history.parent.mkdir(parents=True, exist_ok=True)
        if history.exists():
            self._require_regular(history)
            if history.read_bytes() != payload:
                raise ValueError("Skill deactivation history identity conflicts")
            target.unlink()
        else:
            os.replace(target, history)
        return DeactivatedSkillReceipt(relative.as_posix(), prior_hash, "deactivated")

    def _publish(
        self,
        workspace_root: Path,
        *,
        skill_name: str,
        skill_markdown: str,
        skill_sha256: str,
        publication_key: str,
    ) -> PublishedSkillReceipt:
        root = workspace_root.resolve(strict=True)
        expected_hash = hashlib.sha256(skill_markdown.encode("utf-8")).hexdigest()
        if expected_hash != skill_sha256:
            raise ValueError("approved Skill content hash is invalid")
        relative = Path("skills") / skill_name / "SKILL.md"
        target = root / relative
        self._require_confined(root, target)
        prior_hash: str | None = None
        activation_kind = "new"
        if target.exists():
            self._require_regular(target)
            prior = target.read_bytes()
            prior_hash = hashlib.sha256(prior).hexdigest()
            if prior_hash == skill_sha256:
                return PublishedSkillReceipt(
                    relative.as_posix(), skill_sha256, prior_hash, "idempotent_reconcile"
                )
            activation_kind = "version_update"
            history = root / ".stata-agent" / "skill-history" / publication_key / "SKILL.md"
            self._require_confined(root, history)
            history.parent.mkdir(parents=True, exist_ok=True)
            if history.exists():
                self._require_regular(history)
                if history.read_bytes() != prior:
                    raise ValueError("Skill history identity conflicts with prior version")
            else:
                history.write_bytes(prior)
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = root / ".stata-agent" / "staging" / "skills" / publication_key / "SKILL.md"
        self._require_confined(root, staging)
        staging.parent.mkdir(parents=True, exist_ok=True)
        staging.write_text(skill_markdown, encoding="utf-8", newline="\n")
        if hashlib.sha256(staging.read_bytes()).hexdigest() != skill_sha256:
            raise ValueError("staged Skill failed integrity validation")
        os.replace(staging, target)
        self._require_regular(target)
        if hashlib.sha256(target.read_bytes()).hexdigest() != skill_sha256:
            raise ValueError("installed Skill failed integrity validation")
        return PublishedSkillReceipt(relative.as_posix(), skill_sha256, prior_hash, activation_kind)

    @staticmethod
    def _require_confined(root: Path, path: Path) -> None:
        parent = path.parent.resolve()
        if not parent.is_relative_to(root):
            raise ValueError("Skill path escaped the Workspace")
        for ancestor in (parent, *parent.parents):
            if ancestor == root.parent:
                break
            if ancestor.exists() and ancestor.is_symlink():
                raise ValueError("Skill path cannot traverse a symlink")
            if ancestor == root:
                break

    @staticmethod
    def _require_regular(path: Path) -> None:
        if path.is_symlink() or not path.is_file():
            raise ValueError("Skill payload must be a regular file")
        attributes = getattr(path.stat(), "st_file_attributes", 0)
        if attributes & getattr(os, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise ValueError("Skill payload cannot be a reparse point")
