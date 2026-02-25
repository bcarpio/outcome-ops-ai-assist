"""
Unit tests for shared prompts module.

Tests the prompt constants and builder functions used across
ask-claude and chat handlers for RAG-based responses.
"""

import importlib.util
import os
import sys


# ============================================================================
# Module loading
# ============================================================================


def _load_prompts_module():
    """Load or reload the shared prompts module from its exact file path."""
    if "shared_prompts" in sys.modules:
        del sys.modules["shared_prompts"]

    shared_dir = os.path.join(os.path.dirname(__file__), "../../shared")
    prompts_path = os.path.join(shared_dir, "prompts.py")
    spec = importlib.util.spec_from_file_location("shared_prompts", prompts_path)
    prompts_module = importlib.util.module_from_spec(spec)
    sys.modules["shared_prompts"] = prompts_module
    spec.loader.exec_module(prompts_module)

    return prompts_module


prompts_module = _load_prompts_module()

# Import constants and functions
CONTEXT_TYPES = prompts_module.CONTEXT_TYPES
FORMATTING_RULES = prompts_module.FORMATTING_RULES
REQUIREMENT_STRENGTH_RULES = prompts_module.REQUIREMENT_STRENGTH_RULES
build_rag_system_prompt = prompts_module.build_rag_system_prompt
build_chat_system_prompt = prompts_module.build_chat_system_prompt


# ============================================================================
# CONTEXT_TYPES constant
# ============================================================================


class TestContextTypes:
    """Test CONTEXT_TYPES constant definition."""

    def test_context_types_is_nonempty_string(self):
        """Test CONTEXT_TYPES is a non-empty string."""
        assert isinstance(CONTEXT_TYPES, str)
        assert len(CONTEXT_TYPES) > 0

    def test_context_types_includes_adrs(self):
        """Test CONTEXT_TYPES includes ADR documentation."""
        assert "ADRs" in CONTEXT_TYPES
        assert "Architecture Decision Records" in CONTEXT_TYPES

    def test_context_types_includes_readme(self):
        """Test CONTEXT_TYPES includes README files."""
        assert "README" in CONTEXT_TYPES

    def test_context_types_includes_code_maps(self):
        """Test CONTEXT_TYPES includes code maps."""
        assert "Code maps" in CONTEXT_TYPES
        assert "code structure" in CONTEXT_TYPES

    def test_context_types_includes_infrastructure_patterns(self):
        """Test CONTEXT_TYPES includes infrastructure patterns."""
        assert "Infrastructure patterns" in CONTEXT_TYPES
        assert "Terraform" in CONTEXT_TYPES

    def test_context_types_uses_bullet_format(self):
        """Test CONTEXT_TYPES uses bullet-point list format."""
        lines = [line.strip() for line in CONTEXT_TYPES.strip().splitlines()]
        for line in lines:
            assert line.startswith("- "), f"Expected bullet format, got: {line}"


# ============================================================================
# FORMATTING_RULES constant
# ============================================================================


class TestFormattingRules:
    """Test FORMATTING_RULES constant definition."""

    def test_formatting_rules_is_nonempty_string(self):
        """Test FORMATTING_RULES is a non-empty string."""
        assert isinstance(FORMATTING_RULES, str)
        assert len(FORMATTING_RULES) > 0

    def test_formatting_rules_starts_with_header(self):
        """Test FORMATTING_RULES starts with 'FORMATTING:' header."""
        assert FORMATTING_RULES.startswith("FORMATTING:")

    def test_formatting_rules_prohibits_emojis(self):
        """Test FORMATTING_RULES prohibits emojis."""
        assert "NEVER use emojis" in FORMATTING_RULES

    def test_formatting_rules_includes_bold(self):
        """Test FORMATTING_RULES includes bold guidance."""
        assert "bold" in FORMATTING_RULES

    def test_formatting_rules_includes_bullet_points(self):
        """Test FORMATTING_RULES includes bullet point guidance."""
        assert "bullet points" in FORMATTING_RULES

    def test_formatting_rules_includes_code_blocks(self):
        """Test FORMATTING_RULES includes code block guidance."""
        assert "code blocks" in FORMATTING_RULES

    def test_formatting_rules_includes_paragraph_guidance(self):
        """Test FORMATTING_RULES includes paragraph length guidance."""
        assert "paragraphs short" in FORMATTING_RULES


