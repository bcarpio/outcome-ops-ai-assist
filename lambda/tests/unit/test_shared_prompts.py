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
CODE_MAP_LITERAL_DISCIPLINE = prompts_module.CODE_MAP_LITERAL_DISCIPLINE
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
        assert "source code" in CONTEXT_TYPES

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
        """Test RAG prompt instructs to say when context is genuinely missing."""
        prompt = build_rag_system_prompt()
        assert "genuinely not present" in prompt

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

    def test_build_rag_system_prompt_has_optional_workspace_prompt(self):
        """Test build_rag_system_prompt accepts optional workspace_prompt parameter."""
        import inspect
        sig = inspect.signature(build_rag_system_prompt)
        assert len(sig.parameters) == 1
        param = sig.parameters["workspace_prompt"]
        assert param.default is None

    def test_build_chat_system_prompt_has_optional_workspace_prompt(self):
        """Test build_chat_system_prompt accepts optional workspace_prompt parameter."""
        import inspect
        sig = inspect.signature(build_chat_system_prompt)
        assert len(sig.parameters) == 1
        param = sig.parameters["workspace_prompt"]
        assert param.default is None

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


class TestWorkspacePromptMerging:
    """Tests for workspace-specific prompt prepending."""

    def test_rag_prompt_without_workspace_prompt(self):
        """Test build_rag_system_prompt returns base prompt when no workspace_prompt."""
        base = build_rag_system_prompt()
        with_none = build_rag_system_prompt(workspace_prompt=None)
        assert base == with_none

    def test_chat_prompt_without_workspace_prompt(self):
        """Test build_chat_system_prompt returns base prompt when no workspace_prompt."""
        base = build_chat_system_prompt()
        with_none = build_chat_system_prompt(workspace_prompt=None)
        assert base == with_none

    def test_rag_prompt_with_workspace_prompt_appends(self):
        """Test build_rag_system_prompt appends workspace instructions after base."""
        workspace_text = "Always recommend our standard Terraform modules."
        result = build_rag_system_prompt(workspace_prompt=workspace_text)
        assert result.startswith("CRITICAL INSTRUCTION")
        assert result.endswith(workspace_text)
        assert "WORKSPACE-SPECIFIC INSTRUCTIONS" in result
        assert workspace_text in result

    def test_chat_prompt_with_workspace_prompt_appends(self):
        """Test build_chat_system_prompt appends workspace instructions after base."""
        workspace_text = "Help draft outbound emails. Use a professional tone."
        result = build_chat_system_prompt(workspace_prompt=workspace_text)
        assert result.startswith("CRITICAL INSTRUCTION")
        assert result.endswith(workspace_text)
        assert "WORKSPACE-SPECIFIC INSTRUCTIONS" in result
        assert workspace_text in result

    def test_workspace_prompt_separator(self):
        """Test that workspace prompt and base prompt are separated by ---."""
        workspace_text = "Security team context."
        result = build_rag_system_prompt(workspace_prompt=workspace_text)
        assert "\n---\n" in result

    def test_empty_string_workspace_prompt_returns_base(self):
        """Test that empty string workspace_prompt returns base prompt (falsy)."""
        base = build_rag_system_prompt()
        with_empty = build_rag_system_prompt(workspace_prompt="")
        assert base == with_empty


class TestSynthesisGuardrails:
    """Synthesis discipline rules present in the chat prompt (ADR-025):
    value-grounding (verbatim quote tied to the construct), the
    constrained code-vs-doc drift rule, and SAST scope."""

    def test_chat_prompt_includes_value_grounding_rule(self):
        prompt = build_chat_system_prompt()
        # Must quote the verbatim text tied to the named construct...
        assert "Quote the exact retrieved text" in prompt
        # ...not attach a neighbor's value (the token_urlsafe scatter)...
        assert "SIMILAR but different construct is NOT evidence" in prompt
        assert "token_urlsafe(8), (16), (32)" in prompt
        # ...and never invent a file/line.
        assert "NEVER invent a file path or line number" in prompt
        assert "(not in retrieved context)" in prompt

    def test_chat_prompt_drift_rule_requires_both_sides_quotable(self):
        prompt = build_chat_system_prompt()
        assert "DOCUMENTATION DRIFT" in prompt
        # A drift finding requires BOTH a code AND a doc source quotable.
        assert "quote BOTH a code/code-map source AND a documentation source" in prompt
        # Single-source values must be used as stated, not "overridden"
        # by an inferred code value — the failure mode that fabricated 32.
        assert "is a fabrication, not a finding" in prompt

    def test_chat_prompt_includes_sast_scope_rule(self):
        prompt = build_chat_system_prompt()
        assert "SAST does NOT cover design-level security" in prompt
        assert "rate limiting" in prompt
        # SAST is one row, never the verdict / exoneration.
        assert "never as the overall verdict" in prompt
        assert "exoneration" in prompt

    def test_chat_prompt_still_carries_base_rules(self):
        # Regression: adding the guardrails must not drop existing rules.
        prompt = build_chat_system_prompt()
        assert "FORMATTING:" in prompt
        assert "RULES:" in prompt


