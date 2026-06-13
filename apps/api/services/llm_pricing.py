"""LLM pricing — model id → USD per 1K tokens.

Costs are stored in **micro-cents** (1/1,000,000 of a USD cent) internally
so we can sum many small per-chunk costs without floating-point drift and
still convert cleanly to whole cents at session aggregation time. Public
helpers return whole-cent values which is what the API + UI consume.

The table is hand-curated against the vendors' public pricing pages and
versioned by ``PRICING_REVISION`` so audits can pin a session to the
pricing that was in effect when it ran. Unknown models default to a
conservative "free tier" so a missing entry can never inflate a bill —
the operator just sees ``$0.00`` and knows to add the model to this
file. Telemetry will surface unmatched model ids in logs.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

log = logging.getLogger(__name__)


# Bump on every table edit so historical executions can be re-priced
# against the right snapshot if we ever need to.
PRICING_REVISION = "2026-05-18"


@dataclass(frozen=True)
class ModelPrice:
    """USD per 1,000 tokens, as fractional cents (i.e. 0.5 == half a cent)."""

    model_id: str
    input_cents_per_1k: float
    output_cents_per_1k: float
    family: str  # "openai" | "anthropic" | "azure" | "other"


# ---------------------------------------------------------------------------
# Table — keep alphabetical within family for review-friendliness.
# ---------------------------------------------------------------------------


_MODELS: tuple[ModelPrice, ...] = (
    # ---- OpenAI direct (api.openai.com) ----
    ModelPrice("gpt-4o",         input_cents_per_1k=0.25, output_cents_per_1k=1.00,  family="openai"),
    ModelPrice("gpt-4o-mini",    input_cents_per_1k=0.015, output_cents_per_1k=0.06, family="openai"),
    ModelPrice("gpt-4.1",        input_cents_per_1k=0.20, output_cents_per_1k=0.80,  family="openai"),
    ModelPrice("gpt-4.1-mini",   input_cents_per_1k=0.04, output_cents_per_1k=0.16,  family="openai"),
    ModelPrice("gpt-5",          input_cents_per_1k=0.50, output_cents_per_1k=2.00,  family="openai"),
    ModelPrice("o4-mini",        input_cents_per_1k=0.11, output_cents_per_1k=0.44,  family="openai"),
    # ---- Anthropic ----
    ModelPrice("claude-opus-4-7",        input_cents_per_1k=1.50, output_cents_per_1k=7.50, family="anthropic"),
    ModelPrice("claude-sonnet-4-6",      input_cents_per_1k=0.30, output_cents_per_1k=1.50, family="anthropic"),
    ModelPrice("claude-haiku-4-5",       input_cents_per_1k=0.08, output_cents_per_1k=0.40, family="anthropic"),
    ModelPrice("claude-3-5-sonnet-20241022", input_cents_per_1k=0.30, output_cents_per_1k=1.50, family="anthropic"),
    # ---- Azure (matches the corresponding OpenAI model) ----
    ModelPrice("gpt-4o-azure",       input_cents_per_1k=0.25, output_cents_per_1k=1.00,  family="azure"),
    ModelPrice("gpt-4o-mini-azure",  input_cents_per_1k=0.015, output_cents_per_1k=0.06, family="azure"),
)

_BY_ID: dict[str, ModelPrice] = {m.model_id: m for m in _MODELS}


def _normalise(model_id: str) -> str:
    """Strip vendor prefixes / Azure deployment suffixes so the lookup is forgiving.

    Azure deployments are typically named ``<workspace>-gpt-4o`` etc. — we
    accept the bare model id as the canonical key.
    """
    s = (model_id or "").strip().lower()
    if not s:
        return ""
    # Strip ``openai/`` or ``anthropic/`` prefix some clients pass.
    for prefix in ("openai/", "anthropic/", "azure/"):
        if s.startswith(prefix):
            s = s[len(prefix):]
    return s


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def lookup(model_id: str) -> ModelPrice | None:
    norm = _normalise(model_id)
    if not norm:
        return None
    if norm in _BY_ID:
        return _BY_ID[norm]
    # Best-effort: try stripping a known-deployment suffix.
    for candidate in (norm.removesuffix("-azure"), norm.split("/")[-1]):
        if candidate in _BY_ID:
            return _BY_ID[candidate]
    log.debug("llm_pricing.unknown_model", model_id=model_id)
    return None


def cost_microcents(
    model_id: str,
    *,
    prompt_tokens: int,
    completion_tokens: int,
) -> int:
    """Return the cost in **micro-cents** (1/1,000,000 of a USD cent).

    Sub-cent precision matters when many small turns aggregate — a Slack
    bot may average 30 micro-cents/turn, and rounding each turn to cents
    would zero out the total. We sum in micro-cents and round once at
    aggregation time.
    """
    price = lookup(model_id)
    if price is None:
        return 0
    in_mc = math.ceil((prompt_tokens / 1000.0) * price.input_cents_per_1k * 1_000_000)
    out_mc = math.ceil((completion_tokens / 1000.0) * price.output_cents_per_1k * 1_000_000)
    return int(in_mc + out_mc)


def microcents_to_cents(microcents: int) -> int:
    """Round up to whole cents. Use only at aggregation / display time."""
    if microcents <= 0:
        return 0
    return int(math.ceil(microcents / 1_000_000))


def estimate_cents(
    model_id: str,
    *,
    prompt_tokens: int,
    completion_tokens: int,
) -> int:
    """Whole-cent estimate. Sums micro-cents then rounds up once."""
    return microcents_to_cents(
        cost_microcents(
            model_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
    )


__all__ = [
    "ModelPrice",
    "PRICING_REVISION",
    "cost_microcents",
    "estimate_cents",
    "lookup",
    "microcents_to_cents",
]