# ============================================================================
# REQUIREMENT_STRENGTH_RULES constant
# ============================================================================


class TestRequirementStrengthRules:
    """Test REQUIREMENT_STRENGTH_RULES constant definition."""

    def test_requirement_strength_rules_is_nonempty_string(self):
        """Test REQUIREMENT_STRENGTH_RULES is a non-empty string."""
        assert isinstance(REQUIREMENT_STRENGTH_RULES, str)
        assert len(REQUIREMENT_STRENGTH_RULES) > 0

    def test_requirement_strength_rules_includes_must(self):
        """Test REQUIREMENT_STRENGTH_RULES includes MUST/SHALL/REQUIRED."""
        assert "MUST" in REQUIREMENT_STRENGTH_RULES
        assert "SHALL" in REQUIREMENT_STRENGTH_RULES
        assert "REQUIRED" in REQUIREMENT_STRENGTH_RULES

    def test_requirement_strength_rules_includes_should(self):
        """Test REQUIREMENT_STRENGTH_RULES includes SHOULD/RECOMMENDED."""
        assert "SHOULD" in REQUIREMENT_STRENGTH_RULES
        assert "RECOMMENDED" in REQUIREMENT_STRENGTH_RULES

    def test_requirement_strength_rules_includes_may(self):
        """Test REQUIREMENT_STRENGTH_RULES includes MAY/OPTIONAL."""
        assert "MAY" in REQUIREMENT_STRENGTH_RULES
        assert "OPTIONAL" in REQUIREMENT_STRENGTH_RULES

    def test_requirement_strength_rules_includes_must_not(self):
        """Test REQUIREMENT_STRENGTH_RULES includes MUST NOT/SHALL NOT."""
        assert "MUST NOT" in REQUIREMENT_STRENGTH_RULES
        assert "SHALL NOT" in REQUIREMENT_STRENGTH_RULES

    def test_requirement_strength_rules_prohibits_generalizing(self):
        """Test REQUIREMENT_STRENGTH_RULES tells not to generalize specific values."""
        assert "do not generalize" in REQUIREMENT_STRENGTH_RULES.lower()


# ============================================================================
# build_rag_system_prompt
# ============================================================================


class TestBuildRagSystemPrompt:
    """Test RAG system prompt builder."""

    def test_returns_string(self):
        """Test build_rag_system_prompt returns a string."""
        prompt = build_rag_system_prompt()
        assert isinstance(prompt, str)

    def test_returns_nonempty_string(self):
        """Test build_rag_system_prompt returns a non-empty string."""
        prompt = build_rag_system_prompt()
        assert len(prompt) > 0

    def test_includes_context_types(self):
        """Test RAG prompt embeds the CONTEXT_TYPES constant."""
        prompt = build_rag_system_prompt()
        assert "ADRs" in prompt
        assert "Code maps" in prompt
        assert "README" in prompt
        assert "Infrastructure patterns" in prompt

    def test_includes_instructions_section(self):
        """Test RAG prompt includes the INSTRUCTIONS section."""
        prompt = build_rag_system_prompt()
        assert "INSTRUCTIONS" in prompt

    def test_includes_citation_guidance(self):
        """Test RAG prompt instructs to cite sources."""
        prompt = build_rag_system_prompt()
        assert "Cite the specific sources" in prompt

    def test_includes_insufficient_context_guidance(self):
        """Test RAG prompt instructs to say when context is insufficient."""
        prompt = build_rag_system_prompt()
        assert "doesn't contain enough information" in prompt

    def test_includes_requirement_strength_rules(self):
        """Test RAG prompt embeds REQUIREMENT_STRENGTH_RULES."""
        prompt = build_rag_system_prompt()
        assert "MUST" in prompt
        assert "SHOULD" in prompt
        assert "MAY" in prompt
        assert "mandatory" in prompt

    def test_is_technology_agnostic(self):
        """Test RAG prompt does not refuse based on technology."""
        prompt = build_rag_system_prompt()
        assert "Do not refuse requests based on the technology" in prompt

    def test_supports_document_creation(self):
        """Test RAG prompt supports creating documents like ADRs."""
        prompt = build_rag_system_prompt()
        assert "create documents" in prompt.lower() or "ADRs, specs" in prompt

    def test_does_not_include_formatting_rules(self):
        """Test RAG prompt does not embed FORMATTING_RULES (chat-only)."""
        prompt = build_rag_system_prompt()
        assert "FORMATTING:" not in prompt

    def test_does_not_include_conversation_memory(self):
        """Test RAG prompt does not mention conversation memory (chat-only)."""
        prompt = build_rag_system_prompt()
        assert "conversation memory" not in prompt

    def test_returns_consistent_output(self):
        """Test build_rag_system_prompt returns the same output on repeated calls."""
        prompt1 = build_rag_system_prompt()
        prompt2 = build_rag_system_prompt()
        assert prompt1 == prompt2


