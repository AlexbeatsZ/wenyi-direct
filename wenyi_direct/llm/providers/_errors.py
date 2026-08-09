"""Shared provider error classification helpers."""


_TIMEOUT_ERROR_MARKERS = (
    "deadline exceeded",
    "time out",
    "time-out",
    "timed out",
    "timeout",
    "超时",
)


def is_explicit_timeout_error(detail: str) -> bool:
    """Return whether provider output explicitly reports any form of timeout."""
    lowered = detail.casefold()
    return any(marker in lowered for marker in _TIMEOUT_ERROR_MARKERS)
