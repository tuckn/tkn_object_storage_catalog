class AppError(Exception):
    """An actionable input, consistency, or service error."""


class ConflictError(AppError):
    """Existing data changed or would be replaced without authorization."""
