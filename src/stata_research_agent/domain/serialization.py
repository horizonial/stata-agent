"""Small deterministic primitive encoder for domain boundary tests.

This is not the public API schema layer.  M0-06 will define versioned wire DTOs explicitly.
"""

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any

from .identifiers import OpaqueId
from .revisions import ControlRevision, EntityRevision, Ordinal, WorkspaceRevision


def to_primitive(value: Any) -> Any:
    """Convert a supported domain value into JSON-compatible primitives."""

    if isinstance(value, (OpaqueId, WorkspaceRevision, ControlRevision, EntityRevision, Ordinal)):
        return value.to_primitive()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: to_primitive(getattr(value, field.name)) for field in fields(value)}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, tuple):
        return [to_primitive(item) for item in value]
    if isinstance(value, list):
        return [to_primitive(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): to_primitive(item) for key, item in value.items()}
    raise TypeError(f"unsupported domain primitive: {type(value).__name__}")