# ============================================================================
# build_chat_system_prompt
# ============================================================================


class TestBuildChatSystemPrompt:
    """Test chat system prompt builder."""

    def test_returns_string(self):
        """Test build_chat_system_prompt returns a string."""
        prompt = build_chat_system_prompt()
        assert isinstance(prompt, str)

    def test_returns_nonempty_string(self):
        """Test build_chat_system_prompt returns a non-empty string."""
        prompt = build_chat_system_prompt()
        assert len(prompt) > 0

    def test_includes_context_types(self):
        """Test chat prompt embeds the CONTEXT_TYPES constant."""
        prompt = build_chat_system_prompt()
        assert "ADRs" in prompt
        assert "Code maps" in prompt
        assert "README" in prompt
        assert "Infrastructure patterns" in prompt

    def test_includes_formatting_rules(self):
        """Test chat prompt embeds the FORMATTING_RULES constant."""
        prompt = build_chat_system_prompt()
        assert "FORMATTING" in prompt
        assert "bold" in prompt
        assert "NEVER use emojis" in prompt

    def test_includes_conversation_memory(self):
        """Test chat prompt mentions conversation memory."""
        prompt = build_chat_system_prompt()
        assert "conversation memory" in prompt

    def test_includes_no_source_listing_rule(self):
        """Test chat prompt says not to list sources in the response."""
        prompt = build_chat_system_prompt()
        assert "DO NOT list sources" in prompt

    def test_includes_technology_agnostic_rule(self):
        """Test chat prompt does not refuse based on technology."""
        prompt = build_chat_system_prompt()
        assert "DO NOT refuse requests" in prompt

    def test_includes_requirement_strength_rules(self):
        """Test chat prompt embeds REQUIREMENT_STRENGTH_RULES."""
        prompt = build_chat_system_prompt()
        assert "MUST" in prompt
        assert "SHOULD" in prompt
        assert "MAY" in prompt
        assert "mandatory" in prompt

    def test_includes_rules_section(self):
        """Test chat prompt includes a RULES section."""
        prompt = build_chat_system_prompt()
        assert "RULES:" in prompt

    def test_includes_knowledge_base_grounding(self):
        """Test chat prompt instructs to answer based on knowledge base."""
        prompt = build_chat_system_prompt()
        assert "knowledge base" in prompt.lower()

    def test_includes_helpfulness_guidance(self):
        """Test chat prompt ends with helpfulness guidance."""
        prompt = build_chat_system_prompt()
        assert "Be helpful, not restrictive" in prompt

    def test_supports_document_creation(self):
        """Test chat prompt supports creating ADRs and documentation."""
        prompt = build_chat_system_prompt()
        assert "ADRs" in prompt
        assert "documentation" in prompt

    def test_does_not_assume_technology_stack(self):
        """Test chat prompt says not to assume what technologies to use."""
        prompt = build_chat_system_prompt()
        assert "DO NOT make assumptions about what technologies" in prompt

    def test_returns_consistent_output(self):
        """Test build_chat_system_prompt returns the same output on repeated calls."""
        prompt1 = build_chat_system_prompt()
        prompt2 = build_chat_system_prompt()
        assert prompt1 == prompt2


# ============================================================================
# Cross-prompt comparisons
# ============================================================================


