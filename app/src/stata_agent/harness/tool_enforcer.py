"""Single pre-execution enforcement point for agent tools.

The model is allowed to suggest a tool call, but it never gets to decide
whether that call is executable.  This module owns the checks shared by every
entry point: argument shape, permission/phase/privacy, path containment and
network destination validation, followed by a bounded handler invocation.
"""

from __future__ import annotations

import ipaddress
import json
import math
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TYPE_CHECKING
from urllib.parse import urlsplit

from ..phase.phasedef import Phase
from ..privacy.modes import (
    LOCAL_STRICT,
    normalize_mode,
)

if TYPE_CHECKING:  # pragma: no cover - imports are only for type checkers
    from ..toolkit import Tool, ToolContext


_PERMISSIONS = frozenset(
    {"safe", "read", "write", "execute", "network", "external", "destructive"}
)
_EXECUTE_PHASES = frozenset({Phase.DATA.value, Phase.ESTIMATION.value, Phase.ROBUSTNESS.value})
_WRITE_PHASES = frozenset(
    {Phase.DATA.value, Phase.ESTIMATION.value, Phase.ROBUSTNESS.value, Phase.WRITING.value}
)
_URL_SCHEMES = frozenset({"http", "https"})
_PATH_KEYS = frozenset({"path", "data", "output", "directory", "file"})
_ABS_PATH_IN_TEXT = re.compile(
    r"(?<![A-Za-z0-9_])(?:[A-Za-z]:[\\/]|\\\\|/(?:Users|home|tmp|var|mnt|data|private)/)[^\s\"'<>`]+"
)


@dataclass(frozen=True)
class EnforcementError:
    type: str
    message: str
    suggestion: str = ""

    def as_result(self) -> dict:
        error = {"type": self.type, "message": self.message, "retryable": False}
        if self.suggestion:
            error["suggestion"] = self.suggestion
        return {"ok": False, "error": error}


def _type_matches(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def _schema_error(value: Any, schema: dict, location: str = "arguments") -> str | None:
    """Validate the JSON-schema subset emitted by :class:`Tool`.

    Tool schemas are intentionally small, so a dependency-free validator is
    preferable to silently accepting malformed calls or adding a new runtime
    dependency to the package.
    """

    if not isinstance(schema, dict):
        return f"{location}: 工具 schema 无效"
    expected = schema.get("type")
    if expected and not _type_matches(value, expected):
        return f"{location}: 需要 type={expected}"
    if "enum" in schema and value not in schema["enum"]:
        return f"{location}: 值不在允许集合中"
    if isinstance(value, str):
        if "minLength" in schema and len(value) < int(schema["minLength"]):
            return f"{location}: 字符串过短"
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            return f"{location}: 字符串过长"
        if "pattern" in schema:
            try:
                if re.search(str(schema["pattern"]), value) is None:
                    return f"{location}: 字符串格式不匹配"
            except re.error:
                return f"{location}: schema pattern 无效"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            return f"{location}: 数值过小"
        if "maximum" in schema and value > schema["maximum"]:
            return f"{location}: 数值过大"
    if isinstance(value, dict):
        required = schema.get("required") or []
        for key in required:
            if key not in value:
                return f"{location}: 缺少 required 参数 {key!r}"
        properties = schema.get("properties") or {}
        if schema.get("additionalProperties", True) is False:
            unknown = sorted(set(value) - set(properties))
            if unknown:
                return f"{location}: 不允许的参数 {unknown!r}"
        for key, child in properties.items():
            if key in value:
                error = _schema_error(value[key], child, f"{location}.{key}")
                if error:
                    return error
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            error = _schema_error(item, schema["items"], f"{location}[{index}]")
            if error:
                return error
    return None


def normalize_arguments(arguments: Any) -> tuple[dict | None, EnforcementError | None]:
    """Decode a provider's arguments value and require a JSON object."""

    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError as exc:
            return None, EnforcementError("invalid_arguments", f"arguments 不是合法 JSON：{exc.msg}")
    if not isinstance(arguments, dict):
        return None, EnforcementError("invalid_arguments", "arguments 必须是 JSON object")
    return arguments, None


def _phase_value(ctx: ToolContext | None) -> str | None:
    if ctx is None:
        return None
    phase = getattr(ctx, "phase", None)
    if phase is None:
        store = getattr(ctx, "store", None)
        idea = getattr(ctx, "idea", None)
        try:
            phase = store.project(idea).phase if store is not None and idea else None
        except Exception:  # noqa: BLE001 - an unavailable projection is fail-closed
            phase = None
    if isinstance(phase, Phase):
        return phase.value
    if phase is None:
        return None
    return str(phase).strip().upper()


def _roots(ctx: ToolContext | None) -> tuple[Path, ...]:
    if ctx is None:
        return ()
    explicit = list(getattr(ctx, "allowed_roots", ()) or ())
    explicit.extend((getattr(ctx, "run_root", None), getattr(ctx, "data_dir", None)))
    # An explicit workspace root is authoritative.  Falling back to the
    # ledger directory is useful for legacy callers, but must not silently
    # widen a caller-supplied run_root/data_dir.
    candidates: list[Any] = [item for item in explicit if item]
    if not candidates:
        store = getattr(ctx, "store", None)
        db_path = getattr(store, "_path", None)
        if db_path:
            candidates.append(Path(db_path).parent)
    result: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate:
            continue
        try:
            root = Path(candidate).expanduser().resolve(strict=False)
        except (OSError, TypeError, ValueError):
            continue
        key = str(root).casefold()
        if key not in seen:
            result.append(root)
            seen.add(key)
    return tuple(result)


def validate_path(path: Any, ctx: ToolContext | None, *, kind: str = "路径") -> str | None:
    """Require an absolute path contained in a configured workspace root."""

    if not isinstance(path, str) or not path.strip():
        return f"{kind}不能为空"
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        return f"{kind}必须是绝对路径"
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        return f"{kind}无法解析：{exc}"
    roots = _roots(ctx)
    if not roots:
        return f"未配置允许的工作区根目录，拒绝访问{kind}"
    if not any(resolved == root or root in resolved.parents for root in roots):
        return f"{kind}不在允许的工作区根目录内：{path}"
    return None


def _host_is_private(host: str) -> bool:
    host = host.strip().lower().rstrip(".")
    if not host or host in {"localhost", "localhost.localdomain", "ip6-localhost"}:
        return True
    if host.endswith(".localhost") or host.endswith(".local") or host.endswith(".internal"):
        return True
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        # A bare host is commonly an intranet alias and cannot be safely
        # distinguished from a public DNS name without allowing DNS rebinding.
        if "." not in host:
            return True
        return False
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
    )


