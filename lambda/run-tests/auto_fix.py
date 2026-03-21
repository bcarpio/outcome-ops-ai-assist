"""
Shared auto-fix module for run-tests Lambda and backends.

Provides Claude-based auto-fix functionality that can be used by:
- Lambda handler for direct test execution
- Test backends (Python, Java, etc.) for language-specific fixes

This follows ADR-007's principle: provide the same organizational context
(ADRs, patterns, standards) to auto-fix that generate-code had when creating
the code.
"""

import json
import logging
import os
import re
import sys
from typing import Any, Dict, List, Optional

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

# Add shared module to path for Lambda deployment (container or zip)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from shared.audit import log_audit_entry
except ImportError:
    # Container-based Lambda may not have shared module
    log_audit_entry = None

logger = logging.getLogger(__name__)

# Environment variables
ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")

# Bedrock client for Claude API (auto-fix)
bedrock_config = Config(
    connect_timeout=60,
    read_timeout=300,  # 5 minutes for auto-fix generation
    retries={'max_attempts': 2}
)
bedrock_client = boto3.client("bedrock-runtime", config=bedrock_config)
lambda_client = boto3.client("lambda")

CLAUDE_MODEL_ID = os.environ.get(
    "BEDROCK_ADVANCED_MODEL_ID", "us.anthropic.claude-sonnet-4-6"
)

# Claude Sonnet 4.5 pricing (as of 2025)
SONNET_PRICE_PER_MILLION_INPUT_TOKENS = 3.0
SONNET_PRICE_PER_MILLION_OUTPUT_TOKENS = 15.0

# Module-level accumulator for auto-fix costs
_autofix_input_tokens = 0
_autofix_output_tokens = 0
_autofix_cost_usd = 0.0


def reset_autofix_cost() -> None:
    """Reset the auto-fix cost accumulator."""
    global _autofix_input_tokens, _autofix_output_tokens, _autofix_cost_usd
    _autofix_input_tokens = 0
    _autofix_output_tokens = 0
    _autofix_cost_usd = 0.0


def get_autofix_cost() -> dict:
    """Get the accumulated auto-fix cost."""
    return {
        "input_tokens": _autofix_input_tokens,
        "output_tokens": _autofix_output_tokens,
        "cost_usd": _autofix_cost_usd
    }


def _accumulate_autofix_cost(input_tokens: int, output_tokens: int) -> None:
    """Add to the accumulated auto-fix cost."""
    global _autofix_input_tokens, _autofix_output_tokens, _autofix_cost_usd
    _autofix_input_tokens += input_tokens
    _autofix_output_tokens += output_tokens

    input_cost = (input_tokens / 1_000_000) * SONNET_PRICE_PER_MILLION_INPUT_TOKENS
    output_cost = (output_tokens / 1_000_000) * SONNET_PRICE_PER_MILLION_OUTPUT_TOKENS
    _autofix_cost_usd += round(input_cost + output_cost, 6)


# ============================================================================
# Knowledge Base Query Functions
# ============================================================================


def query_knowledge_base(queries: List[str], top_k: int = 3) -> List[str]:
    """
    Query knowledge base for relevant standards and examples.

    Invokes the query-kb Lambda function with multiple queries in raw mode
    (skips ask-claude synthesis). Returns vector search results directly
    since auto-fix will pass them to Claude anyway.

    Args:
        queries: List of search queries (e.g., ["Python testing import patterns"])
        top_k: Number of top results to return per query (default 3)

    Returns:
        List[str]: List of relevant document excerpts from the knowledge base
    """
    if not queries:
        logger.warning("[kb] No queries provided")
        return []

    logger.info(f"[kb] Querying knowledge base with {len(queries)} queries (raw mode)")

    results = []
    query_kb_function_name = f"{ENV}-{APP_NAME}-query-kb"

    for query in queries:
        try:
            logger.info(f"[kb] Query: {query}")

            payload = {
                "query": query,
                "topK": top_k,
                "raw": True  # Skip ask-claude synthesis - we'll process raw results
            }

            response = lambda_client.invoke(
                FunctionName=query_kb_function_name,
                InvocationType="RequestResponse",
                Payload=json.dumps(payload)
            )

            # Parse response
            response_payload = json.loads(response["Payload"].read())

            # Check for Lambda errors
            if response.get("FunctionError"):
                error_msg = response_payload.get("errorMessage", "Unknown error")
                logger.error(f"[kb] query-kb Lambda error: {error_msg}")
                continue

            # Parse body (query-kb returns API Gateway format)
            if "body" in response_payload:
                body = json.loads(response_payload["body"])

                # Raw mode returns {"results": [...]} with vector search results
                raw_results = body.get("results", [])
                if raw_results:
                    # Format raw results into readable text for Claude
                    formatted = _format_raw_results(query, raw_results)
                    if formatted:
                        results.append(formatted)
            else:
                logger.warning(f"[kb] No body in query-kb response for query: {query}")

        except ClientError as e:
            logger.error(f"[kb] Failed to invoke query-kb Lambda: {e}")
        except Exception as e:
            logger.error(f"[kb] Unexpected error querying KB: {e}")

    logger.info(f"[kb] Retrieved {len(results)} results from knowledge base")
    return results


