"""Minimal regex content scrubber (emails, API-key-like secrets).

Preserves ``allowed_names`` tokens exactly (owner attribution exception).
Not a substitute for LLM redaction or multi-tenant isolation — PoC opt-in only.
"""
from __future__ import annotations

import re
from typing import Sequence

# Email addresses
_EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"
)
# OpenAI-style / generic sk- keys (sk-… alphanumeric)
_SK_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}\b")
# Bearer tokens in Authorization-style strings
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE)
# Simple NANP-ish phones (optional)
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}(?!\d)"
)

_REDACT_EMAIL = "[REDACTED_EMAIL]"
_REDACT_SECRET = "[REDACTED_SECRET]"
_REDACT_PHONE = "[REDACTED_PHONE]"


class RegexContentScrubber:
    """Regex-only scrubber implementing ContentScrubber protocol."""

    def scrub_text(self, text: str, *, allowed_names: Sequence[str] = ()) -> str:
        if not text:
            return text
        allow = {n for n in allowed_names if n}
        # Protect allowlisted **whole-word** tokens only (not substrings of emails)
        protected: list[tuple[str, str]] = []
        out = text
        for i, name in enumerate(sorted(allow, key=len, reverse=True)):
            if not name:
                continue
            # Do not protect local-part of emails (name@domain)
            pat = re.compile(
                rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_@])"
            )
            if not pat.search(out):
                continue
            token = f"\x00YGG_ALLOW_{i}\x00"
            protected.append((token, name))
            out = pat.sub(token, out)

        out = _EMAIL_RE.sub(_REDACT_EMAIL, out)
        out = _SK_KEY_RE.sub(_REDACT_SECRET, out)
        out = _BEARER_RE.sub(_REDACT_SECRET, out)
        out = _PHONE_RE.sub(_REDACT_PHONE, out)

        for token, name in protected:
            out = out.replace(token, name)
        return out
