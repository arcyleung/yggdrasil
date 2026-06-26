"""Content scrubber port — optional PII/secret redaction on write paths."""
from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable


@runtime_checkable
class ContentScrubber(Protocol):
    """Scrub free-text fields while preserving allowlisted owner names exactly."""

    def scrub_text(self, text: str, *, allowed_names: Sequence[str] = ()) -> str:
        """Return text with emails/secrets redacted; tokens in allowed_names preserved."""
        ...
