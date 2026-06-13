"""Langfuse OpenTelemetry tracing: client bootstrap, LLM span helper, shutdown flush."""

from __future__ import annotations

import functools
import json
from collections.abc import Awaitable, Callable
from typing import Any, ParamSpec, TypeVar

from langfuse import Langfuse, get_client, observe

from core.config import Settings, get_settings

_client_registered: Langfuse | None = None

P = ParamSpec("P")
R = TypeVar("R")


def init_langfuse_from_settings(settings: Settings | None = None) -> Langfuse | None:
    """Instantiate and register the Langfuse client (required before @observe runs)."""
    global _client_registered
    if _client_registered is not None:
        return _client_registered
    s = settings or get_settings()
    pk = (s.LANGFUSE_PUBLIC_KEY or "").strip()
    sk = (s.LANGFUSE_SECRET_KEY or "").strip()
    if not pk or not sk:
        return None
    host = (s.LANGFUSE_HOST or "").strip().rstrip("/") or "https://cloud.langfuse.com"
    _client_registered = Langfuse(
        public_key=pk,
        secret_key=sk,
        host=host,
        environment=s.ENVIRONMENT,
        release=f"{s.APP_NAME}@{s.APP_VERSION}",
    )
    return _client_registered


def get_tracer() -> Langfuse | None:
    """Singleton Langfuse client configured from settings, or ``None`` when disabled."""
    if _client_registered is None:
        init_langfuse_from_settings()
    return _client_registered


def flush_traces() -> None:
    """Block until queued spans are sent (call on process shutdown)."""
    client = _client_registered
    if client is None:
        return
    try:
        client.flush()
        client.shutdown()
    except Exception:  # noqa: BLE001 — never block shutdown on telemetry
        pass


def _truncate_for_trace(val: Any, max_len: int = 8000) -> Any:
    if val is None:
        return None
    try:
        text = json.dumps(val, default=str)
    except (TypeError, ValueError):
        text = repr(val)
    if len(text) <= max_len:
        return json.loads(text) if text.startswith(("{", "[")) else text
    return text[:max_len] + "…[truncated]"


def _usage_from_result(result: Any) -> dict[str, int] | None:
    usage = getattr(result, "usage", None)
    if usage is None:
        return None
    pt = getattr(usage, "prompt_tokens", None)
    ct = getattr(usage, "completion_tokens", None)
    tt = getattr(usage, "total_tokens", None)
    out: dict[str, int] = {}
    if pt is not None:
        out["input"] = int(pt)
    if ct is not None:
        out["output"] = int(ct)
    if tt is not None:
        out["total"] = int(tt)
    return out or None


def trace_llm(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Wrap an async LLM-facing function with a Langfuse *generation* observation."""

    @functools.wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        init_langfuse_from_settings()
        client = get_client()
        if not client._tracing_enabled:  # type: ignore[attr-defined]
            return await func(*args, **kwargs)

        settings = get_settings()
        model = settings.AZURE_OPENAI_DEPLOYMENT or settings.AZURE_OPENAI_DEFAULT_MODEL
        inp = _truncate_for_trace({"args": args, "kwargs": kwargs})

        with client.start_as_current_observation(
            as_type="generation",
            name=func.__name__,
            model=model,
            input=inp,
        ) as obs:
            try:
                result = await func(*args, **kwargs)
            except Exception as exc:
                obs.update(level="ERROR", status_message=str(exc))
                raise
            usage = _usage_from_result(result)
            obs.update(
                output=_truncate_for_result(result),
                usage_details=usage,
            )
            return result

    return wrapper


def _truncate_for_result(result: Any, max_len: int = 12000) -> Any:
    if hasattr(result, "model_dump"):
        return _truncate_for_trace(result.model_dump())
    return _truncate_for_trace(result)


__all__ = [
    "flush_traces",
    "get_tracer",
    "init_langfuse_from_settings",
    "observe",
    "trace_llm",
]
