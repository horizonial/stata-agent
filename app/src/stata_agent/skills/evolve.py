"""Governed Skill evolution: deterministic candidate -> review -> promotion.

The module deliberately remains a file-based governance layer.  It does not
run examples, call a model, or decide that a candidate is approved.  A
candidate is only eligible when its source metadata says that a stable episode
produced it; a human approval record then binds the exact candidate digest to
an atomic promotion.  Older flat ``<slug>_e<n>.md`` files remain readable, but
the promoter keeps only one active copy in the catalog and moves older copies
under ``_history``.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

from .loader import MAX_SKILL_BYTES, Skill, SkillLoadError, load_skill


MAX_CANDIDATE_BYTES = MAX_SKILL_BYTES
MAX_CANDIDATE_FIELD_CHARS = 4096
MAX_EPISODE_VARIANTS = 64
MANIFEST_SCHEMA = "stata-agent.skill-candidate.v2"
ACTIVE_MANIFEST_SCHEMA = "stata-agent.skill-active.v1"
APPROVAL_SCHEMA = "stata-agent.skill-approval.v1"
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_VERSIONED_NAME_RE = re.compile(r"^(?P<slug>[a-z0-9][a-z0-9_-]{0,63})_e(?P<n>[1-9][0-9]*)\.md$")
_AUTOMATED_REVIEWERS = frozenset({"agent", "model", "system", "auto", "automated"})


class SkillEvolutionError(ValueError):
    """A candidate or promotion request cannot be trusted."""


class CandidateConflictError(SkillEvolutionError):
    """A candidate/manifest/target already exists with different content."""


class ApprovalError(PermissionError):
    """The required human approval is missing or does not match the content."""


@dataclass
class EpisodeVariant:
    id: str
    label: str
    y: str
    cluster: str
    reason: str = ""
    # Kept for source compatibility.  Raw machine results are intentionally
    # never copied into a candidate Skill.
    machine: dict = field(default_factory=dict)
    method: str = ""


@dataclass(frozen=True)
class ApprovalRecord:
    """Content-bound human decision required by :func:`promote`."""

    candidate_sha256: str
    reviewer: str
    timestamp: str
    decision: str = "approve"


# A descriptive alias makes integrations that call this a SkillApproval stay
# source-compatible without creating a second approval model.
SkillApproval = ApprovalRecord


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_")
    return s or "evolved_skill"


def _safe_slug(value: Any, *, field_name: str = "name") -> str:
    if not isinstance(value, str):
        raise SkillEvolutionError(f"{field_name} must be a string")
    text = value.strip()
    if not _SLUG_RE.fullmatch(text):
        raise SkillEvolutionError(f"{field_name} must be a safe lowercase slug")
    return text


def _safe_line(value: Any, *, field_name: str, required: bool = True) -> str:
    if not isinstance(value, str):
        raise SkillEvolutionError(f"{field_name} must be a string")
    text = value.strip()
    if required and not text:
        raise SkillEvolutionError(f"{field_name} must not be empty")
    if len(text) > MAX_CANDIDATE_FIELD_CHARS:
        raise SkillEvolutionError(f"{field_name} is too long")
    if any(ord(char) < 32 and char not in "\t" for char in text):
        raise SkillEvolutionError(f"{field_name} contains a control character")
    if "\r" in text or "\n" in text:
        raise SkillEvolutionError(f"{field_name} must be single-line")
    return text


def _json_quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _next_version(existing: list[str], name: str) -> str:
    """Return the next flat active filename for legacy-compatible catalogs."""

    n = 0
    for path in existing:
        match = re.fullmatch(rf"{re.escape(name)}_e(\d+)\.md", path)
        if match:
            n = max(n, int(match.group(1)))
    return f"{name}_e{n + 1}.md"


def _normalise_variants(episode: Sequence[EpisodeVariant]) -> list[EpisodeVariant]:
    if not isinstance(episode, Sequence) or isinstance(episode, (str, bytes)):
        raise SkillEvolutionError("episode must be a sequence of variants")
    if not episode or len(episode) > MAX_EPISODE_VARIANTS:
        raise SkillEvolutionError("episode must contain between 1 and 64 variants")
    out: list[EpisodeVariant] = []
    seen: set[str] = set()
    for variant in episode:
        if not isinstance(variant, EpisodeVariant):
            raise SkillEvolutionError("episode items must be EpisodeVariant values")
        variant_id = _safe_line(variant.id, field_name="variant.id")
        if variant_id in seen:
            raise SkillEvolutionError(f"duplicate variant id: {variant_id}")
        seen.add(variant_id)
        _safe_line(variant.label, field_name="variant.label")
        _safe_line(variant.y, field_name="variant.y")
        _safe_line(variant.cluster, field_name="variant.cluster")
        _safe_line(variant.reason, field_name="variant.reason")
        if not isinstance(variant.machine, dict):
            raise SkillEvolutionError("variant.machine must be a mapping")
        if variant.method:
            _safe_slug(variant.method, field_name="variant.method")
        out.append(variant)
    return out


def skill_candidate_md(
    *,
    name: str,
    task: str,
    episode: list[EpisodeVariant],
    stable: bool,
    source_note: str = "",
    method: str = "did",
    role: str = "methodology",
    version: str = "0.1.0",
    phases: Sequence[str] = ("ESTIMATION",),
    requires_ados: Sequence[str] = ("reghdfe", "esttab"),
    stata_min: str = "17",
    prechecks: Sequence[str] = ("data.has_panel_keys",),
    steps: Sequence[str] = (),
    rules: Sequence[str] = (),
    disabled_when: Sequence[str] = (),
    allowed_tools: Sequence[str] = (),
) -> str:
    """Render a bounded Skill V2 policy package from a stable episode.

    The raw ``machine`` dictionaries are accepted for compatibility with the
    episode producer but are not serialized.  Only bounded variant metadata
    and governance provenance are emitted; execution remains the responsibility
    of existing tools and validators.
    """

    if stable is not True:
        raise SkillEvolutionError("only stable episodes may produce candidates")
    variants = _normalise_variants(episode)
    slug = _slug(name)
    slug = _safe_slug(slug)
    task_text = _safe_line(task, field_name="task")
    source_text = _safe_line(source_note or "episode", field_name="source")
    method_slug = _safe_slug(method, field_name="method")
    role_text = _safe_line(role, field_name="role")
    version_text = _safe_line(version, field_name="version")
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", version_text):
        raise SkillEvolutionError("version must be semantic version text")
    phase_values = [_safe_line(item, field_name="phase") for item in phases]
    ado_values = [_safe_line(item, field_name="requires_ados") for item in requires_ados]
    for ado in ado_values:
        if not re.fullmatch(r"[A-Za-z0-9_]+", ado):
            raise SkillEvolutionError("requires_ados contains an invalid ado name")
    stata_text = _safe_line(str(stata_min), field_name="stata_min")
    if not re.fullmatch(r"\d+(?:\.\d+)?", stata_text):
        raise SkillEvolutionError("stata_min must be numeric text")
    precheck_values = [_safe_line(item, field_name="precheck") for item in prechecks]
    if not precheck_values:
        raise SkillEvolutionError("at least one precheck is required")
    step_values = [_safe_line(item, field_name="step") for item in steps]
    if not step_values:
        if method_slug == "did":
            step_values = [
                "确认处理组、期后与面板键",
                "先执行事件研究或平行趋势诊断，再运行主 spec",
                "保留完整 spec family 并说明主结果选择理由",
            ]
        else:
            step_values = [f"先确认 {method_slug} 的识别假设，再运行主 spec", "保留完整 spec family 并记录选择理由"]
    rule_values = [_safe_line(item, field_name="rule") for item in rules]
    if not rule_values:
        rule_values = [
            "forbid: 单期无事件研究却宣称平行趋势",
            "forbid: 多 spec 只挑显著报（全 family 留痕，选主结果须给理由）",
            "require: 进稿数字来自 run→card；报告注明聚类口径",
        ]
    disabled_values = [_safe_line(item, field_name="disabled_when") for item in disabled_when]
    tool_values = [_safe_line(item, field_name="allowed_tools") for item in allowed_tools]
    for variant in variants:
        if variant.method and _safe_slug(variant.method, field_name="variant.method") != method_slug:
            raise SkillEvolutionError("all episode variants must use the candidate method")
    examples = [
        {
            "id": _safe_line(variant.id, field_name="variant.id"),
            "input": {"outcome": _safe_line(variant.y, field_name="variant.y"),
                      "cluster": _safe_line(variant.cluster, field_name="variant.cluster")},
            "expected": {"label": _safe_line(variant.label, field_name="variant.label"),
                         "reason": _safe_line(variant.reason, field_name="variant.reason")},
        }
        for variant in variants
    ]
    rules_yaml = "\n".join(f"  - {_json_quote(item)}" for item in rule_values)
    prechecks_yaml = "\n".join(f"  - {_json_quote(item)}" for item in precheck_values)
    steps_yaml = "\n".join(f"  - {_json_quote(item)}" for item in step_values)
    phases_yaml = "\n".join(f"  - phase: {_json_quote(item)}" for item in phase_values)
    needs_yaml = f"  - needs: [{_json_quote(method_slug)}]"
    disabled_yaml = "\n".join(f"  - {_json_quote(item)}" for item in disabled_values)
    tools_line = json.dumps(tool_values, ensure_ascii=False)
    examples_json = json.dumps(examples, ensure_ascii=False, separators=(",", ":"))
    evolution_yaml = "\n".join(
        [
            "evolution:",
            "  stable: true",
            f"  source: {_json_quote(source_text)}",
            f"  task: {_json_quote(task_text)}",
            f"  method: {_json_quote(method_slug)}",
            f"  episode_count: {len(variants)}",
        ]
    )
    method_line = (
        f"使用 {method_slug} 方法；变量映射与具体数据由调用方注入，"
        "本 Skill 只声明可审计的政策、前置检查与示例。"
    )
    return f"""---
