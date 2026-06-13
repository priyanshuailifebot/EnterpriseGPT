"""OutputParserService — JSON validation + re-prompt retry."""

from __future__ import annotations

import pytest

from schemas.workflow import OutputParserNode
from services.output_parser_service import parse_or_retry


@pytest.fixture
def schema_simple() -> dict:
    return {
        "type": "object",
        "required": ["intent", "ticket_id"],
        "properties": {
            "intent": {"type": "string", "enum": ["new", "existing"]},
            "ticket_id": {"type": ["string", "null"]},
        },
    }


@pytest.mark.asyncio
async def test_valid_json_passes_first_attempt(schema_simple: dict) -> None:
    node = OutputParserNode(
        id="p", name="p", json_schema=schema_simple, max_retries=2,
    )

    async def never_reprompt(_err: str) -> str:
        raise AssertionError("should not be re-prompted")

    result = await parse_or_retry(
        node=node,
        initial_text='{"intent": "new", "ticket_id": null}',
        reprompt=never_reprompt,
    )
    assert result.ok is True
    assert result.value == {"intent": "new", "ticket_id": None}
    assert result.attempts == 1


@pytest.mark.asyncio
async def test_code_fence_is_stripped(schema_simple: dict) -> None:
    node = OutputParserNode(
        id="p", name="p", json_schema=schema_simple, max_retries=0,
    )

    async def reprompt(_err: str) -> str:
        return ""

    result = await parse_or_retry(
        node=node,
        initial_text='```json\n{"intent": "existing", "ticket_id": "T-1"}\n```',
        reprompt=reprompt,
    )
    assert result.ok is True
    assert result.value["intent"] == "existing"


@pytest.mark.asyncio
async def test_invalid_json_reprompts_then_succeeds(schema_simple: dict) -> None:
    node = OutputParserNode(
        id="p", name="p", json_schema=schema_simple, max_retries=2,
    )
    calls: list[str] = []

    async def reprompt(err: str) -> str:
        calls.append(err)
        return '{"intent": "new", "ticket_id": null}'

    result = await parse_or_retry(
        node=node,
        initial_text="not valid json at all",
        reprompt=reprompt,
    )
    assert result.ok is True
    assert result.attempts == 2
    assert len(calls) == 1
    assert "invalid JSON" in calls[0]


@pytest.mark.asyncio
async def test_schema_mismatch_reprompts(schema_simple: dict) -> None:
    node = OutputParserNode(
        id="p", name="p", json_schema=schema_simple, max_retries=2,
    )
    seen_errors: list[str] = []

    async def reprompt(err: str) -> str:
        seen_errors.append(err)
        # Fix it on the retry.
        return '{"intent": "new", "ticket_id": null}'

    # Wrong enum value first.
    result = await parse_or_retry(
        node=node,
        initial_text='{"intent": "maybe", "ticket_id": null}',
        reprompt=reprompt,
    )
    assert result.ok is True
    assert result.attempts == 2
    # The corrective message references the enum failure on the first try.
    assert any("intent" in e or "enum" in e.lower() for e in seen_errors)


@pytest.mark.asyncio
async def test_exhausted_retries_returns_last_error(schema_simple: dict) -> None:
    node = OutputParserNode(
        id="p", name="p", json_schema=schema_simple, max_retries=1,
    )

    async def reprompt(_err: str) -> str:
        return "still not valid"

    result = await parse_or_retry(
        node=node,
        initial_text="garbage",
        reprompt=reprompt,
    )
    assert result.ok is False
    assert result.attempts == 2  # initial + 1 retry
    assert result.error
