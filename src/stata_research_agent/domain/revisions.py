"""Revision and ordering primitives with explicit numeric domains."""

from __future__ import annotations

from dataclasses import dataclass

from .errors import DomainValidationError


@dataclass(frozen=True, slots=True, order=True)
class WorkspaceRevision:
    """Per-workspace authoritative commit sequence; zero means no commits yet."""

    value: int

    def __post_init__(self) -> None:
        if isinstance(self.value, bool) or not isinstance(self.value, int) or self.value < 0:
            raise DomainValidationError("workspace revision must be an integer >= 0")

    def next(self) -> WorkspaceRevision:
        return WorkspaceRevision(self.value + 1)

    def to_primitive(self) -> int:
        return self.value


@dataclass(frozen=True, slots=True, order=True)
class ControlRevision:
    """Non-negative CAS revision for mutable control state such as the write lane."""

    value: int

    def __post_init__(self) -> None:
        if isinstance(self.value, bool) or not isinstance(self.value, int) or self.value < 0:
            raise DomainValidationError("control revision must be an integer >= 0")

    def next(self) -> ControlRevision:
        return ControlRevision(self.value + 1)

    def to_primitive(self) -> int:
        return self.value


@dataclass(frozen=True, slots=True, order=True)
class EntityRevision:
    """Revision of a mutable control record or immutable aggregate revision."""

    value: int

    def __post_init__(self) -> None:
        if isinstance(self.value, bool) or not isinstance(self.value, int) or self.value < 1:
            raise DomainValidationError("entity revision must be an integer >= 1")

    def next(self) -> EntityRevision:
        return EntityRevision(self.value + 1)

    def to_primitive(self) -> int:
        return self.value


@dataclass(frozen=True, slots=True, order=True)
class Ordinal:
    """One-based stable ordering within an owning object."""

    value: int

    def __post_init__(self) -> None:
        if isinstance(self.value, bool) or not isinstance(self.value, int) or self.value < 1:
            raise DomainValidationError("ordinal must be an integer >= 1")

    def to_primitive(self) -> int:
        return self.value