schema_version: 2
name: {slug}
version: {version_text}
role: {role_text}
triggers:
{phases_yaml}
{needs_yaml}
requires:
  ados: {json.dumps(ado_values, ensure_ascii=False)}
  stata_min: {_json_quote(stata_text)}
prechecks:
{prechecks_yaml}
steps:
{steps_yaml}
rules:
{rules_yaml}
examples: {examples_json}
{"disabled_when: []" if not disabled_yaml else "disabled_when:\n" + disabled_yaml}
allowed_tools: {tools_line}
{evolution_yaml}
---

# {slug}

> 由稳定 episode 提炼。任务：{task_text}。来源：{source_text}。
> 方法学：{method_line}

## examples

这些 examples 只用于人工 review 与确定性回归，不是模型可执行指令。

```json
{json.dumps(examples, ensure_ascii=False, indent=2)}
```

## evidence threshold

- L-R：rc=0 + 结构化(coef/SE/N) + 可复现 do + env_sig。
- 主结果入选须带理由；模型不能直接签 EvidenceCard/Claim。
"""


def _sha256_bytes(data: bytes) -> str:
    return sha256(data).hexdigest()


def _root_path(skills_dir: str | Path) -> Path:
    root = Path(skills_dir).expanduser()
    if root.exists() and root.is_symlink():
        raise SkillEvolutionError("skills_dir must not be a symlink")
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _contained(root: Path, path: Path, *, label: str) -> Path:
    candidate = path.resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise SkillEvolutionError(f"{label} escapes skills_dir") from error
    current = path
    while current != root and current != current.parent:
        if current.exists() and current.is_symlink():
            raise SkillEvolutionError(f"{label} traverses a symlink")
        current = current.parent
    return candidate


def _mkdir_contained(root: Path, relative: str) -> Path:
    path = root / relative
    _contained(root, path, label=relative)
    if path.exists() and path.is_symlink():
        raise SkillEvolutionError(f"{relative} must not be a symlink")
    path.mkdir(parents=True, exist_ok=True)
    return _contained(root, path, label=relative)


@contextmanager
def _evolution_lock(root: Path):
    lock = root / ".skill-evolve.lock"
    _contained(root, lock, label="evolution lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as error:
        raise CandidateConflictError("another Skill promotion is in progress") from error
    try:
        os.write(fd, f"pid={os.getpid()}\n".encode("ascii"))
        os.fsync(fd)
        yield
    finally:
        os.close(fd)
        try:
            lock.unlink()
        except FileNotFoundError:
            pass


def _write_exclusive(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags)
    try:
        with os.fdopen(fd, "wb") as output:
            fd = -1
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
    finally:
        if fd >= 0:
            os.close(fd)


def _write_temp(parent: Path, data: bytes, *, suffix: str) -> Path:
    handle, raw_path = tempfile.mkstemp(prefix=".skill-evolve-", suffix=suffix, dir=parent)
    path = Path(raw_path)
    try:
        with os.fdopen(handle, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        return path
    except BaseException:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise


def _validate_evolution_metadata(skill: Skill) -> None:
    if skill.schema_version != 2:
        raise SkillEvolutionError("candidate requires schema_version=2")
    evolution = dict(skill.evolution)
    if evolution.get("stable") is not True:
        raise SkillEvolutionError("candidate source episode is not marked stable")
    source = evolution.get("source")
    if not isinstance(source, str) or not source.strip():
        raise SkillEvolutionError("candidate requires non-empty evolution.source")
    if not skill.prechecks or not skill.steps or not skill.rules or not skill.examples:
        raise SkillEvolutionError("candidate requires prechecks, steps, rules, and examples")
    _safe_slug(skill.slug, field_name="candidate name")


def _manifest_path(candidate: Path) -> Path:
    return candidate.with_suffix(".manifest.json")


def _manifest_data(skill: Skill, data: bytes, *, created_at: str | None = None) -> dict[str, Any]:
    digest = _sha256_bytes(data)
    return {
        "schema": MANIFEST_SCHEMA,
        "candidate_id": f"cand_{digest[:24]}",
        "slug": skill.slug,
        "sha256": digest,
        "size": len(data),
        "created_at": created_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "stable": True,
        "source": str(skill.evolution.get("source", ""))[:MAX_CANDIDATE_FIELD_CHARS],
    }


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise SkillEvolutionError("candidate manifest is unreadable") from error
    if not isinstance(raw, dict) or raw.get("schema") != MANIFEST_SCHEMA:
        raise SkillEvolutionError("candidate manifest schema is invalid")
    return raw


def _validate_staged_candidate(path: Path, root: Path) -> tuple[Skill, bytes, dict[str, Any]]:
    staging = _mkdir_contained(root, "_staging")
    resolved = _contained(root, path, label="candidate")
    try:
        resolved.relative_to(staging)
    except ValueError as error:
        raise SkillEvolutionError("candidate must be inside _staging") from error
    if resolved.suffix.lower() != ".md" or resolved.is_symlink():
        raise SkillEvolutionError("candidate must be a regular Markdown file")
    try:
        data = resolved.read_bytes()
    except OSError as error:
        raise SkillEvolutionError("candidate cannot be read") from error
    if len(data) > MAX_CANDIDATE_BYTES:
        raise SkillEvolutionError("candidate exceeds size limit")
    manifest_path = _manifest_path(resolved)
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise SkillEvolutionError("candidate manifest is missing")
    manifest = _read_manifest(manifest_path)
    digest = _sha256_bytes(data)
    if manifest.get("sha256") != digest or manifest.get("size") != len(data):
        raise SkillEvolutionError("candidate digest does not match manifest")
    try:
        skill = load_skill(resolved)
    except SkillLoadError as error:
        raise SkillEvolutionError("candidate Skill schema is invalid") from error
    _validate_evolution_metadata(skill)
    if manifest.get("candidate_id") != f"cand_{digest[:24]}":
        raise SkillEvolutionError("candidate manifest ID does not match digest")
    if manifest.get("stable") is not True or manifest.get("source") != skill.evolution.get("source"):
        raise SkillEvolutionError("candidate manifest provenance mismatch")
    if manifest.get("slug") != skill.slug:
        raise SkillEvolutionError("candidate manifest slug mismatch")
    return skill, data, manifest


def stage(candidate_md: str, *, skills_dir: str | Path, name: str | None = None) -> Path:
    """Validate and atomically stage one stable Skill V2 candidate."""

    if not isinstance(candidate_md, str):
        raise SkillEvolutionError("candidate_md must be text")
    data = candidate_md.encode("utf-8")
    if len(data) > MAX_CANDIDATE_BYTES:
        raise SkillEvolutionError("candidate exceeds size limit")
    root = _root_path(skills_dir)
    staging = _mkdir_contained(root, "_staging")
    temporary: Path | None = None
    try:
        temporary = _write_temp(staging, data, suffix=".md")
        try:
            skill = load_skill(temporary)
        except SkillLoadError as error:
            raise SkillEvolutionError("candidate Skill schema is invalid") from error
        _validate_evolution_metadata(skill)
        slug = _safe_slug(name or skill.slug)
        if name is not None and slug != skill.slug:
            raise SkillEvolutionError("staging name must match candidate frontmatter name")
        digest = _sha256_bytes(data)
        candidate_id = f"cand_{digest[:24]}"
        target = _contained(root, staging / f"{slug}--{candidate_id}.md", label="staged candidate")
        manifest_path = _manifest_path(target)
        manifest_data = json.dumps(
            _manifest_data(skill, data), ensure_ascii=False, sort_keys=True, indent=2
        ).encode("utf-8")
        with _evolution_lock(root):
            if target.exists():
                if target.is_symlink() or target.read_bytes() != data:
                    raise CandidateConflictError("staged candidate already exists with different content")
                if (
                    not manifest_path.is_file()
                    or manifest_path.is_symlink()
                    or _read_manifest(manifest_path).get("sha256") != digest
                ):
                    raise CandidateConflictError("staged candidate manifest conflicts")
                return target
            os.replace(temporary, target)
            temporary = None
            try:
                _write_exclusive(manifest_path, manifest_data)
            except BaseException:
                try:
                    target.unlink()
                except FileNotFoundError:
                    pass
                raise
        return target
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _coerce_approval(value: ApprovalRecord | Mapping[str, Any] | None) -> ApprovalRecord:
    if isinstance(value, ApprovalRecord):
        record = value
    elif isinstance(value, Mapping):
        try:
            record = ApprovalRecord(
                candidate_sha256=value["candidate_sha256"],
                reviewer=value["reviewer"],
                timestamp=value["timestamp"],
                decision=value.get("decision", "approve"),
            )
        except (KeyError, TypeError) as error:
            raise ApprovalError("approval record is incomplete") from error
    else:
        raise ApprovalError("human approval record is required")
    if not isinstance(record.candidate_sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", record.candidate_sha256):
        raise ApprovalError("approval candidate_sha256 must be a SHA-256 digest")
    reviewer = _safe_line(record.reviewer, field_name="approval.reviewer")
    if reviewer.casefold() in _AUTOMATED_REVIEWERS:
        raise ApprovalError("approval reviewer must be human")
    timestamp = _safe_line(record.timestamp, field_name="approval.timestamp")
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as error:
        raise ApprovalError("approval timestamp must be ISO-8601") from error
    if parsed.tzinfo is None:
        raise ApprovalError("approval timestamp must include a timezone")
    decision = _safe_line(record.decision, field_name="approval.decision").casefold()
    if decision not in {"approve", "approved"}:
        raise ApprovalError("only an explicit approve decision can promote")
    return ApprovalRecord(
        candidate_sha256=record.candidate_sha256.lower(),
        reviewer=reviewer,
        timestamp=timestamp,
        decision="approve",
    )


def _find_staged_by_digest(root: Path, data: bytes) -> Path:
    staging = _mkdir_contained(root, "_staging")
    digest = _sha256_bytes(data)
    matches: list[Path] = []
    for path in staging.glob("*.md"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            if _sha256_bytes(path.read_bytes()) == digest:
                matches.append(path)
        except OSError:
            continue
    if len(matches) != 1:
        raise SkillEvolutionError("candidate must identify exactly one staged artifact")
    return matches[0]


def _active_files(root: Path, slug: str) -> list[Path]:
    files: list[Path] = []
    canonical = root / f"{slug}.md"
    if canonical.exists():
        files.append(canonical)
    for path in root.glob(f"{slug}_e*.md"):
        match = _VERSIONED_NAME_RE.fullmatch(path.name)
        if match and match.group("slug") == slug:
            files.append(path)
    return sorted(set(files), key=lambda item: item.name)


def _next_active_number(root: Path, slug: str) -> int:
    maximum = 0
    paths = list(root.glob(f"{slug}_e*.md"))
    history = root / "_history" / slug
    if history.is_dir() and not history.is_symlink():
        paths.extend(history.glob(f"{slug}_e*.md"))
    for path in paths:
        match = _VERSIONED_NAME_RE.fullmatch(path.name)
        if match and match.group("slug") == slug:
            maximum = max(maximum, int(match.group("n")))
    return maximum + 1


def promote(
    candidate_md: str | Path | None = None,
    *,
    skills_dir: str | Path,
    approved: bool = False,
    approval: ApprovalRecord | Mapping[str, Any] | None = None,
    candidate_path: str | Path | None = None,
    telemetry=None,
    idea: str | None = None,
) -> Path:
    """Promote one staged candidate after an explicit human approval.

    ``candidate_md`` remains the first parameter for source compatibility. A
    raw string is only accepted when the same bytes identify exactly one
    staged candidate; callers may pass the returned ``Path`` from :func:`stage`
    or ``candidate_path=...`` directly.  No path supplied by the candidate
    itself is trusted.
    """

    if approved is not True:
        raise ApprovalError("promote requires approved=True and a human approval record")
    approval_record = _coerce_approval(approval)
    root = _root_path(skills_dir)
    if candidate_path is not None:
        source_path = _contained(root, Path(candidate_path), label="candidate")
        if candidate_md is not None and isinstance(candidate_md, str):
            try:
                supplied = candidate_md.encode("utf-8")
                if source_path.read_bytes() != supplied:
                    raise CandidateConflictError("candidate text does not match candidate_path")
            except OSError as error:
                raise SkillEvolutionError("candidate cannot be read") from error
    elif isinstance(candidate_md, Path):
        source_path = candidate_md
    elif isinstance(candidate_md, str):
        source_path = _find_staged_by_digest(root, candidate_md.encode("utf-8"))
    else:
        raise SkillEvolutionError("a staged candidate is required")
    skill, data, manifest = _validate_staged_candidate(source_path, root)
    digest = _sha256_bytes(data)
    if approval_record.candidate_sha256 != digest:
        raise ApprovalError("approval digest does not match candidate")
    if manifest.get("candidate_id") != f"cand_{digest[:24]}":
        raise SkillEvolutionError("candidate ID does not match digest")
    slug = _safe_slug(skill.slug, field_name="candidate name")
    history = _mkdir_contained(root, f"_history/{slug}")
    target: Path | None = None
    temporary: Path | None = None
    active_manifest_temp: Path | None = None
    moved: list[tuple[Path, Path]] = []
    try:
        with _evolution_lock(root):
            active = _active_files(root, slug)
            number = _next_active_number(root, slug)
            target = _contained(root, root / f"{slug}_e{number}.md", label="active Skill")
            if target.exists() or target.is_symlink():
                raise CandidateConflictError("active Skill target already exists")
            history_targets = [history / item.name for item in active]
            for old, destination in zip(active, history_targets, strict=True):
                _contained(root, old, label="active Skill")
                _contained(root, destination, label="Skill history")
                if old.is_symlink() or destination.exists() or destination.is_symlink():
                    raise CandidateConflictError("active/history target is unsafe or already occupied")
            temporary = _write_temp(root, data, suffix=".md")
            active_manifest = {
                "schema": ACTIVE_MANIFEST_SCHEMA,
                "slug": slug,
                "filename": target.name,
                "version": skill.version,
                "candidate_sha256": digest,
                "approval": {
                    "schema": APPROVAL_SCHEMA,
                    "reviewer": approval_record.reviewer,
                    "timestamp": approval_record.timestamp,
                    "decision": approval_record.decision,
                },
            }
            active_manifest_temp = _write_temp(
                root,
                json.dumps(active_manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8"),
                suffix=".manifest.json",
            )
            for old, destination in zip(active, history_targets, strict=True):
                os.replace(old, destination)
                moved.append((old, destination))
            os.replace(temporary, target)
            temporary = None
            os.replace(active_manifest_temp, _manifest_path(target))
            active_manifest_temp = None
    except BaseException:
        if target is not None and target.exists() and not target.is_symlink():
            try:
                target.unlink()
            except OSError:
                pass
        for old, destination in reversed(moved):
            if destination.exists() and not destination.is_symlink() and not old.exists():
                try:
                    os.replace(destination, old)
                except OSError:
                    pass
        raise
    finally:
        for path in (temporary, active_manifest_temp):
            if path is not None:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
    assert target is not None
    if telemetry is not None:
        telemetry.record(
            kind="skill.evolved",
            idea=idea,
            detail=f"promote -> {target.name} (sha256={digest[:16]}, reviewer={approval_record.reviewer})",
        )
    return target


__all__ = [
    "APPROVAL_SCHEMA",
    "ApprovalError",
    "ApprovalRecord",
    "CandidateConflictError",
    "EpisodeVariant",
    "MANIFEST_SCHEMA",
    "SkillApproval",
    "SkillEvolutionError",
    "stage",
    "promote",
    "skill_candidate_md",
]
