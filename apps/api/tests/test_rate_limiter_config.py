"""``RateLimitConfig.from_dict`` parsing — env-independent."""

from __future__ import annotations

from services.chat_rate_limiter import RateLimitConfig


def test_empty_input_returns_none() -> None:
    assert RateLimitConfig.from_dict(None) is None
    assert RateLimitConfig.from_dict({}) is None


def test_all_keys_present() -> None:
    cfg = RateLimitConfig.from_dict(
        {
            "messages_per_minute": 30,
            "max_total_tokens": 100_000,
            "max_total_cost_cents": 500,
        }
    )
    assert cfg is not None
    assert cfg.messages_per_minute == 30
    assert cfg.max_total_tokens == 100_000
    assert cfg.max_total_cost_cents == 500


def test_partial_keys_allowed() -> None:
    cfg = RateLimitConfig.from_dict({"messages_per_minute": 60})
    assert cfg is not None
    assert cfg.messages_per_minute == 60
    assert cfg.max_total_tokens is None
    assert cfg.max_total_cost_cents is None


def test_all_zero_keys_returns_none() -> None:
    # All three keys missing → no limits — config builder collapses to None.
    assert RateLimitConfig.from_dict({"unknown_key": 42}) is None
