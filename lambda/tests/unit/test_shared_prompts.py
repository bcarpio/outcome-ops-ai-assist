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


class TestLineCitationGroundingRules:
    """Chat synthesis must use find_symbol_at_line for any
    "<function_name> (file:line)" citation; the bound rule lives in
    LINE_CITATION_GROUNDING_RULES (ADR-031). Also enforces the
    anti-aggregate rule that closes the fabricated-headline-count
    failure mode (2026-06-02 require_auth eval cited "65" while
    enumeration summed to 37 and reality was 51).
    """

    def test_constant_exists_and_names_the_tool(self):
        from shared.prompts import LINE_CITATION_GROUNDING_RULES

        assert "find_symbol_at_line" in LINE_CITATION_GROUNDING_RULES

    def test_constant_names_target_symbol_id_field(self):
        """Synthesis must use the returned target_symbol_id as the
        cited name, not invent one."""
        from shared.prompts import LINE_CITATION_GROUNDING_RULES

        assert "target_symbol_id" in LINE_CITATION_GROUNDING_RULES

    def test_constant_handles_null_result_by_dropping_name_claim(self):
        """When the tool returns null (line not enclosed by any
        defines), synthesis must cite only file:line -- not invent a
        function name."""
        from shared.prompts import LINE_CITATION_GROUNDING_RULES

        assert "null" in LINE_CITATION_GROUNDING_RULES
        # Must explicitly forbid guessing
        body = LINE_CITATION_GROUNDING_RULES.lower()
        assert "not invent" in body or "do not invent" in body \
            or "do not guess" in body or "not guess" in body

    def test_constant_forbids_inference_from_surrounding_text(self):
        """The class of error this rule closes is the model patching
        the enclosing name from memory of the file. Must explicitly
        forbid inference."""
        from shared.prompts import LINE_CITATION_GROUNDING_RULES

        body = LINE_CITATION_GROUNDING_RULES.lower()
        assert "do not infer" in body or "trust the graph" in body

    def test_anti_aggregate_subrule_forbids_llm_computed_counts(self):
        """The anti-aggregate sub-rule (item 6) forbids LLM-computed
        totals across enumerated items. Without it, the model can
        invent a headline number divorced from the data it cited."""
        from shared.prompts import LINE_CITATION_GROUNDING_RULES

        body = LINE_CITATION_GROUNDING_RULES.lower()
        # The rule must mention BOTH the prohibition (counts/aggregates)
        # AND the alternative (tool-returned count / len(rows)).
        assert (
            "count" in body or "aggregate" in body or "total" in body
        )
        assert (
            "len(rows)" in LINE_CITATION_GROUNDING_RULES
            or "tool" in body
        )

    def test_chat_prompt_includes_line_citation_block(self):
        """build_chat_system_prompt must compose in the line-citation
        rules so the chat synthesis path actually receives them."""
        from shared.prompts import (
            build_chat_system_prompt,
            LINE_CITATION_GROUNDING_RULES,
        )

        prompt = build_chat_system_prompt()
        assert LINE_CITATION_GROUNDING_RULES in prompt

    def test_line_citation_block_appears_after_read_source(self):
        """Reading order: VALUE_GROUNDING tells the model what an
        unsafe value claim looks like; READ_SOURCE tells it how to
        verify the value; LINE_CITATION tells it how to ground the
        enclosing symbol. The progression Value -> Read -> Locate is
        intentional."""
        from shared.prompts import (
            build_chat_system_prompt,
            READ_SOURCE_VERIFICATION_RULES,
            LINE_CITATION_GROUNDING_RULES,
        )

        prompt = build_chat_system_prompt()
        i_rs = prompt.find(READ_SOURCE_VERIFICATION_RULES)
        i_lc = prompt.find(LINE_CITATION_GROUNDING_RULES)
        assert i_rs != -1 and i_lc != -1
        assert i_rs < i_lc