def validate_url(url: Any) -> str | None:
    """Allow only public HTTP(S) URLs; reject local/private destinations."""

    if not isinstance(url, str) or not url.strip():
        return "url 不能为空"
    try:
        parsed = urlsplit(url.strip())
        host = parsed.hostname
    except ValueError as exc:
        return f"url 无效：{exc}"
    if parsed.scheme.lower() not in _URL_SCHEMES:
        return f"禁止的 URL scheme：{parsed.scheme or '(空)'}（仅 http/https）"
    if not host:
        return "url 缺少 host"
    if parsed.username or parsed.password:
        return "URL 不得携带用户名/密码"
    if _host_is_private(host):
        return f"拒绝访问 loopback/private network host：{host}"
    return None


# 相对路径穿越：`../`/`..\` 段（前面是引号/空白/等号/逗号，避免误伤 `a..b` 类标识符）
_REL_TRAVERSAL = re.compile(r"(?<![\w.\-])(?:\.\.[\\/]+)+[^\s\"'<>`]*")


def _strip_stata_comments(code: str) -> str:
    """Remove Stata comments (block /* */, line //, whole-line *) before scanning."""
    cleaned = re.sub(r"/\*.*?\*/", " ", code, flags=re.S)
    lines = []
    for raw in cleaned.splitlines():
        line = raw.split("//", 1)[0]
        stripped = line.strip()
        if stripped.startswith("*"):
            continue
        lines.append(line)
    return "\n".join(lines)


def _validate_code_paths(code: str, ctx: ToolContext | None) -> str | None:
    """Catch absolute and relative (`../`) paths embedded in executable Stata code.

    Study code has no legitimate reason to reach outside its workspace root, so
    any `..` traversal in a path-like token is a fail-closed deny.  Comments are
    stripped first so a benign note does not trip the guard.
    """
    for match in _ABS_PATH_IN_TEXT.finditer(code):
        error = validate_path(match.group(0).rstrip(",);"), ctx, kind="代码中的路径")
        if error:
            return error
    cleaned = _strip_stata_comments(code)
    rel = _REL_TRAVERSAL.search(cleaned)
    if rel:
        return f"代码中的相对路径穿越被拒：{rel.group(0)[:40]}"
    return None


def _requires_store_thread(tool: Tool, ctx: ToolContext) -> bool:
    """SQLiteStore connections are thread-affine on supported Python builds.

    Keep built-in ledger-mutating handlers on the caller thread.  Arbitrary
    custom tools still run in a daemon worker and therefore receive the hard
    timeout below; moving a SQLite connection to another thread would turn a
    valid call into a misleading ``sqlite3.ProgrammingError``.
    """

    if getattr(ctx, "store", None) is None:
        return False
    if getattr(tool.handler, "__module__", "") != "stata_agent.toolkit":
        return False
    return tool.name in {"run_stata", "run_do_file", "verify_result", "write_draft"}


