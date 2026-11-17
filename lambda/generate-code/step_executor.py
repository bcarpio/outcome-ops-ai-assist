# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Step execution flow.

Handles executing individual code generation steps from SQS messages.
Uses pluggable backends for language-specific code generation and validation.
"""

import base64
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import boto3
from botocore.exceptions import ClientError

# Make shared/ importable; the deploy package includes lambda/shared/
# at zip root via terraform source_path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.audit_writer import ACTION_CODE_GENERATED, log_admin_action  # noqa: E402

from codegen_backends import CodeGeneratorBackend, get_backend
from bedrock_client import invoke_claude
from github_api import (
    get_github_token,
    get_file,
    commit_file,
    update_file,
    create_pull_request,
    post_pr_comment,
    get_branch_head_sha
)
from knowledge_base import get_step_context, query_knowledge_base
from models import (
    StepExecutionMessage,
    ExecutionPlan,
    PlanStep,
    GeneratedFile
)
from plan_manager import parse_plan_from_markdown, update_step_in_plan, serialize_plan_to_markdown
from sqs_client import send_step_message
from utils import calculate_cost, generate_branch_name, resolve_owning_workspace

logger = logging.getLogger()

events_client = boto3.client("events")
EVENT_BUS_NAME = os.environ.get("EVENT_BUS_NAME", "default")
ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")
ENABLE_BEDROCK_COOLDOWN = os.environ.get("ENABLE_BEDROCK_COOLDOWN", "true").lower() == "true"

# Regex pattern for GitHub tokens (PAT and fine-grained tokens)
TOKEN_PATTERN = re.compile(r'(github_pat_[A-Za-z0-9_]{22,}|ghp_[A-Za-z0-9]{36,}|x-access-token:[^@\s]+)')

# Delimiter pattern for file extraction (===FILE: path===)
FILE_DELIMITER_PATTERN = re.compile(r'^===FILE:\s*(.+?)\s*===$', re.MULTILINE)


# ============================================================================
# Delimiter-Based File Parsing
# ============================================================================


def _github_host() -> str:
    """GHES-aware git host for clone URLs (shared.github_base)."""
    from shared.github_base import github_host
    return github_host()


def parse_json_fallback(response_text: str) -> List[GeneratedFile]:
    """
    Fallback parser for when Claude returns JSON instead of delimiters.

    Handles responses like:
        ```json
        {"files": [{"path": "...", "content": "..."}]}
        ```

    Args:
        response_text: Raw response from Claude

    Returns:
        List of GeneratedFile objects

    Raises:
        ValueError: If JSON parsing fails or format is unexpected
    """
    logger.warning("[parse] Attempting JSON fallback parsing")

    # Strip markdown code fences if present
    text = response_text.strip()
    if text.startswith("```"):
        # Remove opening fence (```json or ```)
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1:]
        # Remove closing fence
        if text.endswith("```"):
            text = text[:-3].strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON fallback parsing failed: {e}")

    # Handle {"files": [...]} format
    if isinstance(data, dict) and "files" in data:
        files_data = data["files"]
    elif isinstance(data, list):
        files_data = data
    else:
        raise ValueError(f"Unexpected JSON structure: {type(data)}")

    files = []
    for item in files_data:
        if not isinstance(item, dict):
            continue
        path = item.get("path", "")
        content = item.get("content", "")
        if path and content:
            logger.info(f"[parse] JSON fallback extracted: {path} ({len(content)} chars)")
            files.append(GeneratedFile(path=path, content=content))

    if not files:
        raise ValueError("No files extracted from JSON fallback")

    logger.info(f"[parse] JSON fallback successfully extracted {len(files)} file(s)")
    return files


def parse_delimited_files(response_text: str) -> List[GeneratedFile]:
    """
    Parse Claude's delimiter-based output format into GeneratedFile objects.

    Format:
        ===FILE: path/to/file.py===
        <raw file content>
        ===FILE: path/to/next_file.py===
        <next file content>
        ===END===

    Falls back to JSON parsing if delimiters are not found (Claude occasionally
    returns JSON format despite being asked for delimiters).

    Args:
        response_text: Raw response from Claude

    Returns:
        List of GeneratedFile objects

    Raises:
        ValueError: If no files found or format is invalid
    """
    logger.info(f"[parse] Parsing delimited response ({len(response_text)} chars)")

    # Find all file delimiters
    matches = list(FILE_DELIMITER_PATTERN.finditer(response_text))

    if not matches:
        # Log the response for debugging
        logger.error(f"[parse] No file delimiters found in response")
        logger.error(f"[parse] Response preview: {response_text[:500]}")

        # Try JSON fallback if response looks like JSON
        if response_text.strip().startswith(("{", "[", "```")):
            try:
                return parse_json_fallback(response_text)
            except ValueError as e:
                logger.error(f"[parse] JSON fallback also failed: {e}")

        raise ValueError("No file delimiters (===FILE: path===) found in Claude response")

    logger.info(f"[parse] Found {len(matches)} file delimiter(s)")

    files = []
    for i, match in enumerate(matches):
        file_path = match.group(1).strip()
        start_pos = match.end()

        # Find end position (next delimiter or ===END=== or end of text)
        if i + 1 < len(matches):
            end_pos = matches[i + 1].start()
        else:
            # Look for ===END=== marker
            end_marker = response_text.find('===END===', start_pos)
            if end_marker != -1:
                end_pos = end_marker
            else:
                end_pos = len(response_text)

        # Extract content and strip leading/trailing whitespace
        content = response_text[start_pos:end_pos].strip()

        # Remove any trailing ===END=== that might be included
        if content.endswith('===END==='):
            content = content[:-9].strip()

        logger.info(f"[parse] Extracted file: {file_path} ({len(content)} chars)")

        # Create GeneratedFile with raw content (no base64/JSON encoding needed)
        files.append(GeneratedFile(path=file_path, content=content))

    if not files:
        raise ValueError("No files extracted from delimited response")

    return files


def sanitize_for_logging(text: Union[str, List[str], None]) -> str:
    """
    Sanitize text for logging by redacting sensitive tokens.

    Handles strings, lists (command args), and None values.
    Redacts GitHub PAT tokens and access tokens from URLs.
    """
    if text is None:
        return ""
    if isinstance(text, list):
        text = " ".join(str(arg) for arg in text)
    return TOKEN_PATTERN.sub("[REDACTED]", str(text))


# ============================================================================
# Terraform Formatting Functions
# ============================================================================


def format_terraform_files(
    repo_full_name: str,
    branch_name: str,
    terraform_files: List[str],
    github_token: str,
    step_number: int
) -> None:
    """
    Format Terraform files using terraform fmt and commit changes.

    Args:
        repo_full_name: Full repository name (owner/repo)
        branch_name: Branch to format files on
        terraform_files: List of .tf file paths to format
        github_token: GitHub API token
        step_number: Current step number for commit message
    """
    logger.info(f"[terraform-fmt] Formatting {len(terraform_files)} Terraform files")

    try:
        # Create temp directory for repo
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            repo_url = f"https://x-access-token:{github_token}@{_github_host()}/{repo_full_name}.git"

            # Clone repo and checkout branch
            subprocess.run(
                ["git", "clone", "--depth=1", "--branch", branch_name, repo_url, str(repo_dir)],
                check=True,
                capture_output=True,
                text=True,
                timeout=60
            )

            # Run terraform fmt on each file
            formatted_files = []
            for tf_file in terraform_files:
                tf_path = repo_dir / tf_file
                if not tf_path.exists():
                    logger.warning(f"[terraform-fmt] File not found: {tf_file}")
                    continue

                result = subprocess.run(
                    ["terraform", "fmt", str(tf_path)],
                    cwd=repo_dir,
                    capture_output=True,
                    text=True,
                    timeout=30
                )

                if result.returncode == 0:
                    # Check if file was modified
                    git_status = subprocess.run(
                        ["git", "status", "--porcelain", str(tf_file)],
                        cwd=repo_dir,
                        capture_output=True,
                        text=True
                    )

                    if git_status.stdout.strip():
                        formatted_files.append(tf_file)
                        logger.info(f"[terraform-fmt] Formatted {tf_file}")

            # If files were formatted, commit and push
            if formatted_files:
                subprocess.run(
                    ["git", "config", "user.name", "OutcomeOps Bot"],
                    cwd=repo_dir,
                    check=True
                )
                subprocess.run(
                    ["git", "config", "user.email", "bot@outcomeops.ai"],
                    cwd=repo_dir,
                    check=True
                )

                for tf_file in formatted_files:
                    subprocess.run(
                        ["git", "add", tf_file],
                        cwd=repo_dir,
                        check=True
                    )

                commit_msg = f"style: terraform fmt for step {step_number}\n\nAutomatically formatted {len(formatted_files)} Terraform file(s)"
                subprocess.run(
                    ["git", "commit", "-m", commit_msg],
                    cwd=repo_dir,
                    check=True,
                    capture_output=True,
                    text=True
                )

                subprocess.run(
                    ["git", "push"],
                    cwd=repo_dir,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=60
                )

                logger.info(f"[terraform-fmt] Committed and pushed {len(formatted_files)} formatted files")
            else:
                logger.info("[terraform-fmt] No files needed formatting")

    except subprocess.TimeoutExpired:
        logger.error("[terraform-fmt] Terraform fmt timed out")
    except subprocess.CalledProcessError as e:
        logger.error(f"[terraform-fmt] Command failed: {sanitize_for_logging(e.cmd)}, stderr: {sanitize_for_logging(e.stderr)}")
    except Exception as e:
        logger.error(f"[terraform-fmt] Unexpected error: {e}", exc_info=True)


# ============================================================================
# Validation Functions
# ============================================================================


def validate_requirements_file(file_path: str, content: str) -> Optional[str]:
    """
    Validate Python requirements.txt file using pip.

    Per ADR-006, all requirements.txt files must be validated before commit
    to prevent hallucinated or non-existent packages.

    DEPRECATED: Use backend.validate_file() instead for language-agnostic validation.

    Args:
        file_path: Path to the requirements file (for logging)
        content: Content of the requirements.txt file

    Returns:
        None if valid, error message string if invalid
    """
    # Only validate requirements.txt files
    if not file_path.endswith("requirements.txt"):
        return None

    logger.info(f"[validation] Validating {file_path} per ADR-006")

    # Create temporary file with requirements content
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as tmp_file:
        tmp_file.write(content)
        tmp_path = tmp_file.name

    try:
        # Run pip install --dry-run to validate
        result = subprocess.run(
            ['pip', 'install', '--dry-run', '-r', tmp_path],
            capture_output=True,
            text=True,
            timeout=30
        )

        if result.returncode != 0:
            # Extract meaningful error, filtering out harmless warnings
            error_lines = result.stderr.strip().split('\n')

            # Filter out cache directory warnings (common in Lambda sandbox)
            filtered_lines = [
                line for line in error_lines
                if not any(ignore in line for ignore in [
                    "cache has been disabled",
                    "not owned or is not writable",
                    "WARNING: The directory",
                ])
            ]

            # Check for actual package errors
            package_error = None
            for line in filtered_lines:
                if 'Could not find a version' in line or 'No matching distribution' in line:
                    package_error = line
                    break

            # If we found a package error, that's a real failure
            if package_error:
                error_msg = f"requirements.txt validation failed: {package_error}"
                logger.error(f"[validation] {error_msg}")
                return error_msg

            # If only cache warnings remain, treat as success
            if not filtered_lines:
                logger.info(f"[validation] {file_path} passed validation (ignoring cache warnings)")
                return None

            # Other errors - report them
            error_msg = f"requirements.txt validation failed: {result.stderr[:200]}"
            logger.error(f"[validation] {error_msg}")
            return error_msg

        logger.info(f"[validation] {file_path} passed validation")
        return None

    except subprocess.TimeoutExpired:
        error_msg = "requirements.txt validation timed out after 30s"
        logger.error(f"[validation] {error_msg}")
        return error_msg
    except Exception as e:
        error_msg = f"requirements.txt validation error: {str(e)}"
        logger.error(f"[validation] {error_msg}")
        return error_msg
    finally:
        # Clean up temp file
        try:
            Path(tmp_path).unlink()
        except Exception:
            pass


def validate_files_with_backend(
    files: List[Dict[str, str]],
    backend: CodeGeneratorBackend,
) -> List[str]:
    """
    Validate generated files using the backend's validation logic.

    Args:
        files: List of dicts with 'path' and 'content' keys
        backend: Code generator backend

    Returns:
        List of validation error messages (empty if all valid)
    """
    logger.info(f"[validation] Validating {len(files)} files with {backend.name} backend")

    validation_results = backend.validate_files(files)
    errors = []

    for result in validation_results:
        if not result.valid:
            errors.append(f"{result.file_path}: {result.error_message}")

    if errors:
        logger.error(f"[validation] {len(errors)} validation errors")
    else:
        logger.info("[validation] All files passed validation")

    return errors


# ============================================================================
# Code Generation Prompt
# ============================================================================


CODE_GENERATION_SYSTEM_PROMPT = """You are a senior software engineer implementing code based on a step-by-step plan.

