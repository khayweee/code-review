"""Distinct, actionable failures for one Agent call.

https://github.com/khayweee/code-review/issues/4 - naming which stage broke (the
process never started, the process exited non-zero, no structured answer was
present anywhere, or an answer was found but did not fit the schema) lets a step
author choose retry, fallback, or ask-user without inspecting a generic message.
"""

from __future__ import annotations


class AgentError(Exception):
    """Base for every distinct failure an ``Agent.run`` call can raise."""


class ProcessStartError(AgentError):
    """The backend subprocess never started (e.g. the executable was not found)."""

    def __init__(self, executable: str, cause: OSError) -> None:
        super().__init__(f"could not start {executable!r}: {cause}")
        self.executable = executable
        self.cause = cause


class ProcessExitError(AgentError):
    """The backend subprocess started but exited with a non-zero status."""

    def __init__(self, returncode: int, stderr: str) -> None:
        super().__init__(f"backend exited with status {returncode}: {stderr}")
        self.returncode = returncode
        self.stderr = stderr


class NoStructuredOutputError(AgentError):
    """No structured answer could be found anywhere in the backend's response."""

    def __init__(self, text: str) -> None:
        super().__init__(f"no structured answer found in response: {text!r}")
        self.text = text


class OutputValidationError(AgentError):
    """A structured answer was found but did not fit the caller's schema."""

    def __init__(self, value: object, cause: Exception) -> None:
        super().__init__(f"structured answer failed schema validation: {cause}")
        self.value = value
        self.cause = cause