def _format_raw_results(query: str, raw_results: List[dict]) -> str:
    """
    Format raw vector search results into readable text for Claude.

    Args:
        query: The original query
        raw_results: List of vector search results with score, text, source

    Returns:
        Formatted string with query and results
    """
    if not raw_results:
        return ""

    parts = [f"# Query: {query}\n"]

    for result in raw_results:
        source = result.get("source", "Unknown")
        text = result.get("text", "")
        score = result.get("score", 0.0)

        # Only include results with reasonable relevance
        if score < 0.3:
            continue

        parts.append(f"\n## Source: {source} (relevance: {score:.2f})\n")
        parts.append(text)
        parts.append("\n")

    # Return empty if no relevant results after filtering
    if len(parts) <= 1:
        return ""

    return "".join(parts)


def query_kb_for_fix(
    error_type: str,
    file_path: str,
    backend_type: str = "python",
    config: Optional[Dict[str, Any]] = None
) -> List[str]:
    """
    Query knowledge base for relevant patterns based on error type and file.

    This provides the same organizational context to auto-fix that generate-code
    had when creating the code, following ADR-007's principle that documentation
    should guide behavior at all stages.

    Args:
        error_type: Type of error (import_error, syntax_error, etc.)
        file_path: Path to the file with the error
        backend_type: Language backend (python, java, typescript)
        config: Optional .outcomeops.yaml config dict for project-specific queries

    Returns:
        List[str]: Relevant ADRs, patterns, and standards from knowledge base
    """
    queries = []

    # Add project-specific queries from .outcomeops.yaml if available
    if config:
        code_gen_config = config.get("code_generation", {})
        additional_queries = code_gen_config.get("additional_kb_queries", [])
        if additional_queries:
            logger.info(f"[auto-fix] Adding {len(additional_queries)} queries from .outcomeops.yaml")
            queries.extend(additional_queries)

    # If it's a test file, get testing standards and patterns
    if "test" in file_path.lower():
        queries.extend([
            "test import structure and module resolution",
            "test file structure and best practices",
        ])
        # Language-specific test patterns
        if backend_type == "python":
            queries.extend([
                "Python testing import patterns and conventions",
                "pytest test structure and patterns",
            ])
        elif backend_type == "java":
            queries.extend([
                "Java testing import patterns and conventions",
                "JUnit test structure and best practices",
            ])
        elif backend_type == "typescript":
            queries.extend([
                "TypeScript testing import patterns and conventions",
                "Jest Vitest test structure and best practices",
            ])

    # Add error-type-specific queries
    if error_type == "syntax_error":
        if backend_type == "python":
            queries.append("Python syntax patterns and common errors")
        elif backend_type == "java":
            queries.append("Java syntax patterns and compilation errors")
        elif backend_type == "typescript":
            queries.append("TypeScript syntax patterns and type errors")
    elif error_type == "import_error":
        if backend_type == "python":
            queries.extend([
                "Python import patterns and module structure",
                "sys.path manipulation for test imports",
            ])
        elif backend_type == "java":
            queries.extend([
                "Java import patterns and package structure",
                "Gradle Maven dependency resolution",
            ])
        elif backend_type == "typescript":
            queries.extend([
                "TypeScript import patterns and module resolution",
                "Node.js module path configuration",
            ])

    # Always include general standards for context
    if backend_type == "python":
        queries.append("Python coding standards and conventions")
    elif backend_type == "java":
        queries.append("Java coding standards and conventions")
    elif backend_type == "typescript":
        queries.append("TypeScript coding standards and conventions")

    logger.info(f"[auto-fix] Querying KB for {error_type} in {file_path} (backend: {backend_type})")
    return query_knowledge_base(queries, top_k=3)