class TestListCompletenessRules:
    """Chat synthesis must honor `complete: false` on every list-returning
    tool response: surface "showing N of K" or refuse the enumeration
    rather than ship a confidently-incomplete answer (ADR-032).
    """

    def test_constant_exists_and_names_the_contract_fields(self):
        from shared.prompts import LIST_COMPLETENESS_RULES

        assert "complete" in LIST_COMPLETENESS_RULES
        assert "total_known" in LIST_COMPLETENESS_RULES

    def test_constant_names_the_three_required_responses(self):
        """On complete:false the model has three legal moves:
        re-call with higher limit, disclose 'showing N of K', or
        refuse the enumeration. The rule must spell all three out."""
        from shared.prompts import LIST_COMPLETENESS_RULES

        body = LIST_COMPLETENESS_RULES.lower()
        assert "higher" in body or "call the tool again" in body
        assert "showing n of" in body or "showing" in body
        assert "refuse" in body

    def test_constant_forbids_quoting_len_items_when_truncated(self):
        """The deterministic count is total_known; len(items) on a
        partial result is the silent-aggregation trap."""
        from shared.prompts import LIST_COMPLETENESS_RULES

        assert "total_known" in LIST_COMPLETENESS_RULES
        body = LIST_COMPLETENESS_RULES.lower()
        assert "len(items)" in body or "never" in body

    def test_constant_references_the_augment_note_pattern(self):
        """When the chat router clips the canonical set it prepends a
        NOTE to the symbol facts block. The prompt rule must treat
        that NOTE as complete:false so the LLM doesn't strip it."""
        from shared.prompts import LIST_COMPLETENESS_RULES

        assert "NOTE" in LIST_COMPLETENESS_RULES
        body = LIST_COMPLETENESS_RULES.lower()
        assert "do not strip" in body or "treat as" in body or \
            "do not" in body

    def test_constant_carries_the_require_auth_failure_example(self):
        """A concrete WRONG example pinned in the rule so future
        prompt-tightening reviewers know what they're guarding against.
        The 2026-06-02 require_auth eval claimed 65 sites; reality 51."""
        from shared.prompts import LIST_COMPLETENESS_RULES

        assert "require_auth" in LIST_COMPLETENESS_RULES
        assert "WRONG" in LIST_COMPLETENESS_RULES or "wrong" in LIST_COMPLETENESS_RULES

    def test_chat_prompt_includes_list_completeness_block(self):
        from shared.prompts import (
            build_chat_system_prompt,
            LIST_COMPLETENESS_RULES,
        )

        prompt = build_chat_system_prompt()
        assert LIST_COMPLETENESS_RULES in prompt

    def test_list_completeness_appears_after_line_citation(self):
        """Reading order: VALUE_GROUNDING -> READ_SOURCE -> LINE_CITATION
        -> LIST_COMPLETENESS. Each rule layer builds on the previous:
        value -> verify-value -> locate-symbol -> list-completeness.
        """
        from shared.prompts import (
            build_chat_system_prompt,
            LINE_CITATION_GROUNDING_RULES,
            LIST_COMPLETENESS_RULES,
        )

        prompt = build_chat_system_prompt()
        i_lc = prompt.find(LINE_CITATION_GROUNDING_RULES)
        i_lcomplete = prompt.find(LIST_COMPLETENESS_RULES)
        assert i_lc != -1 and i_lcomplete != -1
        assert i_lc < i_lcomplete


class TestListCompletenessIntroAggregateBan:
    """The 2026-06-02 require_auth audit: chat answer led with
    'Based on the symbol graph, 40 distinct definitions ... 94 direct
    call sites total.' against ground truth 24 / 51. The numbers
    came from the renderer (CANONICAL count + retrieval total) but
    were authored into prose by the model. Both halves are closed:
    the renderer no longer emits the aggregate summary line; the
    prompt rule explicitly forbids the model from inventing one.
    """

    def test_intro_aggregate_summary_explicitly_forbidden(self):
        from shared.prompts import LIST_COMPLETENESS_RULES

        body = LIST_COMPLETENESS_RULES.lower()
        # 2026-06-03 follow-up: widened from "intro only" to "anywhere
        # in an answer containing a list/table". Intro is now one
        # named position among several (header / footer / inline /
        # table caption). The rule must name multiple positions so a
        # future move (next whack-a-mole instance) is closed too.
        assert "intro" in body or "header" in body or "footer" in body
        assert "anywhere" in body or "twice" in body or \
            "this rule has surfaced" in body

    def test_intro_aggregate_rule_pins_the_failure_case(self):
        """The require_auth wrong numbers (40 / 94 vs ground truth
        24 / 51) are documented in the rule so future prompt
        reviewers see what they're guarding against."""
        from shared.prompts import LIST_COMPLETENESS_RULES

        # The failure case numbers appear verbatim so a reviewer
        # editing the rule sees the audit story they would erase.
        assert "40" in LIST_COMPLETENESS_RULES
        assert "94" in LIST_COMPLETENESS_RULES
        assert "24" in LIST_COMPLETENESS_RULES
        assert "51" in LIST_COMPLETENESS_RULES

    def test_intro_clause_without_count_is_explicitly_allowed(self):
        """The rule must not over-correct into 'never introduce the
        answer.' A short clause without a quantified claim is fine
        ('Here are the lambdas that call require_auth:')."""
        from shared.prompts import LIST_COMPLETENESS_RULES

        body = LIST_COMPLETENESS_RULES.lower()
        assert "without a quantified claim" in body or \
            "no count" in body or \
            "without a count" in body


