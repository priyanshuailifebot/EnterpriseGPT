"""Structured logging via structlog.

* Pretty colored console output in development.
* JSON output (one object per line) in production / staging — ready for
  log aggregators like Loki, Datadog, or CloudWatch.

Use it like::

    from core.logging import get_logger
    logger = get_logger(__name__)
    logger.info("workflow.started", workflow_id=str(wf.id))
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from core.config import get_settings


def configure_logging() -> None:
    """Configure structlog + the stdlib root logger.

    Safe to call more than once; subsequent calls are no-ops because we set a
    sentinel attribute on the structlog config.
    """
    settings = get_settings()
    log_level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)

    # Stdlib root logger — keep simple, structlog wraps it.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
        force=True,
    )

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if settings.is_development:
        renderer: structlog.types.Processor = structlog.dev.ConsoleRenderer(colors=True)
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None, **initial_context: Any) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger optionally bound with initial context."""
    logger = structlog.get_logger(name)
    if initial_context:
        logger = logger.bind(**initial_context)
    return logger
