"""Validate an agent's final response against an ``OutputParserNode`` schema.

The flow at the runtime:

1. Agent's tool loop concludes with a text response.
2. ``parse_or_retry`` is called with that response, the parser node, and
   a callable that lets us re-prompt the LLM with the validation error.
3. We JSON-decode the response — tolerating a ```code fence or prose
   wrapped around an embedded ``{...}`` / ``[...]`` payload — then validate
   against ``json_schema``. On success the structured value is returned.
4. On failure we re-prompt the LLM up to ``max_retries`` times, each
   time appending a short corrective message describing what's wrong.

We deliberately use ``jsonschema`` (already a transitive dep of the API
through pydantic) so we get accurate keyword coverage without writing a
mini-validator. When ``jsonschema`` isn't importable (lightweight test
envs) we fall back to a small recursive checker covering ``type``,
``required``, ``properties``, and ``enum`` — enough for the templates we
ship today.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from schemas.workflow import OutputParserNode

log = logging.getLogger(__name__)


@dataclass
class ParseResult:
    ok: bool
    value: Any
    raw_text: str
    error: str | None = None
    attempts: int = 1


# ---------------------------------------------------------------------------
# Validation backends
# ---------------------------------------------------------------------------


def _validate(value: Any, schema: dict[str, Any]) -> str | None:
    """Returns ``None`` on success, an error message on failure."""
    try:
        import jsonschema
    except ImportError:  # pragma: no cover — fallback for slim envs
        return _validate_inline(value, schema)
    try:
        jsonschema.validate(value, schema)
        return None
    except jsonschema.ValidationError as exc:  # type: ignore[attr-defined]
        path = ".".join(str(p) for p in exc.absolute_path)
        if path:
            return f"at `{path}`: {exc.message}"
        return exc.message


def _validate_inline(value: Any, schema: dict[str, Any]) -> str | None:
    """Tiny offline subset of JSON Schema covering the keywords we use."""
    t = schema.get("type")
    if t == "object":
        if not isinstance(value, dict):
            return f"expected object, got {type(value).__name__}"
        for req in schema.get("required", []):
            if req not in value:
                return f"missing required field `{req}`"
        for k, sub in (schema.get("properties") or {}).items():
            if k in value:
                err = _validate_inline(value[k], sub)
                if err:
                    return f"at `{k}`: {err}"
        return None
    if t == "array":
        if not isinstance(value, list):
            return f"expected array, got {type(value).__name__}"
        sub = schema.get("items")
        if isinstance(sub, dict):
            for i, item in enumerate(value):
                err = _validate_inline(item, sub)
                if err:
                    return f"at [{i}]: {err}"
        return None
    if isinstance(t, list):
        # JSON-Schema ``type: ["string", "null"]`` — match any.
        for sub_t in t:
            err = _validate_inline(value, {**schema, "type": sub_t})
            if err is None:
                return None
        return f"value didn't match any of types {t!r}"
    if t == "string":
        if value is None and "null" in (schema.get("type") or []):
            return None
        if not isinstance(value, str):
            return f"expected string, got {type(value).__name__}"
        if "enum" in schema and value not in schema["enum"]:
            return f"value {value!r} not in enum {schema['enum']!r}"
        return None
    if t == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            return f"expected integer, got {type(value).__name__}"
        return None
    if t == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return f"expected number, got {type(value).__name__}"
        return None
    if t == "boolean":
        if not isinstance(value, bool):
            return f"expected boolean, got {type(value).__name__}"
        return None
    if t == "null":
        if value is not None:
            return f"expected null, got {type(value).__name__}"
        return None
    return None  # unknown / missing type — pass through


# ---------------------------------------------------------------------------
# Re-prompt loop
# ---------------------------------------------------------------------------


async def parse_or_retry(
    *,
    node: OutputParserNode,
    initial_text: str,
    reprompt: Callable[[str], Awaitable[str]],
) -> ParseResult:
    """Try to parse + validate; on failure re-prompt up to ``max_retries`` times.

    ``reprompt(error_message)`` MUST drive the LLM with a corrective user
    turn ("Your prior response failed schema validation: <err>. Reply
    again with ONLY corrected JSON.") and return the LLM's new raw text.
    Keeping that callback at the runtime layer is what lets ``parse_or_retry``
    stay LLM-agnostic.
    """
    schema = node.json_schema or {}
    text = initial_text
    attempts = 0
    last_error = "could not parse JSON from agent response"

    for attempt in range(node.max_retries + 1):
        attempts = attempt + 1
        try:
            value = _extract_json(text)
        except ValueError as exc:
            last_error = f"invalid JSON: {exc}"
            if attempt < node.max_retries:
                text = await reprompt(last_error)
                continue
            return ParseResult(
                ok=False, value=None, raw_text=text, error=last_error,
                attempts=attempts,
            )

        err = _validate(value, schema) if schema else None
        if err is None:
            return ParseResult(
                ok=True, value=value, raw_text=text, error=None, attempts=attempts,
            )

        last_error = err
        if attempt < node.max_retries:
            text = await reprompt(err)
            continue
        return ParseResult(
            ok=False, value=value, raw_text=text, error=err, attempts=attempts,
        )

    # Unreachable, but satisfies the type checker.
    return ParseResult(
        ok=False, value=None, raw_text=text, error=last_error, attempts=attempts,
    )


def _extract_json(text: str) -> Any:
    """Parse JSON from ``text``, tolerating prose around a JSON object/array.

    LLMs often wrap the structured payload in prose ("Sure! Here's the JSON:
    {...}") or a ```code fence even when asked for JSON only. Strategy:

    1. Strip a code fence and try to parse the whole thing (the strict path —
       what a well-behaved JSON-only agent returns).
    2. Failing that, locate the first balanced ``{...}`` or ``[...]`` span and
       parse that.

    Raises ``ValueError`` when no parseable JSON is found, so the caller can
    treat it the same as a hard parse failure and re-prompt.
    """
    cleaned = _strip_code_fence(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    span = _first_json_span(cleaned)
    if span is None:
        raise ValueError("no JSON object or array found in response")
    try:
        return json.loads(span)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"embedded JSON did not parse: {exc.msg} at column {exc.colno}"
        ) from exc


def extract_json_loose(text: str) -> Any | None:
    """Best-effort JSON extraction that returns ``None`` instead of raising.

    For callers like ``for_each`` item resolution that want "give me the list
    if there is one, tolerating a code fence or surrounding prose" without the
    re-prompt machinery.
    """
    try:
        return _extract_json(text)
    except ValueError:
        return None


def _first_json_span(s: str) -> str | None:
    """Return the first balanced ``{...}`` / ``[...]`` substring, or ``None``.

    Tracks string state so braces inside string literals don't throw off the
    depth count. Returns the span verbatim for ``json.loads`` to validate.
    """
    starts = [i for i in (s.find("{"), s.find("[")) if i != -1]
    if not starts:
        return None
    start = min(starts)
    open_ch = s[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    return None


def _strip_code_fence(text: str) -> str:
    """LLMs love wrapping JSON in ```json blocks. Tolerate it."""
    s = (text or "").strip()
    if s.startswith("```"):
        s = s.lstrip("`")
        first_newline = s.find("\n")
        if first_newline != -1 and s[:first_newline].strip().lower() in {"", "json"}:
            s = s[first_newline + 1 :]
        if s.endswith("```"):
            s = s[: -3]
    return s.strip()


__all__ = ["ParseResult", "parse_or_retry", "extract_json_loose"]
