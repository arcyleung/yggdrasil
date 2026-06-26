"""Tests for optional regex content scrubber."""
from __future__ import annotations

from yggdrasil.adapters.regex_scrubber import RegexContentScrubber


def test_redacts_email():
    s = RegexContentScrubber()
    out = s.scrub_text("contact alice@example.com for help")
    assert "alice@example.com" not in out
    assert "[REDACTED_EMAIL]" in out


def test_preserves_allowed_owner_name_exactly():
    s = RegexContentScrubber()
    # Owner name that happens to look like it could be part of PII context
    out = s.scrub_text(
        "owner alice ran the agent; email was alice@corp.io",
        allowed_names=["alice"],
    )
    assert "alice" in out
    assert "alice@corp.io" not in out
    assert "[REDACTED_EMAIL]" in out


def test_redacts_sk_and_bearer_keys():
    s = RegexContentScrubber()
    out = s.scrub_text(
        "key=sk-abcdefghijklmnopqrstuvwxyz123456 Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc"
    )
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in out
    assert "Bearer eyJhbGciOiJIUzI1NiJ9.abc" not in out
    assert out.count("[REDACTED_SECRET]") >= 2


def test_allowed_name_not_partial_redact_when_exact():
    s = RegexContentScrubber()
    out = s.scrub_text("team lead is bob", allowed_names=["bob"])
    assert out == "team lead is bob"