# ============================================================================
# Claude Auto-Fix Functions
# ============================================================================


def invoke_claude_for_fix(error_output: str, failure_type: str) -> Optional[str]:
    """
    Invoke Claude to suggest a fix for the error.

    Args:
        error_output: The test error output
        failure_type: Type of failure (import_error or syntax_error)

    Returns:
        Suggested fix as text, or None if Claude fails
    """
    logger.info(f"[auto-fix] Invoking Claude for {failure_type} fix")

    if failure_type == "import_error":
        system_prompt = """You are helping fix a Python import error.
Analyze the error and provide ONLY the specific fix needed.
For missing modules, respond with: "Add missing import: from module import Class"
Be concise and specific."""
    else:  # syntax_error
        system_prompt = """You are helping fix a Python syntax error.
Analyze the error and provide ONLY the specific fix needed.
Be concise and specific about what to change."""

    prompt = f"""Error output:
{error_output[:2000]}

Provide a concise fix for this error."""

    try:
        response = bedrock_client.converse(
            modelId=CLAUDE_MODEL_ID,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            system=[{"text": system_prompt}],
            inferenceConfig={"temperature": 0.3, "maxTokens": 500}
        )

        # Track token usage and cost
        usage = response.get("usage", {})
        input_tokens = usage.get("inputTokens", 0)
        output_tokens = usage.get("outputTokens", 0)
        _accumulate_autofix_cost(input_tokens, output_tokens)
        logger.info(f"[auto-fix] Token usage: {input_tokens} in, {output_tokens} out")

        fix_suggestion = response.get("output", {}).get("message", {}).get("content", [{}])[0].get("text", "")
        logger.info(f"[auto-fix] Claude suggestion: {fix_suggestion[:200]}")

        if log_audit_entry:
            log_audit_entry(
                audit_table_name=os.environ.get("AUDIT_TABLE", ""),
                lambda_name="run-tests",
                model_id=CLAUDE_MODEL_ID,
                input_text=prompt,
                output_text=fix_suggestion,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )

        return fix_suggestion

    except Exception as e:
        logger.error(f"[auto-fix] Claude invocation failed: {e}")
        return None