class TestPromptDifferences:
    """Test that RAG and chat prompts have the correct structural differences."""

    def test_both_prompts_include_context_types(self):
        """Test both prompts embed the same CONTEXT_TYPES content."""
        rag = build_rag_system_prompt()
        chat = build_chat_system_prompt()
        assert CONTEXT_TYPES in rag
        assert CONTEXT_TYPES in chat

    def test_both_prompts_include_requirement_strength_rules(self):
        """Test both prompts embed the REQUIREMENT_STRENGTH_RULES content."""
        rag = build_rag_system_prompt()
        chat = build_chat_system_prompt()
        assert REQUIREMENT_STRENGTH_RULES in rag
        assert REQUIREMENT_STRENGTH_RULES in chat

    def test_only_chat_includes_formatting_rules(self):
        """Test FORMATTING_RULES appears only in chat prompt, not RAG prompt."""
        rag = build_rag_system_prompt()
        chat = build_chat_system_prompt()
        assert FORMATTING_RULES in chat
        assert FORMATTING_RULES not in rag

    def test_only_chat_includes_conversation_memory(self):
        """Test conversation memory mention appears only in chat prompt."""
        rag = build_rag_system_prompt()
        chat = build_chat_system_prompt()
        assert "conversation memory" in chat
        assert "conversation memory" not in rag

    def test_only_chat_includes_no_source_listing(self):
        """Test 'DO NOT list sources' appears only in chat prompt."""
        rag = build_rag_system_prompt()
        chat = build_chat_system_prompt()
        assert "DO NOT list sources" in chat
        assert "DO NOT list sources" not in rag

    def test_only_rag_includes_citation_instruction(self):
        """Test RAG prompt instructs citation, chat prompt does not."""
        rag = build_rag_system_prompt()
        chat = build_chat_system_prompt()
        assert "Cite the specific sources" in rag
        assert "Cite the specific sources" not in chat

    def test_prompts_are_different(self):
        """Test RAG and chat prompts are not identical."""
        rag = build_rag_system_prompt()
        chat = build_chat_system_prompt()
        assert rag != chat


# ============================================================================
# Module-level checks
# ============================================================================


class TestModuleStructure:
    """Test that the module exports the expected public interface."""

    def test_module_has_context_types(self):
        """Test module defines CONTEXT_TYPES constant."""
        assert hasattr(prompts_module, "CONTEXT_TYPES")

    def test_module_has_formatting_rules(self):
        """Test module defines FORMATTING_RULES constant."""
        assert hasattr(prompts_module, "FORMATTING_RULES")

    def test_module_has_requirement_strength_rules(self):
        """Test module defines REQUIREMENT_STRENGTH_RULES constant."""
        assert hasattr(prompts_module, "REQUIREMENT_STRENGTH_RULES")

    def test_module_has_build_rag_system_prompt(self):
        """Test module defines build_rag_system_prompt function."""
        assert hasattr(prompts_module, "build_rag_system_prompt")
        assert callable(prompts_module.build_rag_system_prompt)

    def test_module_has_build_chat_system_prompt(self):
        """Test module defines build_chat_system_prompt function."""
        assert hasattr(prompts_module, "build_chat_system_prompt")
        assert callable(prompts_module.build_chat_system_prompt)

    def test_module_has_docstring(self):
        """Test module has a docstring."""
        assert prompts_module.__doc__ is not None
        assert len(prompts_module.__doc__.strip()) > 0

    def test_build_rag_system_prompt_has_docstring(self):
        """Test build_rag_system_prompt has a docstring."""
        assert build_rag_system_prompt.__doc__ is not None
        assert len(build_rag_system_prompt.__doc__.strip()) > 0

    def test_build_chat_system_prompt_has_docstring(self):
        """Test build_chat_system_prompt has a docstring."""
        assert build_chat_system_prompt.__doc__ is not None
        assert len(build_chat_system_prompt.__doc__.strip()) > 0

    def test_build_rag_system_prompt_takes_no_args(self):
        """Test build_rag_system_prompt requires no arguments."""
        import inspect
        sig = inspect.signature(build_rag_system_prompt)
        assert len(sig.parameters) == 0

    def test_build_chat_system_prompt_takes_no_args(self):
        """Test build_chat_system_prompt requires no arguments."""
        import inspect
        sig = inspect.signature(build_chat_system_prompt)
        assert len(sig.parameters) == 0

    def test_build_rag_system_prompt_return_annotation(self):
        """Test build_rag_system_prompt has str return type annotation."""
        import inspect
        sig = inspect.signature(build_rag_system_prompt)
        assert sig.return_annotation is str

    def test_build_chat_system_prompt_return_annotation(self):
        """Test build_chat_system_prompt has str return type annotation."""
        import inspect
        sig = inspect.signature(build_chat_system_prompt)
        assert sig.return_annotation is str
