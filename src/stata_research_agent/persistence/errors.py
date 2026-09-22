"""Typed persistence bootstrap failures."""


class PersistenceBootstrapError(RuntimeError):
    """Base class for errors that must fail Workspace opening closed."""


class ConnectionContractError(PersistenceBootstrapError):
    """A SQLite connection does not satisfy the required correctness pragmas."""


class SchemaCompatibilityError(PersistenceBootstrapError):
    """The database identity or schema version is unsupported."""


class MigrationChecksumError(PersistenceBootstrapError):
    """An applied migration no longer matches the reviewed registry."""


class MigrationLeaseHeldError(PersistenceBootstrapError):
    """Another owner holds the durable migration lease."""


class WorkspaceIdentityError(PersistenceBootstrapError):
    """The database belongs to a different Workspace identity."""


class CommandConflictError(PersistenceBootstrapError):
    """A command ID was reused with different canonical request content."""