Your task is to generate production-ready code for the files specified in the current step.

CRITICAL REQUIREMENTS:
1. Generate complete, working code that follows the standards provided
2. Return ONLY valid JSON in this exact format:

{
  "files": [
    {
      "path": "path/to/file.py",
      "content": "# Your code here\\nimport json\\n..."
    }
  ]
}

IMPORTANT - JSON STRING ESCAPING:
- The content field is a JSON string - you MUST escape special characters
- Escape newlines as \\n (not literal newlines)
- Escape double quotes as \\"
- Escape backslashes as \\\\
- Example: "content": "def hello():\\n    return \\"world\\""

VERBOSITY: Generate CONCISE, production-ready code.
- Docstrings: 1-2 lines max, describe purpose only
- Comments: Only for non-obvious logic (explain "why", not "what")
- NO inline tutorials, examples in comments, or alternative approaches
- NO line-by-line explanations of standard library or framework usage
- Code should be self-documenting through clear naming and type hints

For Terraform/IaC:
- NO policy explanation comments (resource names should be self-documenting)
- Use locals for repeated values, not repeated comments

For Tests:
- 1 line docstrings max per test function
- Minimal fixtures - only what's needed
- Test names should be descriptive enough without comments

For Python:
- Type hints on all function signatures
- Brief module docstring (1-2 lines)
- No "# This function does X" comments"""


def _extract_error_context(content: str, error_msg: str) -> str:
    """
    Extract context around the error line from content.

    Parses the error message to find line numbers and shows surrounding lines.
    """
    import re

    # Try to extract line number from error message
    line_match = re.search(r'line (\d+)', error_msg)
    if not line_match:
        return ""

    error_line = int(line_match.group(1))
    lines = content.split('\n')

    if error_line <= 0 or error_line > len(lines):
        return ""

    # Show 3 lines before and after the error
    start = max(0, error_line - 4)
    end = min(len(lines), error_line + 3)

    context_parts = ["\n--- Error context (lines around the error) ---"]
    for i in range(start, end):
        line_num = i + 1
        marker = ">>> " if line_num == error_line else "    "
        context_parts.append(f"{marker}{line_num}: {lines[i]}")
    context_parts.append("--- End error context ---\n")

    return '\n'.join(context_parts)


def build_syntax_fix_prompt(validation_error: str, previous_code: str) -> str:
    """
    Build a minimal prompt for fixing syntax errors only.

    This is used on retry attempts to avoid regenerating code from scratch.
    The prompt ONLY contains the error and the code to fix, nothing else.
    Extracts and highlights the problematic line(s) for better debugging.
    """
    # Extract error context for each file in previous_code
    # The format is: ## path/to/file.py\n```python\n...\n```
    import re

    error_context = ""
    # Find file blocks and extract context from each
    file_blocks = re.split(r'^## ', previous_code, flags=re.MULTILINE)
    for block in file_blocks:
        if not block.strip():
            continue
        # Extract content between code fences
        code_match = re.search(r'```\w*\n(.*?)```', block, re.DOTALL)
        if code_match:
            code_content = code_match.group(1)
            context = _extract_error_context(code_content, validation_error)
            if context:
                error_context = context
                logger.info(f"[step-exec] Error context extracted:{context}")
                break

    if not error_context:
        logger.warning(f"[step-exec] Could not extract error context from code")

    return f"""Fix the syntax error and return ONLY the JSON response. No explanation.

