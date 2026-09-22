"""Identity creation port; concrete UUID/ULID policy belongs to an adapter."""

from typing import Protocol, TypeVar

from stata_research_agent.domain.identifiers import OpaqueId

IdentifierT = TypeVar("IdentifierT", bound=OpaqueId)


class IdentityGenerator(Protocol):
    def new(self, identity_type: type[IdentifierT]) -> IdentifierT: ...
