"""Distinct, actionable failures for one Agent call.

https://github.com/khayweee/code-review/issues/4 - naming which stage broke (the
process never started, the process exited non-zero, no structured answer was
present anywhere, or an answer was found but did not fit the schema) lets a step
author choose retry, fallback, or ask-user without inspecting a generic message.

https://github.com/khayweee/code-review/issues/41 adds a fifth: the subprocess appeared
blocked waiting on stdin and no ``RunOpts.on_input_needed`` was supplied to relay the
prompt to a human, so the backend fails closed rather than hanging or fabricating an
answer.
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


class StdinBlockedError(AgentError):
    """The subprocess appeared blocked waiting on stdin with no relay to answer it.

    Raised only on the non-default path (``tools_allowlist`` set, or ``permission_mode``
    pinned) once the backend's idle-read timeout elapses with no ``RunOpts.on_input_needed``
    supplied to relay the detected prompt to a human. Carries the stdout accumulated before
    the stall so a caller can see what the subprocess was asking for.
    """

    def __init__(self, stdout_so_far: str) -> None:
        super().__init__(
            "backend subprocess appears blocked waiting on stdin and no "
            "on_input_needed was supplied to relay the prompt: "
            f"{stdout_so_far!r}"
        )
        self.stdout_so_far = stdout_so_far
