"""Tool errors: raised inside the tools, returned as a dict by ``Toolbox.call``."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

UNKNOWN_TOOL = "unknown_tool"
BAD_ARGUMENTS = "bad_arguments"  # arguments are not a JSON object
BAD_ARGUMENT = "bad_argument"  # one argument is wrong
WINDOW_TOO_LONG = "window_too_long"
UNAVAILABLE = "unavailable"  # a cache stage is missing


class ToolError(Exception):
    """Bad tool input; ``Toolbox.call`` returns ``result()`` instead of raising."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        argument: str | None = None,
        hint: str | None = None,
        **extra: Any,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.argument = argument
        self.hint = hint
        self.extra = extra

    def result(self) -> dict[str, Any]:
        error: dict[str, Any] = {
            "code": self.code,
            "argument": self.argument,
            "message": self.message,
        }
        if self.hint is not None:
            error["hint"] = self.hint
        error.update(self.extra)
        return {"error": error}


def is_error(result: Mapping[str, Any]) -> bool:
    """Whether a tool result is an error result."""
    return isinstance(result.get("error"), dict)
