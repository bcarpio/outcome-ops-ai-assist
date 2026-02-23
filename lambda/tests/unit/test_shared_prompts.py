"""
Unit tests for shared prompts module.

Tests the prompt constants and builder functions used across
ask-claude and chat handlers for RAG-based responses.
"""

import sys
import os

# Add shared module to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../shared'))

from prompts import (
    CONTEXT_TYPES,
    FORMATTING_RULES,
    REQUIREMENT_STRENGTH_RULES,
    build_rag_system_prompt,
    build_chat_system_prompt,
)


class TestPromptConstants:
    """Test prompt constant definitions."""

    def test_context_types_includes_adrs(self):
        """Test: CONTEXT_TYPES includes ADR documentation."""
        assert "ADRs" in CONTEXT_TYPES
        assert "Architecture Decision Records" in CONTEXT_TYPES

    def test_context_types_includes_code_maps(self):
        """Test: CONTEXT_TYPES includes code maps."""
        assert "Code maps" in CONTEXT_TYPES
        assert "code structure" in CONTEXT_TYPES

    def test_context_types_includes_readme(self):
        """Test: CONTEXT_TYPES includes README files."""
        assert "README" in CONTEXT_TYPES

    def test_formatting_rules_no_emojis(self):
        """Test: FORMATTING_RULES prohibits emojis."""
        assert "NEVER use emojis" in FORMATTING_RULES

    def test_formatting_rules_includes_markdown(self):
        """Test: FORMATTING_RULES includes markdown guidance."""
        assert "bold" in FORMATTING_RULES
        assert "bullet points" in FORMATTING_RULES

    def test_requirement_strength_rules_includes_must(self):
        """Test: REQUIREMENT_STRENGTH_RULES includes RFC 2119 terms."""
        assert "MUST" in REQUIREMENT_STRENGTH_RULES
        assert "SHOULD" in REQUIREMENT_STRENGTH_RULES
        assert "MAY" in REQUIREMENT_STRENGTH_RULES


class TestBuildRagSystemPrompt:
    """Test RAG system prompt builder."""

    def test_returns_string(self):
        """Test: build_rag_system_prompt returns a string."""
        prompt = build_rag_system_prompt()
        assert isinstance(prompt, str)

    def test_includes_context_types(self):
        """Test: RAG prompt includes context types."""
        prompt = build_rag_system_prompt()
        assert "ADRs" in prompt
        assert "Code maps" in prompt

    def test_includes_instructions(self):
        """Test: RAG prompt includes instructions."""
        prompt = build_rag_system_prompt()
        assert "INSTRUCTIONS" in prompt
        assert "Cite the specific sources" in prompt

    def test_includes_requirement_strength(self):
        """Test: RAG prompt includes requirement strength guidance."""
        prompt = build_rag_system_prompt()
        assert "MUST" in prompt
        assert "SHOULD" in prompt


class TestBuildChatSystemPrompt:
    """Test chat system prompt builder."""

    def test_returns_string(self):
        """Test: build_chat_system_prompt returns a string."""
        prompt = build_chat_system_prompt()
        assert isinstance(prompt, str)

    def test_includes_context_types(self):
        """Test: Chat prompt includes context types."""
        prompt = build_chat_system_prompt()
        assert "ADRs" in prompt
        assert "Code maps" in prompt

    def test_includes_formatting_rules(self):
        """Test: Chat prompt includes formatting rules."""
        prompt = build_chat_system_prompt()
        assert "FORMATTING" in prompt
        assert "bold" in prompt

    def test_includes_conversation_memory(self):
        """Test: Chat prompt mentions conversation memory."""
        prompt = build_chat_system_prompt()
        assert "conversation memory" in prompt

    def test_includes_no_source_listing_rule(self):
        """Test: Chat prompt says not to list sources."""
        prompt = build_chat_system_prompt()
        assert "DO NOT list sources" in prompt

    def test_includes_technology_agnostic_rule(self):
        """Test: Chat prompt is technology agnostic."""
        prompt = build_chat_system_prompt()
        assert "DO NOT refuse requests" in prompt
