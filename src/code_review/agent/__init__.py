"""Agent abstraction: one call in, one result out."""

from code_review.agent.base import Agent, Result, RunOpts, Usage
from code_review.agent.claude_cli import ClaudeCLI

__all__ = ["Agent", "ClaudeCLI", "Result", "RunOpts", "Usage"]
