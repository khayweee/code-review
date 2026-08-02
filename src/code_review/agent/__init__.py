"""Agent abstraction: one call in, one result out."""

from code_review.agent.base import Agent, Result, RunOpts, Usage
from code_review.agent.claude_cli import ClaudeCLI
from code_review.agent.errors import (
    AgentError,
    NoStructuredOutputError,
    OutputValidationError,
    ProcessExitError,
    ProcessStartError,
    StdinBlockedError,
)

__all__ = [
    "Agent",
    "AgentError",
    "ClaudeCLI",
    "NoStructuredOutputError",
    "OutputValidationError",
    "ProcessExitError",
    "ProcessStartError",
    "Result",
    "RunOpts",
    "StdinBlockedError",
    "Usage",
]
