"""Exit codes and the one exception type that carries them.

The codes are outside the range that Python (1), argparse (2) and uv use, so an operator can
tell "the tool decided this" from "the tool did not start".
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any


class Exit(IntEnum):
    OK = 0
    FAILED_ROLLED_BACK = 21  # the unit of work failed and was rolled back; nothing of it remains
    REFUSED = 22  # nothing was executed
    UNKNOWN = 23  # outcome unknown; a human must inspect and run `resolve`
    RETRY_SAFE = 24  # clean stop; safe to start again
    LOCKED = 25  # another run holds the lock
    DRIFT = 30  # drift found (`drift` command only)


class ToolError(Exception):
    """A decision of the tool, with the exit code and a stable reason code for the runbook.

    reason_code is an UPPER_SNAKE string, for example STALE_PLAN. It is written to report.json
    and never changes meaning between versions.
    """

    def __init__(
        self,
        exit_code: Exit,
        reason_code: str,
        message: str,
        *,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.reason_code = reason_code
        self.message = message
        self.detail = detail or {}

    def __str__(self) -> str:
        return f"{self.reason_code}: {self.message}"


def refused(reason_code: str, message: str, **detail: Any) -> ToolError:
    return ToolError(Exit.REFUSED, reason_code, message, detail=detail)


def failed(reason_code: str, message: str, **detail: Any) -> ToolError:
    return ToolError(Exit.FAILED_ROLLED_BACK, reason_code, message, detail=detail)


def unknown(reason_code: str, message: str, **detail: Any) -> ToolError:
    return ToolError(Exit.UNKNOWN, reason_code, message, detail=detail)


def retry_safe(reason_code: str, message: str, **detail: Any) -> ToolError:
    return ToolError(Exit.RETRY_SAFE, reason_code, message, detail=detail)


def locked(reason_code: str, message: str, **detail: Any) -> ToolError:
    return ToolError(Exit.LOCKED, reason_code, message, detail=detail)