class TestCodeMapLiteralDiscipline:
    """ADR-027 — code-map generation must omit literals/identifiers,
    stack-agnostically (it's appended to every code-map prompt)."""

    def test_discipline_omits_numeric_and_string_literals(self):
        d = CODE_MAP_LITERAL_DISCIPLINE
        assert "DO NOT TRANSCRIBE SPECIFIC LITERAL VALUES" in d
        assert "Numeric literals" in d
        assert "Exact string constants and identifiers" in d
        # The two observed failure classes are covered by category.
        assert "datastore object names" in d  # USED_TOKEN# class
        assert "magic number" in d            # token_urlsafe(32) class

    def test_discipline_is_stack_agnostic(self):
        d = CODE_MAP_LITERAL_DISCIPLINE
        # Not biased to one stack: no AWS-specific nouns; covers SAP-ish
        # and SQL-ish identifiers generically.
        assert "regardless of language, framework, or platform" in d
        assert "transaction / program / module codes" in d
        for aws_noun in ("DynamoDB", "SSM", "token_urlsafe", "PK/SK"):
            assert aws_noun not in d

    def test_discipline_keeps_structural_facts(self):
        d = CODE_MAP_LITERAL_DISCIPLINE
        assert "function / method / procedure / module names" in d
        assert "(see source)" in d
        assert "never invent" in d

    def test_discipline_forbids_placeholder_identifiers(self):
        # A caveated placeholder key (e.g. JTI#{jti}) is still a wrong
        # literal — the regenerated auth code-map showed it leaking.
        d = CODE_MAP_LITERAL_DISCIPLINE
        assert "placeholder, example, or templated form" in d
        assert "A guessed shape is still a wrong literal" in d


class TestReadSourceVerificationRules:
    """plan-read-source-adjudicator chunk 03 -- the system prompt must
    tell the LLM to verify exact code values against source via the
    read_source MCP tool, not only when docs and code-map disagree."""

    def test_constant_exists_and_names_the_tool(self):
        from shared.prompts import READ_SOURCE_VERIFICATION_RULES

        assert "read_source" in READ_SOURCE_VERIFICATION_RULES

    def test_constant_triggers_on_every_value_claim_not_just_drift(self):
        """Without this clause, a fabricated value with no contradicting
        doc would never trip the verification gate and would ship
        silently (the USED_TOKEN# class). Must be explicit."""
        from shared.prompts import READ_SOURCE_VERIFICATION_RULES

        assert "every specific value claim" in READ_SOURCE_VERIFICATION_RULES.lower() \
            or "not only when documentation" in READ_SOURCE_VERIFICATION_RULES

    def test_constant_promotes_at_graph_sha_for_sha_skew(self):
        from shared.prompts import READ_SOURCE_VERIFICATION_RULES

        assert "at_graph_sha" in READ_SOURCE_VERIFICATION_RULES
        assert "SHA-skew" in READ_SOURCE_VERIFICATION_RULES or "graph SHA" in READ_SOURCE_VERIFICATION_RULES

    def test_chat_prompt_includes_read_source_block(self):
        """build_chat_system_prompt must compose in the verification
        rules so the chat synthesis path actually receives them."""
        from shared.prompts import build_chat_system_prompt, READ_SOURCE_VERIFICATION_RULES

        prompt = build_chat_system_prompt()
        assert READ_SOURCE_VERIFICATION_RULES in prompt

    def test_chat_prompt_block_appears_after_value_grounding(self):
        """Reading order matters: VALUE_GROUNDING_RULES tells the model
        what an unsafe value claim looks like; READ_SOURCE then tells
        it how to resolve safely. Block ordering must follow that arc."""
        from shared.prompts import build_chat_system_prompt, VALUE_GROUNDING_RULES, READ_SOURCE_VERIFICATION_RULES

        prompt = build_chat_system_prompt()
        i_vg = prompt.find(VALUE_GROUNDING_RULES)
        i_rs = prompt.find(READ_SOURCE_VERIFICATION_RULES)
        assert i_vg != -1 and i_rs != -1
        assert i_vg < i_rs