class TestAggregateFooterBanPostMortem:
    """2026-06-03 re-run of the require_auth question put the wrong
    aggregate in the FOOTER ("Total: 24 distinct Lambda functions
    with 56 direct call sites") after we banned the header. Same
    bug class, different location. The rule must forbid model-
    authored aggregates ANYWHERE in an answer that contains a
    list/table, not just at the intro position. The whack-a-mole
    ends because:
      - The renderer emits a labeled COMPUTED_AGGREGATES block
        (sanctioned quote-verbatim source).
      - The prompt rule explicitly bans counts everywhere else.
    """

    def test_rule_bans_aggregates_everywhere_not_just_intro(self):
        from shared.prompts import LIST_COMPLETENESS_RULES

        body = LIST_COMPLETENESS_RULES.lower()
        # Must explicitly name the failure positions: footer, total,
        # across-the-workspace clauses, table captions.
        assert "footer" in body or '"total:' in body or "total:" in body
        # Must name the whack-a-mole dynamic: same bug surfaced again.
        assert "whack" in body or "same bug" in body or \
            "surfaced as the same bug" in body or "twice" in body

    def test_rule_pins_the_56_footer_failure_case(self):
        """The 56 example is in the rule so a future prompt editor
        sees what they would be erasing if they loosen the ban."""
        from shared.prompts import LIST_COMPLETENESS_RULES

        # 56 is the audit-failure footer number; pinned alongside the
        # 40 / 94 / 65 numbers already in the rule from earlier
        # passes.
        assert "56" in LIST_COMPLETENESS_RULES

    def test_rule_names_computed_aggregates_as_sanctioned_source(self):
        """The renderer emits COMPUTED_AGGREGATES in the context; the
        prompt rule must tell the model that block is one of the two
        legal sources for a count (the other being total_known on
        tool responses)."""
        from shared.prompts import LIST_COMPLETENESS_RULES

        assert "COMPUTED_AGGREGATES" in LIST_COMPLETENESS_RULES
        assert "total_known" in LIST_COMPLETENESS_RULES

    def test_rule_explicitly_bans_filtered_subset_counts(self):
        """The 56 came from the model applying a heuristic filter
        (production-only, excluding tests) and authoring its own
        subset count. The rule must explicitly forbid this --
        heuristic filters yield unverifiable numbers."""
        from shared.prompts import LIST_COMPLETENESS_RULES

        body = LIST_COMPLETENESS_RULES.lower()
        assert "heuristic" in body
        assert "filter" in body or "subset" in body or \
            "prod-only" in body or "production-only" in body

    def test_rule_offers_acceptable_intro_examples(self):
        """The fix isn't 'never introduce the answer.' Non-quantified
        clauses are explicitly allowed so the model doesn't
        over-correct into terse / hostile output."""
        from shared.prompts import LIST_COMPLETENESS_RULES

        body = LIST_COMPLETENESS_RULES.lower()
        assert "acceptable" in body or "without a count" in body


