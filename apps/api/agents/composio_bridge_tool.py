"""Dynamiq tool nodes that invoke Composio actions synchronously."""

from __future__ import annotations

from typing import Any, Callable, ClassVar

from pydantic import BaseModel, ConfigDict, PrivateAttr

from dynamiq.nodes.tools.function_tool import FunctionTool


class FlexibleToolInput(BaseModel):
    model_config = ConfigDict(extra="allow")


class ComposioBridgeTool(FunctionTool[Any]):
    """Routes agent tool calls into :meth:`ToolRegistry.sync_execute_action`."""

    input_schema: ClassVar[type[BaseModel]] = FlexibleToolInput

    _invoke_fn: Callable[[dict[str, Any]], Any] = PrivateAttr()

    def __init__(
        self,
        *,
        action_slug: str,
        description: str,
        invoke_fn: Callable[[dict[str, Any]], Any],
        **kwargs: Any,
    ) -> None:
        super().__init__(
            name=action_slug,
            description=(description or "")[:4096],
            id=kwargs.pop("id", f"composio-{action_slug}"),
            **kwargs,
        )
        self._invoke_fn = invoke_fn

    def run_func(self, input_data: BaseModel, config=None, **kwargs: Any) -> Any:
        return self._invoke_fn(input_data.model_dump(mode="python"))