Error: {validation_error}
{error_context}
IMPORTANT: The error is likely caused by:
- An unterminated string (missing closing quote)
- A string containing an unescaped backslash (use raw strings r"..." or escape \\\\)
- A multiline string not properly closed with triple quotes
- In JSON, you must escape: newlines as \\n, quotes as \\", backslashes as \\\\

Code to fix:
{previous_code}

Respond with ONLY this JSON (no text before or after):
```json
{{
  "files": [
    {{
      "path": "path/to/file.py",
      "content": "# Fixed code here\\nimport json\\n..."
    }}
  ]
}}
```

REMEMBER: The content field is a JSON string. Escape newlines as \\n, double quotes as \\", and backslashes as \\\\."""


def build_code_generation_prompt(
    step: PlanStep,
    issue_description: str,
    current_step: int,
    total_steps: int,
    kb_results: List[str],
    lambda_standards: List[str] = None,
    terraform_standards: List[str] = None,
    testing_standards: List[str] = None,
    code_style_standards: List[str] = None,
    existing_files: Dict[str, str] = None,
    previous_step_files: Dict[str, str] = None,
    truncation_recovery: bool = False
) -> str:
    """
    Build the prompt for code generation.

    Args:
        step: Current step to execute
        issue_description: Original issue description
        current_step: Current step number
        total_steps: Total number of steps
        kb_results: Knowledge base query results (step-specific)
        lambda_standards: Cached Lambda standards from plan
        terraform_standards: Cached Terraform standards from plan
        testing_standards: Cached Testing standards from plan
        code_style_standards: Cached code style/verbosity standards from plan
        existing_files: Dict of file_path -> content for files that already exist
        previous_step_files: Dict of file_path -> content for files created in previous steps
        truncation_recovery: If True, adds instructions to generate minimal code

    Returns:
        str: Complete prompt for Claude
    """
    # Combine cached standards with step-specific KB results
    all_context = []

    if lambda_standards:
        all_context.append("## Lambda Handler Standards")
        all_context.extend(lambda_standards)
        all_context.append("")

    if terraform_standards:
        all_context.append("## Terraform Standards")
        all_context.extend(terraform_standards)
        all_context.append("")

    if testing_standards:
        all_context.append("## Testing Standards")
        all_context.extend(testing_standards)
        all_context.append("")

    if code_style_standards:
        all_context.append("## Code Style Standards (IMPORTANT)")
        all_context.extend(code_style_standards)
        all_context.append("")

    if kb_results:
        all_context.append("## Step-Specific Context")
        all_context.extend(kb_results)

    context_text = chr(10).join(all_context) if all_context else 'No context available'

    # Build truncation recovery section if previous attempts were truncated
    truncation_section = ""
    if truncation_recovery:
        truncation_section = """
# ⚠️ OUTPUT SIZE LIMIT - GENERATE MINIMAL CODE
Previous attempts to generate this step were truncated due to output size limits.
You MUST generate significantly LESS code this time. Specifically:

1. **If generating tests**: Generate only 1-2 simple test functions, not comprehensive test suites
2. **If generating handlers**: Keep logic minimal, avoid verbose error handling
3. **If generating modules**: Include only the essential functions mentioned in the step
4. **Avoid**: Long docstrings, extensive comments, multiple examples, verbose fixtures

The goal is to generate WORKING code that fits within limits, not comprehensive code.
Quality over quantity - a simple working implementation is better than a truncated one.

"""

    # Build existing files section if any files already exist
    existing_files_section = ""
    if existing_files:
        existing_files_section = "\n# Existing File Content\nThe following files already exist and should be EXTENDED (not replaced from scratch):\n\n"
        for file_path, content in existing_files.items():
            # Truncate very long files to avoid token explosion
            if len(content) > 10000:
                content = content[:10000] + "\n... (truncated)"
            existing_files_section += f"## {file_path}\n```\n{content}\n```\n\n"
        existing_files_section += "**IMPORTANT:** Add your new code to the existing file content. Do NOT remove existing classes/functions.\n"

    # Build previous step files section for context (e.g., tests need to know handler structure)
    previous_files_section = ""
    if previous_step_files:
        previous_files_section = "\n# Code from Previous Steps\nThe following files were created in previous steps. Use them as reference for imports, function signatures, and structure:\n\n"
        for file_path, content in previous_step_files.items():
            # Truncate very long files to avoid token explosion
            if len(content) > 8000:
                content = content[:8000] + "\n... (truncated)"
            previous_files_section += f"## {file_path}\n```\n{content}\n```\n\n"
        previous_files_section += "**IMPORTANT:** Your code should integrate with and reference the code above correctly. Match import paths, function names, and parameter signatures exactly.\n"

    return f"""You are implementing step {current_step} of {total_steps}.
{truncation_section}
# User Story
{issue_description or 'No description provided'}

# Current Step
**Title:** {step.title}
**Description:** {step.description}
**Files to create/modify:** {', '.join(step.files_to_create)}
{existing_files_section}{previous_files_section}
# Knowledge Base Context
{context_text}

# Task
Generate the code for the files listed in this step. Return ONLY valid JSON following the schema in the system prompt.

CRITICAL: The content field is a JSON string. You MUST escape: newlines as \\n, double quotes as \\", backslashes as \\\\."""


