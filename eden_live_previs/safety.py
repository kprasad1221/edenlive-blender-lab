"""Limits shared by the UI, SDK adapter, watchdog, and command-line test."""

DEFAULT_PAID_SECONDS = 30
MIN_PAID_SECONDS = 10
MAX_PAID_SECONDS = 120
CONNECT_TIMEOUT = 20.0
FIRST_FRAME_TIMEOUT = 15.0
OUTPUT_STALL_TIMEOUT = 10.0
CLOSE_TIMEOUT = 5.0


def paid_limit(value):
    """Reject disabling or silently enlarging the bound, including through Python."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Paid session limit must be a whole number of seconds.")
    if not MIN_PAID_SECONDS <= value <= MAX_PAID_SECONDS:
        raise ValueError(f"Paid session limit must be {MIN_PAID_SECONDS}–{MAX_PAID_SECONDS} seconds.")
    return value