class TestStructuralAttributionRules:
    """ADR-033 structural-attribution rule: when stating WHO calls
    WHOM at WHICH file:line, the claim must be grounded in a code-map
    / handler-group-summary in context, NOT in a doc chunk alone.
    Pinned against the 2026-06-03 magic_link audit failure.
    """

    def test_constant_exists_and_names_code_maps_as_authoritative(self):
        from shared.prompts import STRUCTURAL_ATTRIBUTION_RULES

        body = STRUCTURAL_ATTRIBUTION_RULES.lower()
        assert "code-map" in body or "code map" in body
        assert "authoritative" in body or "structural" in body

    def test_constant_names_specific_code_map_types(self):
        """The rule must name recognizable code-map source types so
        the model can match them in its context (handler-group-summary
        is the load-bearing one for handler-shaped questions)."""
        from shared.prompts import STRUCTURAL_ATTRIBUTION_RULES

        assert "handler-group-summary" in STRUCTURAL_ATTRIBUTION_RULES
        # At least one other recognized code-map type so the rule
        # generalizes beyond Lambda handlers.
        body = STRUCTURAL_ATTRIBUTION_RULES
        assert (
            "architectural-summary" in body
            or "source-module-summary" in body
            or "csharp-project-summary" in body
        )

    def test_constant_pins_the_magic_link_failure_case(self):
        """The 2026-06-03 audit failure (handle_verify vs
        verify_magic_token attribution) is pinned in the rule so a
        future prompt editor sees what they would be erasing."""
        from shared.prompts import STRUCTURAL_ATTRIBUTION_RULES

        assert "handle_verify" in STRUCTURAL_ATTRIBUTION_RULES
        assert "verify_magic_token" in STRUCTURAL_ATTRIBUTION_RULES
        assert "_claim_magic_token_jti" in STRUCTURAL_ATTRIBUTION_RULES
        # Both the wrong line attribution (544) and the right
        # def-line (531) appear so the contrast is unmistakable.
        assert "544" in STRUCTURAL_ATTRIBUTION_RULES
        assert "531" in STRUCTURAL_ATTRIBUTION_RULES

    def test_constant_offers_three_legal_moves_on_doc_only(self):
        """When only a doc carries the structural claim and no
        code-map cross-asserts it, the model has three legal moves:
        qualify with 'doc-derived; not code-map verified', drop the
        attribution, or cite code-map alone. All three must be in
        the rule so the model doesn't over-correct into terse
        refusal."""
        from shared.prompts import STRUCTURAL_ATTRIBUTION_RULES

        body = STRUCTURAL_ATTRIBUTION_RULES.lower()
        # Qualifier move
        assert "doc-derived" in body or "doc derived" in body or \
            "not code-map verified" in body
        # Drop move
        assert "drop the attribution" in body or "cite only the" in body
        # Code-map-alone move is the third option; covered by 'ignore'
        # or 'cite only the code-map'
        assert "ignore the doc" in body or "code-map's view" in body or \
            "cite only the code-map" in body

    def test_constant_scope_excludes_conceptual_claims(self):
        """Behavioral / policy / compliance claims are governed by
        the doc lane (correctly, since docs author them). The rule
        must explicitly scope to structural attribution only, or it
        over-restricts conceptual answers."""
        from shared.prompts import STRUCTURAL_ATTRIBUTION_RULES

        body = STRUCTURAL_ATTRIBUTION_RULES.lower()
        assert "behavioral" in body or "policy" in body or \
            "compliance" in body or "conceptual" in body
        # Must mention examples of non-structural claims that ARE
        # OK for docs to source.
        assert "bcrypt" in body or "encrypt at rest" in body or \
            "magic link" in body.replace("_", " ").lower()

    def test_constant_handles_thin_ingest_workspace(self):
        """When the workspace genuinely has no code-map for the area
        being asked about (thin-ingest customer), the model must
        explicitly disclose rather than silently fall back to docs.
        ADR-033 contract item 1 + this rule item 6."""
        from shared.prompts import STRUCTURAL_ATTRIBUTION_RULES

        body = STRUCTURAL_ATTRIBUTION_RULES.lower()
        assert "thin-ingest" in body or "no code-map" in body or \
            "cannot find a code-map" in body

    def test_code_over_docs_rule_loosens_BOTH_quotable_gate(self):
        """ADR-033 loosens the existing CODE_OVER_DOCS_RULES from
        'ONLY if you can quote BOTH' to 'when you can quote BOTH'.
        Dual-channel retrieval makes both sides reliably available;
        the gate doesn't need to refuse drift findings when both
        sides are present."""
        from shared.prompts import CODE_OVER_DOCS_RULES

        # The "ONLY IF BOTH" hard gate is loosened.
        assert "ONLY if you can quote BOTH" not in CODE_OVER_DOCS_RULES
        # But the rule still says "when you can quote BOTH" -- the
        # drift-finding mechanism is preserved for normal cases.
        assert "BOTH" in CODE_OVER_DOCS_RULES
        # And it now cross-references STRUCTURAL_ATTRIBUTION_RULES so
        # the structural exception is discoverable from the existing
        # rule block.
        assert "STRUCTURAL_ATTRIBUTION_RULES" in CODE_OVER_DOCS_RULES

    def test_chat_prompt_includes_structural_attribution_block(self):
        from shared.prompts import (
            build_chat_system_prompt,
            STRUCTURAL_ATTRIBUTION_RULES,
        )

        prompt = build_chat_system_prompt()
        assert STRUCTURAL_ATTRIBUTION_RULES in prompt

    def test_structural_attribution_appears_after_code_over_docs(self):
        """Reading order: CODE_OVER_DOCS_RULES (precedence rule) then
        STRUCTURAL_ATTRIBUTION_RULES (specific case where precedence
        is binding). Order must be preserved so the model reads the
        general principle before the specific application."""
        from shared.prompts import (
            build_chat_system_prompt,
            CODE_OVER_DOCS_RULES,
            STRUCTURAL_ATTRIBUTION_RULES,
        )

        prompt = build_chat_system_prompt()
        i_code = prompt.find(CODE_OVER_DOCS_RULES)
        i_struct = prompt.find(STRUCTURAL_ATTRIBUTION_RULES)
        assert i_code != -1 and i_struct != -1
        assert i_code < i_struct
