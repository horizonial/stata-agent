"""Application-level settings contracts, catalog, and effective resolver.

This module intentionally has no FastAPI, SQLite, provider, or agent-loop
dependency.  It is the single place where user-owned application settings are
typed, validated, and projected with their source and apply timing.  The
research ledger remains a separate source of truth for research state.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from .agent_budget import (
    GOAL_MAX_STEPS_DEFAULT,
    GOAL_MAX_STEPS_MAX,
    GOAL_MAX_STEPS_MIN,
    INTERACTIVE_MAX_STEPS_DEFAULT,
    INTERACTIVE_MAX_STEPS_MAX,
    INTERACTIVE_MAX_STEPS_MIN,
    MAX_TOOL_CALLS_DEFAULT,
    MAX_TOOL_CALLS_MAX,
    MAX_TOOL_CALLS_MIN,
)
from ..providers.catalog import MODEL_IDS, PROVIDER_IDS, model_profile, provider_definition
from ..settings.local_repository import (
    LocalSettingsRepository,
    RepositoryHealth,
    SETTINGS_SCHEMA,
    SettingsDocument,
    SettingsRepositoryError,
    SettingsRevisionConflict,
)


SettingSource = Literal["default", "dotenv", "user", "credential", "environment"]
ApplyMode = Literal["immediate", "next_request", "restart"]
SettingStatus = Literal["ready", "error"]

_ALLOWED_SOURCES = frozenset({"default", "dotenv", "user", "credential", "environment"})
_ALLOWED_APPLY_MODES = frozenset({"immediate", "next_request", "restart"})
_BOOL_TRUE = frozenset({"1", "true", "yes", "on"})
_BOOL_FALSE = frozenset({"0", "false", "no", "off"})
_SAFE_PATH_MAX = 4096
_SAFE_URL_MAX = 512
_PACKAGE_SKILLS_ROOT = str(Path(__file__).resolve().parents[1] / "skills")


class SettingsError(RuntimeError):
    """Base application settings error with a stable safe error code."""

    code = "settings_error"


class SettingsValidationError(SettingsError, ValueError):
    """A complete settings patch or catalog value is invalid."""

    code = "settings_invalid_value"

    def __init__(self, message: str = "invalid settings value", *, key: str | None = None) -> None:
        self.key = key
        # Never include the submitted value in a public error.  A value can be
        # a credential accidentally sent to the wrong endpoint.
        super().__init__(message)


class SettingsEnvironmentManagedError(SettingsValidationError):
    """The process environment owns a setting and prevents a shadow save."""

    code = "settings_environment_managed"


class SettingsReadOnlyError(SettingsValidationError):
    """A computed/read-only setting is not patchable."""

    code = "settings_read_only"


class SettingsPrivacyAcknowledgementRequired(SettingsValidationError):
    """Reserved application error used when a privacy relaxation lacks ACK."""

    code = "settings_privacy_acknowledgement_required"


@dataclass(frozen=True, slots=True)
class SettingDefinition:
    """One allow-listed setting and its validation/apply contract."""

    key: str
    value_type: str
    default: Any
    apply_mode: ApplyMode
    env_names: tuple[str, ...] = ()
    enum: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    sensitive: bool = False
    editable: bool = True
    path: bool = False
    must_exist: bool = False
    url: bool = False
    optional_url: bool = False
    description: str = ""

    @property
    def type(self) -> str:
        """Compatibility alias for callers that call the type ``type``."""

        return self.value_type

    @property
    def allowed_values(self) -> tuple[str, ...]:
        return self.enum

    @property
    def source_names(self) -> tuple[str, ...]:
        return self.env_names


def _definition(
    key: str,
    value_type: str,
    default: Any,
    apply_mode: ApplyMode,
    *,
    env: str | tuple[str, ...] = (),
    enum: tuple[str, ...] = (),
    minimum: int | None = None,
    maximum: int | None = None,
    sensitive: bool = False,
    editable: bool = True,
    path: bool = False,
    must_exist: bool = False,
    url: bool = False,
    optional_url: bool = False,
    description: str = "",
) -> SettingDefinition:
    names = (env,) if isinstance(env, str) else tuple(env)
    return SettingDefinition(
        key=key,
        value_type=value_type,
        default=default,
        apply_mode=apply_mode,
        env_names=names,
        enum=enum,
        minimum=minimum,
        maximum=maximum,
        sensitive=sensitive,
        editable=editable,
        path=path,
        must_exist=must_exist,
        url=url,
        optional_url=optional_url,
        description=description,
    )


def _default_skills_root() -> str:
    return _PACKAGE_SKILLS_ROOT


_CATALOG: dict[str, SettingDefinition] = {
    "provider.primary": _definition(
        "provider.primary",
        "enum",
        "auto",
        "next_request",
        env="STATA_AGENT_PROVIDER",
        enum=("auto", *PROVIDER_IDS),
        description="首选模型提供方",
    ),
    "provider.model": _definition(
        "provider.model",
        "enum",
        "auto",
        "next_request",
        env="STATA_AGENT_MODEL",
        enum=("auto", *MODEL_IDS),
        description="模型能力画像中的模型",
    ),
    "provider.live_enabled": _definition(
        "provider.live_enabled",
        "bool",
        False,
        "next_request",
        env="STATA_AGENT_LIVE",
        description="是否允许调用远端模型",
    ),
    "provider.base_url": _definition(
        "provider.base_url",
        "url",
        "",
        "next_request",
        env="STATA_AGENT_BASE_URL",
        url=True,
        optional_url=True,
        description="当前 provider 的 OpenAI 兼容端点（留空使用目录默认值）",
    ),
    "provider.deepseek.base_url": _definition(
        "provider.deepseek.base_url",
        "url",
        "https://api.deepseek.com",
        "next_request",
        env="DEEPSEEK_BASE_URL",
        url=True,
        description="DeepSeek OpenAI 兼容端点",
    ),
    "provider.qwen.base_url": _definition(
        "provider.qwen.base_url",
        "url",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "next_request",
        env="DASHSCOPE_BASE_URL",
        url=True,
        description="Qwen/DashScope OpenAI 兼容端点",
    ),
    # Credential keys are catalogued so the read model can report their
    # sensitivity.  They are never accepted in the JSON repository; Task 2
    # owns their lifecycle through SecretStore.
    "provider.deepseek.api_key": _definition(
        "provider.deepseek.api_key",
        "secret",
        None,
        "next_request",
        env="DEEPSEEK_API_KEY",
        sensitive=True,
        description="DeepSeek 凭据（仅显示是否已配置）",
    ),
    "provider.qwen.api_key": _definition(
        "provider.qwen.api_key",
        "secret",
        None,
        "next_request",
        env="DASHSCOPE_API_KEY",
        sensitive=True,
        description="Qwen 凭据（仅显示是否已配置）",
    ),
    "privacy.mode": _definition(
        "privacy.mode",
        "enum",
        "local_strict",
        "next_request",
        env="STATA_AGENT_PRIVACY",
        enum=("local_strict", "mixed_sanitized", "approved_remote"),
        description="远端数据边界",
    ),
    "executor.kind": _definition(
        "executor.kind",
        "enum",
        "disabled",
        "next_request",
        env="STATA_AGENT_EXECUTOR",
        enum=("disabled", "stata"),
        description="Stata 执行器",
    ),
    "stata.mcp_dir": _definition(
        "stata.mcp_dir",
        "path",
        None,
        "restart",
        env="STATA_MCP_DIR",
        path=True,
        must_exist=True,
        description="stata-mcp 安装目录",
    ),
    "library.root": _definition(
        "library.root",
        "path",
        None,
        "restart",
        env="STATA_AGENT_LIBRARY",
        path=True,
        must_exist=True,
        description="本地文献库目录",
    ),
    "skills.root": _definition(
        "skills.root",
        "path",
        _default_skills_root(),
        "restart",
        env="STATA_AGENT_SKILLS",
        path=True,
        must_exist=True,
        description="技能目录",
    ),
    "attachments.default_role": _definition(
        "attachments.default_role",
        "enum",
        "style_only",
        "immediate",
        env="STATA_AGENT_ATTACHMENT_DEFAULT_ROLE",
        enum=("style_only", "citable_evidence"),
        description="新附件默认角色",
    ),
    "agent.default_mode": _definition(
        "agent.default_mode",
        "enum",
        "interactive",
        "immediate",
        env="STATA_AGENT_DEFAULT_MODE",
        enum=("interactive", "goal"),
        description="默认交互模式",
    ),
    "agent.interactive_max_steps": _definition(
        "agent.interactive_max_steps",
        "int",
        INTERACTIVE_MAX_STEPS_DEFAULT,
        "next_request",
        env="STATA_AGENT_INTERACTIVE_MAX_STEPS",
        minimum=INTERACTIVE_MAX_STEPS_MIN,
        maximum=INTERACTIVE_MAX_STEPS_MAX,
        description="单条消息内部模型调用上限（每条消息重置）",
    ),
    "agent.goal_max_steps": _definition(
        "agent.goal_max_steps",
        "int",
        GOAL_MAX_STEPS_DEFAULT,
        "next_request",
        env="STATA_AGENT_GOAL_MAX_STEPS",
        minimum=GOAL_MAX_STEPS_MIN,
        maximum=GOAL_MAX_STEPS_MAX,
        description="单次目标任务内部模型调用上限",
    ),
    "agent.max_tool_calls": _definition(
        "agent.max_tool_calls",
        "int",
        MAX_TOOL_CALLS_DEFAULT,
        "next_request",
        env="STATA_AGENT_MAX_TOOL_CALLS",
        minimum=MAX_TOOL_CALLS_MIN,
        maximum=MAX_TOOL_CALLS_MAX,
        description="单条消息/目标任务的顶层工具调用上限",
    ),
    "compaction.summary_mode": _definition(
        "compaction.summary_mode",
        "enum",
        "deterministic",
        "next_request",
        env="STATA_AGENT_COMPACTION_SUMMARY",
        enum=("deterministic", "provider"),
        description="上下文压缩摘要方式",
    ),
    "memory.extraction_mode": _definition(
        "memory.extraction_mode",
        "enum",
        "off",
        "next_request",
        env="STATA_AGENT_MEMORY_EXTRACTION",
        enum=("off", "provider"),
        description="记忆候选提取方式",
    ),
    "context.max_input_tokens": _definition(
        "context.max_input_tokens",
        "int",
        16_000,
        "next_request",
        env="STATA_AGENT_CONTEXT_MAX_INPUT_TOKENS",
        minimum=1,
        maximum=1_000_000,
        description="上下文输入预算",
    ),
    "context.reserve_output_tokens": _definition(
        "context.reserve_output_tokens",
        "int",
        4_000,
        "next_request",
        env="STATA_AGENT_CONTEXT_RESERVE_OUTPUT_TOKENS",
        minimum=0,
        maximum=1_000_000,
        description="输出预留预算",
    ),
    "context.recent_tail_tokens": _definition(
        "context.recent_tail_tokens",
        "int",
        4_000,
        "next_request",
        env="STATA_AGENT_CONTEXT_RECENT_TAIL_TOKENS",
        minimum=0,
        maximum=1_000_000,
        description="近期消息预算",
    ),
    "context.memory_tokens": _definition(
        "context.memory_tokens",
        "int",
        1_500,
        "next_request",
        env="STATA_AGENT_CONTEXT_MEMORY_TOKENS",
        minimum=0,
        maximum=1_000_000,
        description="记忆预算",
    ),
    "ui.theme": _definition(
        "ui.theme",
        "enum",
        "system",
        "immediate",
        env="STATA_AGENT_UI_THEME",
        enum=("system", "light", "dark"),
        description="界面主题",
    ),
    "ui.language": _definition(
        "ui.language",
        "enum",
        "zh-CN",
        "restart",
        enum=("zh-CN",),
        editable=False,
        description="界面语言",
    ),
    "ui.port": _definition(
        "ui.port",
        "int",
        8001,
        "restart",
        env="STATA_AGENT_UI_PORT",
        minimum=1024,
        maximum=65535,
        description="本地 UI 端口",
    ),
    # Storage roots are intentionally read-only in V1.  They are projected so
    # the settings overview can identify their source without allowing a
    # browser request to relocate live application data.
    "storage.database": _definition(
        "storage.database",
        "path",
        "samples/ideas/ui/ledger.sqlite3",
        "restart",
        env="STATA_AGENT_DB",
        path=True,
        editable=False,
        description="研究账本位置（只读）",
    ),
    "storage.workspaces": _definition(
        "storage.workspaces",
        "path",
        None,
        "restart",
        env="STATA_AGENT_WORKSPACES",
        path=True,
        editable=False,
        description="工作区注册表位置（只读）",
    ),
    "storage.attachments": _definition(
        "storage.attachments",
        "path",
        None,
        "restart",
        env="STATA_AGENT_ATTACHMENTS",
        path=True,
        editable=False,
        description="附件目录（只读）",
    ),
}

# Keep one masked catalog entry for every registered provider.  These entries
# are intentionally not persistable; the secret lifecycle belongs to
# ``SecretStore`` and the settings JSON never receives a credential value.
for _provider_id in PROVIDER_IDS:
    _provider = provider_definition(_provider_id)
    if _provider is not None:
        _CATALOG.setdefault(
            f"provider.{_provider_id}.api_key",
            _definition(
                f"provider.{_provider_id}.api_key",
                "secret",
                None,
                "next_request",
                env=_provider.api_key_env,
                sensitive=True,
                description=f"{_provider.label} 凭据（仅显示是否已配置）",
            ),
        )

SETTINGS_CATALOG: Mapping[str, SettingDefinition] = MappingProxyType(_CATALOG)
# Descriptive aliases used by adapters and tests.
SETTINGS_CATALOG_BY_KEY = SETTINGS_CATALOG
SETTING_CATALOG = SETTINGS_CATALOG


def settings_catalog() -> Mapping[str, SettingDefinition]:
    return SETTINGS_CATALOG


def _provider_model_error(provider: Any, model: Any) -> str | None:
    """Return a stable validation code for an incompatible model selection."""

    provider_id = str(provider or "auto").strip().lower()
    model_id = str(model or "auto").strip()
    if model_id in {"", "auto"}:
        return None
    profile = model_profile(model_id)
    if profile is None:
        return "settings_model_unsupported"
    if provider_id != "auto" and profile.provider != provider_id:
        return "settings_model_provider_mismatch"
    return None


def _clock_ms(clock: Callable[[], int] | None) -> int:
    return int(clock() if clock is not None else time.time() * 1000)


def parse_dotenv(path: str | Path) -> dict[str, str]:
    """Parse a compatibility .env without modifying ``os.environ``.

    This intentionally implements the small dotenv subset used by the
    application.  Unsupported/malformed lines are ignored, while values are
    bounded so a giant dotenv line cannot become a settings response.
    """

    result: dict[str, str] = {}
    file_path = Path(path)
    try:
        raw_lines = file_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return result
    for raw in raw_lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if len(value) > 4096:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        result[key] = value
    return result


def _default_dotenv_path(environ: Mapping[str, str]) -> Path:
    configured = str(environ.get("STATA_AGENT_ENV", "") or "").strip()
    if configured:
        return Path(configured)
    # app/src/stata_agent/application/settings.py -> app/.env
    return Path(__file__).resolve().parents[3] / ".env"


def _is_safe_path_syntax(raw: str) -> bool:
    if not raw or len(raw) > _SAFE_PATH_MAX:
        return False
    if any(ord(char) < 32 for char in raw) or "\x00" in raw:
        return False
    # UNC and alternate data streams are not accepted as V1 settings roots.
    if raw.startswith(("\\\\", "//")):
        return False
    if ":" in raw[2:]:
        return False
    parts = re.split(r"[\\/]", raw)
    return ".." not in parts


def _normalise_path(value: Any, *, definition: SettingDefinition, check_exists: bool) -> str | None:
    if value is None:
        return None
    if isinstance(value, Path):
        raw = str(value)
    elif isinstance(value, str):
        raw = value.strip()
    else:
        raise SettingsValidationError(key=definition.key)
    if not raw:
        return None
    if not _is_safe_path_syntax(raw):
        raise SettingsValidationError(key=definition.key)
    path = Path(raw).expanduser()
    if check_exists and definition.must_exist:
        try:
            if path.is_symlink() or not path.is_dir() or not os.access(path, os.R_OK | os.X_OK):
                raise SettingsValidationError(key=definition.key)
        except OSError as exc:
            raise SettingsValidationError(key=definition.key) from exc
    # Store portable JSON text rather than a platform-specific Path object.
    return str(path)


def _normalise_url(value: Any, *, definition: SettingDefinition) -> str:
    if not isinstance(value, str):
        raise SettingsValidationError(key=definition.key)
    raw = value.strip()
    if not raw and definition.optional_url:
        return ""
    if not raw or len(raw) > _SAFE_URL_MAX or any(ord(char) < 32 for char in raw):
        raise SettingsValidationError(key=definition.key)
    try:
        parsed = urlsplit(raw)
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            raise SettingsValidationError(key=definition.key)
        if parsed.username is not None or parsed.password is not None or parsed.fragment:
            raise SettingsValidationError(key=definition.key)
        # Accessing port rejects malformed/out-of-range explicit ports.
        _ = parsed.port
    except (ValueError, SettingsValidationError) as exc:
        if isinstance(exc, SettingsValidationError):
            raise
        raise SettingsValidationError(key=definition.key) from exc
    return urlunsplit(("https", parsed.netloc, parsed.path.rstrip("/"), parsed.query, ""))


def _normalise_value(
    definition: SettingDefinition,
    value: Any,
    *,
    from_text: bool = False,
    check_exists: bool = False,
) -> Any:
    kind = definition.value_type
    if definition.sensitive:
        # Callers may use this only to inspect configured status; the actual
        # secret is deliberately never accepted by the settings service.
        raise SettingsValidationError("secret values are managed by SecretStore", key=definition.key)
    if kind == "bool":
        if isinstance(value, bool):
            return value
        if from_text and isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in _BOOL_TRUE:
                return True
            if normalized in _BOOL_FALSE:
                return False
        raise SettingsValidationError(key=definition.key)
    if kind == "int":
        if isinstance(value, bool):
            raise SettingsValidationError(key=definition.key)
        if from_text and isinstance(value, str):
            text = value.strip()
            if not re.fullmatch(r"[+-]?\d+", text):
                raise SettingsValidationError(key=definition.key)
            value = int(text, 10)
        if not isinstance(value, int):
            raise SettingsValidationError(key=definition.key)
        if definition.minimum is not None and value < definition.minimum:
            raise SettingsValidationError(key=definition.key)
        if definition.maximum is not None and value > definition.maximum:
            raise SettingsValidationError(key=definition.key)
        return value
    if kind in {"enum", "str"}:
        if not isinstance(value, str):
            raise SettingsValidationError(key=definition.key)
        normalized = value.strip()
        if kind == "enum":
            lowered = normalized.lower()
            # Language identifiers are case-sensitive by contract; other
            # enum values are deliberately normalized for env compatibility.
            normalized = normalized if definition.key == "ui.language" else lowered
            if normalized not in definition.enum:
                raise SettingsValidationError(key=definition.key)
        if not normalized or len(normalized) > 4096:
            raise SettingsValidationError(key=definition.key)
        return normalized
    if kind == "path" or definition.path:
        return _normalise_path(value, definition=definition, check_exists=check_exists)
    if kind == "url" or definition.url:
        return _normalise_url(value, definition=definition)
    raise SettingsValidationError(key=definition.key)


@dataclass(frozen=True, slots=True)
class EffectiveSetting:
    key: str
    value: Any
    source: SettingSource
    editable: bool
    sensitive: bool
    apply_mode: ApplyMode
    pending_restart: bool = False
    status: SettingStatus = "ready"
    message: str = ""

    def __post_init__(self) -> None:
        if self.source not in _ALLOWED_SOURCES:
            raise ValueError("invalid settings source")
        if self.apply_mode not in _ALLOWED_APPLY_MODES:
            raise ValueError("invalid settings apply mode")
        if self.status not in {"ready", "error"}:
            raise ValueError("invalid settings status")
        if self.sensitive:
            object.__setattr__(self, "value", None)

    @property
    def configured(self) -> bool:
        return self.source != "default" or self.value is not None

    def as_dict(self) -> dict[str, Any]:
        value = None if self.sensitive else self.value
        return {
            "key": self.key,
            "value": value,
            "source": self.source,
            "editable": self.editable,
            "sensitive": self.sensitive,
            "apply_mode": self.apply_mode,
            "pending_restart": self.pending_restart,
            "status": self.status,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class SettingsHealthResult:
    component: str = "settings"
    status: str = "ready"
    code: str = "ok"
    message: str = ""
    checked_at: int = 0
    metrics: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))

    @property
    def ok(self) -> bool:
        return self.status == "ready"

    def as_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "status": self.status,
            "code": self.code,
            "message": self.message,
            "checked_at": self.checked_at,
            "metrics": dict(self.metrics),
        }


@dataclass(frozen=True, slots=True)
class EffectiveSettings:
    revision: int
    values: Mapping[str, EffectiveSetting]
    restart_required: bool = False
    pending_keys: tuple[str, ...] = ()
    health: tuple[SettingsHealthResult, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))
        object.__setattr__(self, "pending_keys", tuple(self.pending_keys))
        object.__setattr__(self, "health", tuple(self.health))

    def __getitem__(self, key: str) -> EffectiveSetting:
        return self.values[key]

    def get(self, key: str, default: EffectiveSetting | None = None) -> EffectiveSetting | None:
        return self.values.get(key, default)

    def as_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "values": {key: setting.as_dict() for key, setting in self.values.items()},
            "restart_required": self.restart_required,
            "pending_keys": list(self.pending_keys),
            "health": [item.as_dict() for item in self.health],
        }


@dataclass(frozen=True, slots=True)
class SettingsDocumentContract:
    """Application re-export-friendly alias for the repository document."""

    schema: str
    revision: int
    updated_at: int
    values: Mapping[str, Any]


# The design names this type SettingsDocument.  Re-export the concrete
# validated repository type rather than creating a second incompatible type.
SettingsDocumentType = SettingsDocument


@dataclass(frozen=True, slots=True)
class SettingsPatch:
    expected_revision: int
    changes: Mapping[str, Any]

    def __post_init__(self) -> None:
        if isinstance(self.expected_revision, bool) or not isinstance(self.expected_revision, int):
            raise SettingsValidationError("settings expected revision is invalid")
        if self.expected_revision < 0 or not isinstance(self.changes, Mapping):
            raise SettingsValidationError("settings patch is invalid")
        object.__setattr__(self, "changes", MappingProxyType(dict(self.changes)))

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "SettingsPatch":
        if not isinstance(raw, Mapping):
            raise SettingsValidationError("settings patch is invalid")
        expected = raw.get("expected_revision")
        changes = raw.get("changes", {})
        if isinstance(expected, bool) or not isinstance(expected, int):
            raise SettingsValidationError("settings expected revision is invalid")
        if not isinstance(changes, Mapping):
            raise SettingsValidationError("settings patch changes are invalid")
        return cls(expected_revision=expected, changes=changes)

    def as_dict(self) -> dict[str, Any]:
        return {"expected_revision": self.expected_revision, "changes": dict(self.changes)}


@dataclass(frozen=True, slots=True)
class SettingsApplyResult:
    revision: int
    changed_keys: tuple[str, ...]
    restart_required: bool
    effective: EffectiveSettings

    def __post_init__(self) -> None:
        object.__setattr__(self, "changed_keys", tuple(self.changed_keys))

    def as_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "changed_keys": list(self.changed_keys),
            "restart_required": self.restart_required,
            "effective": self.effective.as_dict(),
        }


def _health_from_repository(health: RepositoryHealth, *, clock: Callable[[], int] | None) -> SettingsHealthResult:
    return SettingsHealthResult(
        component="settings_repository",
        status=health.status,
        code=health.code,
        message=health.message,
        checked_at=health.checked_at or _clock_ms(clock),
    )


class SettingsService:
    """Resolve and atomically apply non-secret application settings."""

    def __init__(
        self,
        repository: LocalSettingsRepository | None = None,
        *,
        path: str | Path | None = None,
        environ: Mapping[str, str] | None = None,
        dotenv: Mapping[str, str] | None = None,
        dotenv_path: str | Path | None = None,
        secret_store: Any | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.repository = repository or LocalSettingsRepository(path=path, clock=clock)
        if environ is None:
            # ``load_env`` keeps legacy callers working by injecting dotenv
            # values into ``os.environ``.  Resolve settings through the
            # source-aware helper so those values do not masquerade as an
            # explicit process override.
            try:
                from ..config import explicit_environment

                self.environ = explicit_environment()
            except Exception:  # noqa: BLE001 - compatibility fallback
                self.environ = dict(os.environ)
        else:
            self.environ = dict(environ)
        self._clock = clock
        if dotenv is None:
            if dotenv_path is None:
                try:
                    from ..config import dotenv_values as loaded_dotenv

                    self.dotenv = loaded_dotenv()
                except Exception:  # noqa: BLE001 - compatibility fallback
                    self.dotenv = parse_dotenv(_default_dotenv_path(self.environ))
            else:
                self.dotenv = parse_dotenv(Path(dotenv_path))
        else:
            self.dotenv = {str(key): str(value) for key, value in dotenv.items()}
        # SecretStore is an optional composition dependency.  Keeping it
        # optional preserves legacy environment-only startup and ensures a
        # non-Windows host never gets a plaintext fallback by accident.
        self.secret_store = secret_store

    @property
    def catalog(self) -> Mapping[str, SettingDefinition]:
        return SETTINGS_CATALOG

    def _raw_environment(self, definition: SettingDefinition) -> tuple[bool, Any]:
        for env_name in definition.env_names:
            if env_name in self.environ:
                return True, self.environ[env_name]
        return False, None

    def _raw_dotenv(self, definition: SettingDefinition) -> tuple[bool, Any]:
        for env_name in definition.env_names:
            if env_name in self.dotenv:
                return True, self.dotenv[env_name]
        return False, None

    def _resolve_one(
        self,
        definition: SettingDefinition,
        user_values: Mapping[str, Any],
    ) -> EffectiveSetting:
        # Secrets are only represented as a masked configured status.  Task 2
        # may layer Credential Manager between dotenv and environment without
        # changing this public contract.
        if definition.sensitive:
            env_present, env_value = self._raw_environment(definition)
            if env_present and str(env_value or "").strip():
                return EffectiveSetting(
                    definition.key,
                    None,
                    "environment",
                    False,
                    True,
                    definition.apply_mode,
                    message=definition.description,
                )
            if self.secret_store is not None:
                try:
                    from ..settings.secret_store import resolve_secret_status

                    provider = definition.key.removeprefix("provider.").removesuffix(".api_key")
                    credential = resolve_secret_status(
                        provider,
                        self.secret_store,
                        dotenv=self.dotenv,
                        environ=self.environ,
                    )
                    if credential.source == "credential":
                        return EffectiveSetting(
                            definition.key,
                            None,
                            "credential",
                            True,
                            True,
                            definition.apply_mode,
                            message=definition.description,
                        )
                except Exception:
                    # An unavailable credential manager is a health issue, not
                    # a reason to invent a file-backed secret.  Environment and
                    # dotenv paths above remain compatible and fail closed.
                    pass
            dotenv_present, dotenv_value = self._raw_dotenv(definition)
            if dotenv_present and str(dotenv_value or "").strip():
                return EffectiveSetting(
                    definition.key,
                    None,
                    "dotenv",
                    True,
                    True,
                    definition.apply_mode,
                    message=definition.description,
                )
            return EffectiveSetting(
                definition.key,
                None,
                "default",
                True,
                True,
                definition.apply_mode,
                message=definition.description,
            )

        value = definition.default
        source: SettingSource = "default"
        status: SettingStatus = "ready"
        message = definition.description

        dotenv_present, dotenv_value = self._raw_dotenv(definition)
        if dotenv_present:
            try:
                value = _normalise_value(definition, dotenv_value, from_text=True, check_exists=True)
                source = "dotenv"
            except SettingsValidationError:
                status = "error"
                message = "dotenv setting is invalid; safe default is active"
                value = definition.default
                source = "dotenv"

        if definition.key in user_values:
            try:
                value = _normalise_value(definition, user_values[definition.key], check_exists=True)
                source = "user"
                status = "ready"
                message = definition.description
            except SettingsValidationError:
                status = "error"
                message = "saved setting is invalid; safe default is active"
                value = definition.default
                source = "user"

        env_present, env_value = self._raw_environment(definition)
        if env_present:
            try:
                value = _normalise_value(definition, env_value, from_text=True, check_exists=True)
                status = "ready"
                message = definition.description
            except SettingsValidationError:
                value = definition.default
                status = "error"
                message = "environment setting is invalid; safe default is active"
            source = "environment"

        editable = definition.editable and source != "environment" and not definition.sensitive
        pending_restart = definition.apply_mode == "restart" and source == "user"
        return EffectiveSetting(
            key=definition.key,
            value=value,
            source=source,
            editable=editable,
            sensitive=definition.sensitive,
            apply_mode=definition.apply_mode,
            pending_restart=pending_restart,
            status=status,
            message=message,
        )

    def _resolve_document(self, document: SettingsDocument) -> EffectiveSettings:
        effective_values = {
            key: self._resolve_one(definition, document.values or {})
            for key, definition in SETTINGS_CATALOG.items()
        }
        errors = [
            SettingsHealthResult(
                component="settings_value",
                status="error",
                code="settings_invalid_value",
                message=setting.message,
                checked_at=_clock_ms(self._clock),
                metrics={"key": key},
            )
            for key, setting in effective_values.items()
            if setting.status == "error"
        ]
        provider_model_error = _provider_model_error(
            effective_values["provider.primary"].value,
            effective_values["provider.model"].value,
        )
        if provider_model_error is not None:
            errors.append(
                SettingsHealthResult(
                    component="settings_value",
                    status="error",
                    code=provider_model_error,
                    message="当前 provider 与模型能力画像不匹配。",
                    checked_at=_clock_ms(self._clock),
                    metrics={"key": "provider.model"},
                )
            )
        repository_health = _health_from_repository(self.repository.health, clock=self._clock)
        health: list[SettingsHealthResult] = []
        if not repository_health.ok:
            health.append(repository_health)
        health.extend(errors)
        pending = tuple(key for key, setting in effective_values.items() if setting.pending_restart)
        return EffectiveSettings(
            revision=document.revision,
            values=effective_values,
            restart_required=bool(pending),
            pending_keys=pending,
            health=tuple(health),
        )

    def resolve(self) -> EffectiveSettings:
        return self._resolve_document(self.repository.load())

    effective = resolve
    get_effective = resolve

    def _coerce_patch(self, patch: SettingsPatch | Mapping[str, Any] | None, *, expected_revision: int | None,
                      changes: Mapping[str, Any] | None) -> SettingsPatch:
        if patch is not None:
            if expected_revision is not None or changes is not None:
                raise TypeError("pass a SettingsPatch or expected_revision/changes, not both")
            return patch if isinstance(patch, SettingsPatch) else SettingsPatch.from_mapping(patch)
        if expected_revision is None or changes is None:
            raise SettingsValidationError("settings patch requires expected_revision and changes")
        return SettingsPatch(expected_revision=expected_revision, changes=changes)

    def validate_patch(
        self,
        patch: SettingsPatch | Mapping[str, Any] | None = None,
        *,
        expected_revision: int | None = None,
        changes: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Validate every change and return normalized user-owned values.

        No filesystem write occurs here.  Every field is checked before the
        service invokes the repository's single compare-and-write operation.
        """

        request = self._coerce_patch(patch, expected_revision=expected_revision, changes=changes)
        document = self.repository.load()
        if not self.repository.health.ok:
            raise SettingsRepositoryError("settings repository is unhealthy")
        if request.expected_revision != document.revision:
            raise SettingsRevisionConflict(request.expected_revision, document.revision)
        candidate = dict(document.values or {})
        for key, raw_value in request.changes.items():
            if not isinstance(key, str) or key not in SETTINGS_CATALOG:
                raise SettingsValidationError("unknown settings key", key=key if isinstance(key, str) else None)
            definition = SETTINGS_CATALOG[key]
            if definition.sensitive:
                raise SettingsValidationError("secret values are managed by SecretStore", key=key)
            env_present, _ = self._raw_environment(definition)
            if env_present:
                raise SettingsEnvironmentManagedError("setting is managed by the process environment", key=key)
            if not definition.editable:
                raise SettingsReadOnlyError("setting is read-only", key=key)
            candidate[key] = _normalise_value(definition, raw_value, check_exists=True)

        # Resolve all effective values with the candidate user document.  This
        # catches cross-field context budget violations before any write.
        resolved = {
            key: self._resolve_one(definition, candidate)
            for key, definition in SETTINGS_CATALOG.items()
        }
        errors = [key for key, setting in resolved.items() if setting.status == "error"]
        if errors:
            raise SettingsValidationError("effective settings contain an invalid value", key=errors[0])
        max_input = resolved["context.max_input_tokens"].value
        reserve = resolved["context.reserve_output_tokens"].value
        if not isinstance(max_input, int) or not isinstance(reserve, int) or reserve > max_input:
            raise SettingsValidationError("reserve_output_tokens cannot exceed max_input_tokens",
                                          key="context.reserve_output_tokens")
        provider_model_error = _provider_model_error(
            resolved["provider.primary"].value,
            resolved["provider.model"].value,
        )
        if provider_model_error is not None:
            raise SettingsValidationError("provider and model profile are incompatible", key="provider.model")
        return {key: candidate[key] for key in request.changes if key in candidate}

    def apply_patch(
        self,
        patch: SettingsPatch | Mapping[str, Any] | None = None,
        *,
        expected_revision: int | None = None,
        changes: Mapping[str, Any] | None = None,
    ) -> SettingsApplyResult:
        request = self._coerce_patch(patch, expected_revision=expected_revision, changes=changes)
        normalized = self.validate_patch(request)
        document = self.repository.load()
        old_values = dict(document.values or {})
        merged = dict(old_values)
        merged.update(normalized)
        changed_keys = tuple(key for key in request.changes if old_values.get(key) != merged.get(key))
        if not changed_keys:
            effective = self._resolve_document(document)
            return SettingsApplyResult(document.revision, (), effective.restart_required, effective)
        try:
            saved = self.repository.compare_and_write(request.expected_revision, merged, updated_at=_clock_ms(self._clock))
        except SettingsRevisionConflict:
            raise
        effective = self._resolve_document(saved)
        return SettingsApplyResult(saved.revision, changed_keys, effective.restart_required, effective)

    apply = apply_patch
    save = apply_patch

    def is_environment_managed(self, key: str) -> bool:
        definition = SETTINGS_CATALOG.get(key)
        if definition is None:
            return False
        present, _ = self._raw_environment(definition)
        return present

    def value(self, key: str, default: Any = None) -> Any:
        """Return one effective raw value for composition adapters.

        Sensitive settings intentionally return ``None``; provider factories
        must obtain their value through ``SecretStore`` directly.
        """

        setting = self.resolve().values.get(key)
        if setting is None or setting.sensitive:
            return default
        return setting.value

    def secret_status(self, provider: str):
        """Return a masked credential status through the injected store."""

        if self.secret_store is None:
            from ..settings.secret_store import SecretStatus

            normalized = str(provider).strip().lower()
            from ..settings.secret_store import PROVIDER_ENV_VARS

            env_name = PROVIDER_ENV_VARS.get(normalized)
            if env_name is None:
                raise SettingsValidationError("unsupported provider")
            if str(self.environ.get(env_name) or "").strip():
                return SecretStatus(normalized, True, "environment", False)
            if str(self.dotenv.get(env_name) or "").strip():
                return SecretStatus(normalized, True, "dotenv", True)
            return SecretStatus(normalized, False, "none", True)
        from ..settings.secret_store import resolve_secret_status

        return resolve_secret_status(
            provider,
            self.secret_store,
            dotenv=self.dotenv,
            environ=self.environ,
        )

    def set_secret(self, provider: str, secret: str):
        if self.secret_store is None:
            from ..settings.secret_store import SecretStoreUnavailable

            raise SecretStoreUnavailable()
        return self.secret_store.set(provider, secret)

    def replace_secret(self, provider: str, secret: str):
        if self.secret_store is None:
            from ..settings.secret_store import SecretStoreUnavailable

            raise SecretStoreUnavailable()
        return self.secret_store.replace(provider, secret)

    def delete_secret(self, provider: str):
        if self.secret_store is None:
            from ..settings.secret_store import SecretStoreUnavailable

            raise SecretStoreUnavailable()
        return self.secret_store.delete(provider)


def validate_setting(key: str, value: Any, *, from_text: bool = False, check_exists: bool = False) -> Any:
    """Validate one catalog value for adapters and focused tests."""

    definition = SETTINGS_CATALOG.get(key)
    if definition is None:
        raise SettingsValidationError("unknown settings key", key=key)
    return _normalise_value(definition, value, from_text=from_text, check_exists=check_exists)


__all__ = [
    "ApplyMode",
    "EffectiveSetting",
    "EffectiveSettings",
    "SETTING_CATALOG",
    "SETTINGS_CATALOG",
    "SETTINGS_CATALOG_BY_KEY",
    "SettingDefinition",
    "SettingSource",
    "SettingsApplyResult",
    "SettingsDocument",
    "SettingsDocumentContract",
    "SettingsEnvironmentManagedError",
    "SettingsError",
    "SettingsHealthResult",
    "SettingsPatch",
    "SettingsPrivacyAcknowledgementRequired",
    "SettingsReadOnlyError",
    "SettingsRepositoryError",
    "SettingsRevisionConflict",
    "SettingsService",
    "SettingsValidationError",
    "parse_dotenv",
    "settings_catalog",
    "validate_setting",
    "SETTINGS_SCHEMA",
]
