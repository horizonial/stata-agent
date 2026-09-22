"""Domain validation failures that do not depend on an interface framework."""


class DomainValidationError(ValueError):
    """Raised when a value cannot represent a valid domain primitive."""
