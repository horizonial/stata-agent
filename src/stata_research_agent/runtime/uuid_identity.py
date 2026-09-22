"""UUID-backed authoritative identity adapter."""

from uuid import uuid4

from stata_research_agent.domain.identifiers import OpaqueId


class UuidIdentityGenerator:
    def new[IdentifierT: OpaqueId](self, identity_type: type[IdentifierT]) -> IdentifierT:
        return identity_type(f"{identity_type.prefix}{uuid4()}")