# ============================================================================
# Step Execution
# ============================================================================


def execute_step(
    step_message: StepExecutionMessage,
    plan: ExecutionPlan,
    github_token: str,
    backend: Optional[CodeGeneratorBackend] = None,
) -> Dict[str, Any]:
    """
    Execute a single step from the plan.

    Steps:
    1. Load backend (if not provided)
    2. Mark step as in-progress and update plan
    3. Query KB with step's queries
    4. Invoke Claude to generate code using backend's system prompt
    5. Validate files using backend's validation
    6. Commit generated files to branch
    7. Format files using backend's formatters
    8. Mark step as completed and update plan
    9. If more steps: send next step to SQS
    10. If all steps done: create PR

    Args:
        step_message: SQS message with step info
        plan: Execution plan
        github_token: GitHub token
        backend: Optional code generator backend (loaded from plan if not provided)

    Returns:
        dict: Execution result

    Raises:
        Exception: If step execution fails
    """
    step_number = step_message.current_step
    step = next((s for s in plan.steps if s.step_number == step_number), None)

    if not step:
        raise Exception(f"Step {step_number} not found in plan")

    # Step 0: Load backend if not provided
    if backend is None:
        backend = get_backend(plan.backend_name)

    logger.info(
        f"[step-exec] Executing step {step_number}/{step_message.total_steps}: {step.title} "
        f"(backend: {backend.name})"
    )

    # Step 1: Mark step as in-progress
    plan = update_step_in_plan(plan, step_number, status="in_progress")

    # Use same naming pattern as plan generation: {issue_number}-{title}-plan.md
    base_name = generate_branch_name(plan.issue_number, plan.issue_title)
    plan_file_path = f"issues/{base_name}-plan.md"
    plan_markdown = serialize_plan_to_markdown(plan)

    update_file(
        repo_full_name=plan.repo_full_name,
        file_path=plan_file_path,
        content=plan_markdown,
        branch_name=plan.branch_name,
        commit_message=f"docs: update plan - step {step_number} in progress",
        github_token=github_token
    )

    # Step 2: Query KB with step's queries (only for step-specific context)
    logger.info(f"[step-exec] Querying KB with {len(step.kb_queries)} step-specific queries")
    kb_results = get_step_context(step.kb_queries, backend, top_k=3)
    logger.info("[step-exec] Using cached standards from plan (no re-querying)")

    # Step 2.5: Fetch existing file content for files that already exist
    # First check feature branch, then fall back to base branch (main)
    # This allows Claude to extend existing files rather than recreate from scratch
    existing_files = {}
    for file_path in step.files_to_create:
        # First try the feature branch
        existing_file_data = get_file(
            repo_full_name=plan.repo_full_name,
            file_path=file_path,
            branch_name=plan.branch_name,
            github_token=github_token
        )
        source_branch = plan.branch_name

        # If not on feature branch, try the base branch (main)
        if not existing_file_data:
            existing_file_data = get_file(
                repo_full_name=plan.repo_full_name,
                file_path=file_path,
                branch_name=step_message.base_branch,
                github_token=github_token
            )
            source_branch = step_message.base_branch

        if existing_file_data:
            # Decode base64 content from GitHub API response
            decoded_content = base64.b64decode(existing_file_data["content"]).decode("utf-8")
            existing_files[file_path] = decoded_content
            logger.info(f"[step-exec] Found existing file on {source_branch}: {file_path} ({len(decoded_content)} chars)")

    if existing_files:
        logger.info(f"[step-exec] Will extend {len(existing_files)} existing file(s)")

    # Step 2.6: Fetch files created in previous completed steps
    # This provides context for steps that depend on earlier code (e.g., tests need to know handler structure)
    previous_step_files = {}
    for prev_step in plan.steps:
        if prev_step.step_number < step_number and prev_step.status == "completed":
            for file_path in prev_step.files_to_create:
                # Don't re-fetch files we already have
                if file_path not in existing_files and file_path not in previous_step_files:
                    file_data = get_file(
                        repo_full_name=plan.repo_full_name,
                        file_path=file_path,
                        branch_name=plan.branch_name,
                        github_token=github_token
                    )
                    if file_data:
                        # Decode base64 content from GitHub API response
                        decoded_content = base64.b64decode(file_data["content"]).decode("utf-8")
                        previous_step_files[file_path] = decoded_content
                        logger.info(f"[step-exec] Fetched previous step file: {file_path} ({len(decoded_content)} chars)")

    if previous_step_files:
        logger.info(f"[step-exec] Including {len(previous_step_files)} file(s) from previous steps as context")

    # Get system prompt from backend (used for all attempts)
    system_prompt = backend.get_code_generation_system_prompt()

    # Resolve the repo's owning workspace once (outside the retry loop) so
    # each attempt's Bedrock audit row attributes code-gen spend to the
    # workspace in Analytics instead of WS#SYSTEM. Fail-soft to "".
    owning_workspace = resolve_owning_workspace(step_message.repo_full_name)

    # Check if this step previously failed with truncation error (SQS retry)
    # If so, enable truncation recovery mode to ask Claude to generate minimal code
    truncation_recovery = False
    if step.error and "truncated" in step.error.lower():
        truncation_recovery = True
        logger.info(
            f"[step-exec] Detected previous truncation failure, "
            f"enabling recovery mode for minimal code generation"
        )

    # Step 3-4.5: Generate code with validation retry loop
    MAX_VALIDATION_RETRIES = 2  # Retry twice on validation failure (3 total attempts)
    validation_error_for_retry = None
    previous_code_for_retry = None
    generated_files = None

    # Track accumulated tokens across all attempts (including failed ones)
    total_input_tokens = 0
    total_output_tokens = 0

    for attempt in range(MAX_VALIDATION_RETRIES + 1):
        attempt_label = f" (retry {attempt})" if attempt > 0 else ""

        # Step 3: Build prompt and invoke Claude
        if attempt == 0:
            # First attempt: full generation prompt with all context
            prompt = build_code_generation_prompt(
                step=step,
                issue_description=plan.issue_description,
                current_step=step_number,
                total_steps=step_message.total_steps,
                kb_results=kb_results,
                lambda_standards=plan.lambda_standards,
                terraform_standards=plan.terraform_standards,
                testing_standards=plan.testing_standards,
                code_style_standards=plan.code_style_standards,
                existing_files=existing_files if existing_files else None,
                previous_step_files=previous_step_files if previous_step_files else None,
                truncation_recovery=truncation_recovery
            )

            # Store debug context for troubleshooting (what was passed to Claude)
            step.debug_context = {
                "prompt_chars": len(prompt),
                "system_prompt_chars": len(system_prompt),
                "kb_results": kb_results,  # Step-specific KB query results
                "existing_files": list(existing_files.keys()) if existing_files else [],
                "previous_step_files": list(previous_step_files.keys()) if previous_step_files else [],
                "cached_standards_counts": {
                    "lambda": len(plan.lambda_standards),
                    "terraform": len(plan.terraform_standards),
                    "testing": len(plan.testing_standards),
                    "code_style": len(plan.code_style_standards),
                },
                # Include first 500 chars of terraform standards for version debugging
                "terraform_standards_preview": (
                    "\n".join(plan.terraform_standards)[:500]
                    if plan.terraform_standards else "None"
                ),
            }
            logger.info(f"[step-exec] Debug context stored: prompt={len(prompt)} chars, "
                        f"kb_results={len(kb_results)}, existing_files={len(existing_files)}")
        else:
            # Retry attempts: minimal fix-only prompt
            prompt = build_syntax_fix_prompt(
                validation_error=validation_error_for_retry,
                previous_code=previous_code_for_retry
            )

        logger.info(f"[step-exec] Invoking Claude to generate code{attempt_label}")

        response = invoke_claude(
            prompt=prompt,
            system_prompt=system_prompt,
            temperature=0.3,
            max_tokens=32000,  # Balance between output size and avoiding truncation waste
            workspace_id=owning_workspace
        )

        # Accumulate tokens from this attempt (even if it fails)
        total_input_tokens += response.usage.inputTokens
        total_output_tokens += response.usage.outputTokens

        # Check for truncation
        if response.stop_reason == "max_tokens":
            # Calculate wasted cost for this truncated attempt
            wasted_cost = calculate_cost(response.usage.inputTokens, response.usage.outputTokens)
            error_msg = (
                f"Code generation truncated for step {step_number}. "
                f"Step may be too large - consider splitting into smaller steps. "
                f"Wasted cost: ${wasted_cost.total_cost_usd:.2f} "
                f"({response.usage.inputTokens} input, {response.usage.outputTokens} output tokens)"
            )
            logger.error(f"[step-exec] {error_msg}")

            # Log the truncated response so we can see what was generated
            logger.info(f"[step-exec] TRUNCATED RESPONSE START ===")
            # Log in chunks to avoid CloudWatch message size limits (256KB)
            truncated_text = response.text
            chunk_size = 50000  # ~50KB chunks
            for i in range(0, len(truncated_text), chunk_size):
                chunk = truncated_text[i:i + chunk_size]
                logger.info(f"[step-exec] TRUNCATED CHUNK {i // chunk_size + 1}: {chunk}")
            logger.info(f"[step-exec] TRUNCATED RESPONSE END ===")

            # Mark step as failed with cost info
            plan = update_step_in_plan(plan, step_number, status="failed", error=error_msg, cost=wasted_cost)
            update_file(
                repo_full_name=plan.repo_full_name,
                file_path=plan_file_path,
                content=serialize_plan_to_markdown(plan),
                branch_name=plan.branch_name,
                commit_message=f"docs: update plan - step {step_number} failed (truncated)",
                github_token=github_token
            )
            raise Exception(error_msg)

        # Step 4: Extract and parse generated files using delimiter format
        try:
            generated_files = parse_delimited_files(response.text)
        except Exception as e:
            error_msg = f"Failed to parse generated files: {e}"
            logger.error(f"[step-exec] {error_msg}")
            logger.error(f"[step-exec] Response text: {response.text[:500]}")

            # Mark step as failed
            plan = update_step_in_plan(plan, step_number, status="failed", error=error_msg)
            update_file(
                repo_full_name=plan.repo_full_name,
                file_path=plan_file_path,
                content=serialize_plan_to_markdown(plan),
                branch_name=plan.branch_name,
                commit_message=f"docs: update plan - step {step_number} failed",
                github_token=github_token
            )
            raise

        # Step 4.5: Validate generated files using backend
        logger.info(f"[step-exec] Validating {len(generated_files)} files with {backend.name}{attempt_label}")

        # Prepare files for backend validation
        # With delimiter format, content is raw (no encoding), so use .content directly
        files_to_validate = [
            {"path": file.path, "content": file.content}
            for file in generated_files
        ]
        validation_errors = validate_files_with_backend(files_to_validate, backend)

        if not validation_errors:
            # Validation passed - exit retry loop
            if attempt > 0:
                logger.info(f"[step-exec] Validation passed on retry {attempt}")
            break

        # Validation failed
        error_msg = f"File validation failed:\n" + "\n".join(validation_errors)
        logger.error(f"[step-exec] {error_msg}")

        if attempt < MAX_VALIDATION_RETRIES:
            # Will retry - store error and generated code for next attempt
            logger.info(f"[step-exec] Validation failed, retrying with error feedback...")
            validation_error_for_retry = error_msg
            # Build the previous code to show Claude what it generated
            previous_code_parts = []
            for file in generated_files:
                # Infer language from file extension for syntax highlighting
                ext = Path(file.path).suffix.lower()
                lang_map = {
                    ".py": "python",
                    ".tf": "hcl",
                    ".ts": "typescript",
                    ".tsx": "typescript",
                    ".js": "javascript",
                    ".jsx": "javascript",
                    ".json": "json",
                    ".yaml": "yaml",
                    ".yml": "yaml",
                    ".txt": "",
                }
                lang = lang_map.get(ext, "")
                # With delimiter format, content is raw (no encoding)
                previous_code_parts.append(f"## {file.path}\n```{lang}\n{file.content}\n```")
            previous_code_for_retry = "\n\n".join(previous_code_parts)
        else:
            # Final attempt failed - mark step as failed and raise
            logger.error(f"[step-exec] Validation failed after {MAX_VALIDATION_RETRIES + 1} attempts")
            plan = update_step_in_plan(plan, step_number, status="failed")
            update_file(
                repo_full_name=plan.repo_full_name,
                file_path=plan_file_path,
                content=serialize_plan_to_markdown(plan),
                branch_name=plan.branch_name,
                commit_message=f"docs: update plan - step {step_number} failed (validation)",
                github_token=github_token
            )
            raise Exception(error_msg)

    # Step 5: Commit generated files to branch
    logger.info(f"[step-exec] Validation passed. Committing {len(generated_files)} files")

    # Track files that need formatting based on backend
    files_to_format = []

    for file in generated_files:
        # With delimiter format, content is raw (no encoding)
        file_content = file.content

        # Track files for formatting based on backend's formatters
        if backend.should_format_file(file.path):
            files_to_format.append(file.path)

        # Check if file already exists
        existing_file = get_file(
            repo_full_name=plan.repo_full_name,
            file_path=file.path,
            branch_name=plan.branch_name,
            github_token=github_token
        )

        if existing_file:
            # File exists, update it
            commit_message = f"feat: update {file.path} (step {step_number})"
            logger.info(f"[step-exec] Updating existing file {file.path}")

            update_file(
                repo_full_name=plan.repo_full_name,
                file_path=file.path,
                content=file_content,
                branch_name=plan.branch_name,
                commit_message=commit_message,
                github_token=github_token
            )
        else:
            # File doesn't exist, create it
            commit_message = f"feat: add {file.path} (step {step_number})"
            logger.info(f"[step-exec] Creating new file {file.path}")

            commit_file(
                repo_full_name=plan.repo_full_name,
                file_path=file.path,
                content=file_content,
                branch_name=plan.branch_name,
                commit_message=commit_message,
                github_token=github_token
            )

    # Format files using backend's formatters (currently terraform fmt)
    # TODO: Refactor to use backend.get_formatters() for full generalization
    terraform_files = [f for f in files_to_format if f.endswith('.tf')]
    if terraform_files:
        format_terraform_files(
            repo_full_name=plan.repo_full_name,
            branch_name=plan.branch_name,
            terraform_files=terraform_files,
            github_token=github_token,
            step_number=step_number
        )

    # Step 6: Mark step as completed with cost (includes all attempts including retries)
    cost = calculate_cost(total_input_tokens, total_output_tokens)

    plan = update_step_in_plan(plan, step_number, status="completed", cost=cost)

    update_file(
        repo_full_name=plan.repo_full_name,
        file_path=plan_file_path,
        content=serialize_plan_to_markdown(plan),
        branch_name=plan.branch_name,
        commit_message=f"docs: update plan - step {step_number} completed",
        github_token=github_token
    )

    logger.info(
        f"[step-exec] Step {step_number} completed. "
        f"Cost: ${cost.total_cost_usd:.6f} "
        f"({cost.input_tokens} input, {cost.output_tokens} output tokens)"
    )

    # Step 7: Check if more steps remain
    if step_number < step_message.total_steps:
        # Send next step to SQS
        next_step_message = StepExecutionMessage(
            issue_number=step_message.issue_number,
            issue_title=step_message.issue_title,
            issue_description=step_message.issue_description,
            repo_full_name=step_message.repo_full_name,
            branch_name=step_message.branch_name,
            current_step=step_number + 1,
            total_steps=step_message.total_steps,
            base_branch=step_message.base_branch,
            customer_id=step_message.customer_id
        )

        # Add delay to avoid Bedrock rate limiting (configurable via ENABLE_BEDROCK_COOLDOWN)
        # Cross-region inference profiles have low quotas, spacing out requests helps
        if ENABLE_BEDROCK_COOLDOWN:
            delay_seconds = 15
            logger.info(
                f"[step-exec] Waiting {delay_seconds}s before sending next step "
                f"(to avoid Bedrock throttling)"
            )
            time.sleep(delay_seconds)

        logger.info(f"[step-exec] Sending next step ({step_number + 1}) to SQS")
        send_step_message(next_step_message)

        return {
            "success": True,
            "step": step_number,
            "status": "completed",
            "next_step": step_number + 1
        }

    else:
        # All steps completed - emit event for test runner (NO PR yet)
        logger.info("[step-exec] All steps completed - emitting event for test runner")

        finalize_and_publish_event(
            plan=plan,
            step_message=step_message,
            github_token=github_token
        )

        return {
            "success": True,
            "step": step_number,
            "status": "completed",
            "all_steps_completed": True
        }