def apply_fix_to_file(
    file_path: str,
    error_output: str,
    failure_type: str,
    backend_type: str = "python",
    config: Optional[Dict[str, Any]] = None
) -> bool:
    """
    Apply Claude-suggested fix to a file with knowledge base context.

    This follows ADR-007's principle: provide the same organizational context
    (ADRs, patterns, standards) to auto-fix that generate-code had when creating
    the code. This significantly improves fix success rate.

    Args:
        file_path: Path to file to fix
        error_output: The error output
        failure_type: Type of error (import_error or syntax_error)
        backend_type: Language backend (python, java, typescript)
        config: Optional .outcomeops.yaml config dict for project-specific context

    Returns:
        True if fix was applied successfully
    """
    # Map backend type to language name for prompts
    language_names = {
        "python": "Python",
        "java": "Java",
        "typescript": "TypeScript",
    }
    language = language_names.get(backend_type, "Python")

    try:
        # Read the current file content
        with open(file_path, 'r') as f:
            original_content = f.read()

        logger.info(f"[auto-fix] Read {len(original_content)} bytes from {file_path}")

        # Query knowledge base for relevant patterns and standards
        kb_context = query_kb_for_fix(failure_type, file_path, backend_type, config)

        # Build context section for prompt - KB context goes FIRST as authoritative guidance
        kb_context_text = ""
        if kb_context:
            kb_context_text = "# Organizational Standards (MUST FOLLOW):\n\n"
            kb_context_text += "\n\n---\n\n".join(kb_context)
            kb_context_text += "\n\n---\n\n"
            logger.info(f"[auto-fix] Including {len(kb_context)} KB results in context")
        else:
            logger.warning("[auto-fix] No KB context retrieved - proceeding without organizational standards")

        # Get Claude's fix suggestion with full context
        # KB context comes FIRST so it's treated as authoritative, not supplementary
        # Full file content is provided since we ask for complete output
        prompt = f"""{kb_context_text}# Task: Fix this {language} {failure_type.replace('_', ' ')}

You MUST follow the organizational standards above when fixing this error.
The standards contain the correct patterns for imports, module structure, and testing conventions.

## Error:
{error_output[:2000]}

## File to fix: {file_path}

```{backend_type}
{original_content}
```

## Instructions:
1. Analyze the error message carefully
2. Apply the fix using ONLY the patterns from the organizational standards above
3. Do NOT invent new patterns - use exactly what the standards specify
4. Output the COMPLETE fixed file

Output ONLY the fixed {language} code, no explanations or markdown fences."""

        system_prompt = f"""You are fixing a {language} {failure_type.replace('_', ' ')}.

CRITICAL: You have been provided with organizational standards and ADRs that define the CORRECT patterns.
You MUST use those patterns exactly - do not invent alternatives.

For import/module errors: The standards show the exact import and module resolution patterns to use.
For syntax errors: Fix the syntax while preserving the file structure.

Output the COMPLETE corrected file using the organizational patterns.
Output ONLY valid {language} code - no markdown, no explanations."""

        response = bedrock_client.converse(
            modelId=CLAUDE_MODEL_ID,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            system=[{"text": system_prompt}],
            inferenceConfig={"temperature": 0.2, "maxTokens": 8000}
        )

        # Track token usage and cost
        usage = response.get("usage", {})
        input_tokens = usage.get("inputTokens", 0)
        output_tokens = usage.get("outputTokens", 0)
        _accumulate_autofix_cost(input_tokens, output_tokens)
        logger.info(f"[auto-fix] Token usage for file fix: {input_tokens} in, {output_tokens} out")

        fixed_content = response.get("output", {}).get("message", {}).get("content", [{}])[0].get("text", "")

        if log_audit_entry:
            log_audit_entry(
                audit_table_name=os.environ.get("AUDIT_TABLE", ""),
                lambda_name="run-tests",
                model_id=CLAUDE_MODEL_ID,
                input_text=prompt,
                output_text=fixed_content,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )

        if not fixed_content or len(fixed_content) < 10:
            logger.warning("[auto-fix] Claude returned empty or very short content")
            return False

        # Remove markdown code fences if present (handle all supported languages)
        fixed_content = re.sub(r'^```(?:python|java|typescript|ts|javascript|js)\s*\n', '', fixed_content)
        fixed_content = re.sub(r'\n```\s*$', '', fixed_content)
        fixed_content = fixed_content.strip()

        # Write the fixed content
        with open(file_path, 'w') as f:
            f.write(fixed_content)

        logger.info(f"[auto-fix] Applied fix to {file_path}")
        return True

    except Exception as e:
        logger.error(f"[auto-fix] Failed to apply fix: {e}")
        return False


def parse_error_file_path(error_output: str) -> Optional[str]:
    """
    Parse error output to find the problematic file path.

    Handles multiple formats:
    - Python traceback: File "/path/to/file.py", line X
    - Pytest collection error: path/to/test_file.py:13: in <module>
    - Pytest import error: ImportError while importing test module '/path/to/test_file.py'

    Args:
        error_output: The pytest error output

    Returns:
        File path if found, None otherwise
    """
    # Format 1: Python traceback - File "/path/to/file.py", line X
    file_match = re.search(r'File "([^"]+\.py)", line (\d+)', error_output)
    if file_match:
        return file_match.group(1)

    # Format 2: Pytest collection error - path/to/test_file.py:13: in <module>
    pytest_match = re.search(r'([^\s]+\.py):\d+: in <module>', error_output)
    if pytest_match:
        return pytest_match.group(1)

    # Format 3: Pytest import path in error message
    # ImportError while importing test module '/tmp/.../test_file.py'
    import_match = re.search(r"importing test module '([^']+\.py)'", error_output)
    if import_match:
        return import_match.group(1)

    return None
