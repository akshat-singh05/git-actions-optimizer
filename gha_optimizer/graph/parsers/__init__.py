"""
Language parser registry.

Provides a common interface for all language-specific AST parsers.
Each parser implements BaseLanguageParser and reports its capabilities
honestly.
"""
from gha_optimizer.graph.parsers.base import BaseLanguageParser

__all__ = ["BaseLanguageParser"]