# ============================================================================
# Event Emission
# ============================================================================


def publish_code_generation_completed_event(
    plan: ExecutionPlan,
    step_message: StepExecutionMessage,
    pr_url: Optional[str],
    pr_number: Optional[int],
    plan_file_path: str,
    commit_sha: Optional[str]
) -> None:
    """
    Emit an EventBridge event used to trigger downstream automation (test runner).

    NOTE: pr_url and pr_number are optional - they will be None when PR creation
    is deferred to the test runner.

    The 'backend' field is used by EventBridge rules to route to the correct
    test runner Lambda (run-tests-python, run-tests-java, etc.).
    """
    detail = {
        "issueNumber": plan.issue_number,
        "issueTitle": plan.issue_title,
        "repoFullName": plan.repo_full_name,
        "branchName": plan.branch_name,
        "baseBranch": step_message.base_branch,
        "planFile": plan_file_path,
        "commitSha": commit_sha,
        "backend": plan.backend_name,
        "eventVersion": "2024-12-05",
        "environment": ENV,
        "appName": APP_NAME
    }

    # Only include PR fields if they exist (backward compatibility)
    if pr_url is not None:
        detail["prUrl"] = pr_url
    if pr_number is not None:
        detail["prNumber"] = pr_number

    entry = {
        "Source": "outcomeops.generate-code",
        "DetailType": "OutcomeOps.CodeGeneration.Completed",
        "Detail": json.dumps(detail),
        "EventBusName": EVENT_BUS_NAME
    }

    try:
        events_client.put_events(Entries=[entry])
        logger.info(
            "[step-exec] Published code generation completion event for issue #%s",
            plan.issue_number
        )
    except ClientError as exc:
        logger.error("[step-exec] Failed to publish completion event: %s", exc)

    # Emit ACTION_CODE_GENERATED audit row — the parent event for the
    # per-step Bedrock rows that bedrock_client wrote during the
    # generation cycle. plan/audit-completeness chunk 07.
    _emit_code_generated_audit(
        plan=plan,
        step_message=step_message,
        pr_url=pr_url,
        pr_number=pr_number,
        commit_sha=commit_sha,
    )