class ToolEnforcer:
    """The one policy gate used immediately before every tool handler."""

    def __init__(self, tools: dict[str, Tool]):
        self.tools = tools

    def validate(self, name: str, arguments: Any, ctx: ToolContext | None = None) -> str | None:
        if name not in self.tools:
            return f"未知工具 {name!r}（只能调用已注册工具，不许现造）"
        tool = self.tools[name]
        args, arg_error = normalize_arguments(arguments)
        if arg_error:
            return arg_error.message
        schema_error = _schema_error(args, tool.input_schema)
        if schema_error:
            return schema_error
        permission = str(tool.permission or "").strip().lower()
        if permission not in _PERMISSIONS:
            return f"工具 {name!r} 的 permission 未登记：{permission!r}"
        try:
            mode = normalize_mode(getattr(ctx, "privacy_mode", LOCAL_STRICT) if ctx else LOCAL_STRICT)
        except Exception as exc:  # noqa: BLE001 - unknown mode is a hard deny
            return str(exc)
        phase = _phase_value(ctx)
        if permission == "destructive":
            return "destructive 工具没有可用的审批 token，已拒绝"
        if permission in {"network", "external"}:
            if mode == LOCAL_STRICT:
                return "local_strict 禁止联网/外发"
            if not bool(getattr(ctx, "network_available", False)):
                return "当前上下文未启用网络能力"
        if permission == "execute":
            if phase == Phase.DONE.value:
                return "DONE 阶段禁止 execute 工具"
            # ``phase=None`` is retained for legacy callers that have not
            # bootstrapped a phase event yet; once a phase is explicit, the
            # allow-list is strict.
            if phase is not None and phase not in _EXECUTE_PHASES:
                return f"execute 工具仅限 {sorted(_EXECUTE_PHASES)} 阶段"
        if permission == "write":
            if phase == Phase.DONE.value:
                return "DONE 阶段禁止 write 工具"
            if phase is not None and phase not in _WRITE_PHASES:
                return f"write 工具仅限 {sorted(_WRITE_PHASES)} 阶段"
        if args is not None:
            for key, value in args.items():
                if key in _PATH_KEYS and isinstance(value, str):
                    error = validate_path(value, ctx)
                    if error:
                        return error
            tool_name = str(getattr(tool, "name", name))
            if tool_name in {"run_stata", "run_do_file"} and isinstance(args.get("code"), str):
                error = _validate_code_paths(args["code"], ctx)
                if error:
                    return error
            if tool_name == "run_do_file" and isinstance(args.get("path"), str):
                try:
                    do_code = Path(args["path"]).read_text(encoding="utf-8", errors="replace")
                except OSError as exc:
                    return f"do 文件无法读取：{exc}"
                error = _validate_code_paths(do_code, ctx)
                if error:
                    return error
            if tool_name == "fetch_source":
                error = validate_url(args.get("url"))
                if error:
                    return error
        return None

    def execute(self, name: str, arguments: Any, ctx: ToolContext) -> dict:
        error = self.validate(name, arguments, ctx)
        if error:
            return EnforcementError("permission_denied", error).as_result()
        tool = self.tools[name]
        args, arg_error = normalize_arguments(arguments)
        if arg_error or args is None:  # defensive: validate already did this
            return (arg_error or EnforcementError("invalid_arguments", "arguments 无效")).as_result()

        try:
            timeout = float(tool.timeout_seconds)
        except (TypeError, ValueError):
            return EnforcementError("timeout", f"工具 {name} 的 timeout_seconds 无效").as_result()
        if not math.isfinite(timeout) or timeout <= 0:
            return EnforcementError("timeout", f"工具 {name} 的 timeout_seconds 必须大于 0").as_result()

        if _requires_store_thread(tool, ctx):
            # These handlers delegate to SQLiteStore, whose connection is
            # deliberately kept thread-affine.  Their underlying Stata/store
            # operations have their own bounded transports; do not break the
            # ledger by moving them to a worker thread.
            try:
                result = tool.handler(args, ctx)
            except BaseException as exc:  # noqa: BLE001
                return EnforcementError("tool_error", str(exc)[:200]).as_result()
            return result if isinstance(result, dict) else EnforcementError(
                "tool_error", "工具返回值必须是 object"
            ).as_result()

        result_box: list[Any] = []
        error_box: list[BaseException] = []
        done = threading.Event()

        def invoke() -> None:
            try:
                result_box.append(tool.handler(args, ctx))
            except BaseException as exc:  # noqa: BLE001 - converted to tool error below
                error_box.append(exc)
            finally:
                done.set()

        thread = threading.Thread(target=invoke, name=f"stata-agent-tool-{name}", daemon=True)
        thread.start()
        if not done.wait(timeout):
            return EnforcementError(
                "timeout",
                f"工具 {name} 超过 {timeout:g}s 执行时限，已停止等待",
                "缩小输入或拆分操作后重试",
            ).as_result()
        if error_box:
            exc = error_box[0]
            return EnforcementError("tool_error", str(exc)[:200]).as_result()
        result = result_box[0] if result_box else None
        if not isinstance(result, dict):
            return EnforcementError("tool_error", "工具返回值必须是 object").as_result()
        return result


__all__ = [
    "EnforcementError",
    "ToolEnforcer",
    "normalize_arguments",
    "validate_path",
    "validate_url",
]