def _emit_code_generated_audit(
    plan: ExecutionPlan,
    step_message: StepExecutionMessage,
    pr_url: Optional[str],
    pr_number: Optional[int],
    commit_sha: Optional[str],
) -> None:
    """Emit ACTION_CODE_GENERATED. Non-fatal."""
    audit_table = os.environ.get("AUDIT_TABLE", "")
    if not audit_table:
        return
    try:
        log_admin_action(
            audit_table,
            ACTION_CODE_GENERATED,
            workspace_id="",  # SQS-driven; no workspace_id on the message today
            user_email="",     # webhook / Jira-driven, no human caller in moment
            source_ip="",
            user_agent="",
            details={
                "repo": plan.repo_full_name,
                "issue_number": plan.issue_number,
                "issue_title": plan.issue_title,
                "branch": plan.branch_name,
                "base_branch": step_message.base_branch,
                "backend": plan.backend_name,
                "pr_url": pr_url or "",
                "pr_number": pr_number,
                "commit_sha": commit_sha or "",
                "step_count": len(plan.steps),
            },
        )
    except Exception as exc:
        logger.warning("[step-exec] Failed to emit CODE_GENERATED audit row: %s", exc)


# ============================================================================
# PR Creation
# ============================================================================


def finalize_and_publish_event(
    plan: ExecutionPlan,
    step_message: StepExecutionMessage,
    github_token: str
) -> None:
    """
    Finalize code generation and publish event for test runner.

    NOTE: PR creation is deferred to run-tests Lambda after tests pass.

    Args:
        plan: Execution plan
        step_message: Step message with repo info
        github_token: GitHub token
    """
    logger.info("[step-exec] Publishing completion event (PR will be created after tests)")

    # Generate plan filename using same pattern
    base_name = generate_branch_name(plan.issue_number, plan.issue_title)
    plan_file_path = f"issues/{base_name}-plan.md"

    commit_sha = get_branch_head_sha(
        repo_full_name=plan.repo_full_name,
        branch_name=plan.branch_name,
        github_token=github_token
    )

    publish_code_generation_completed_event(
        plan=plan,
        step_message=step_message,
        pr_url=None,  # Will be created by run-tests after tests pass
        pr_number=None,
        plan_file_path=plan_file_path,
        commit_sha=commit_sha
    )


# ============================================================================
# SQS Message Handler
# ============================================================================


def handle_step_message(step_message: StepExecutionMessage) -> Dict[str, Any]:
    """
    Handle SQS step execution message.

    Args:
        step_message: Step execution message from SQS

    Returns:
        dict: Execution result
    """
    logger.info(
        f"[step-exec] Handling step {step_message.current_step}/{step_message.total_steps} "
        f"for issue #{step_message.issue_number}"
    )

    # Get GitHub token
    github_token = get_github_token()

    # Get plan from branch (use same naming pattern as plan generation)
    base_name = generate_branch_name(step_message.issue_number, step_message.issue_title)
    plan_file_path = f"issues/{base_name}-plan.md"

    plan_file = get_file(
        repo_full_name=step_message.repo_full_name,
        file_path=plan_file_path,
        branch_name=step_message.branch_name,
        github_token=github_token
    )

    if not plan_file:
        raise Exception(f"Plan file not found: {plan_file_path}")

    # Decode plan content (GitHub returns base64)
    import base64
    plan_markdown = base64.b64decode(plan_file["content"]).decode("utf-8")

    # Parse plan
    plan = parse_plan_from_markdown(plan_markdown)

    # Execute step
    return execute_step(step_message, plan, github_token)
