# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
README Fixer module for PR comment commands.

Handles the "outcomeops: fix readme" command by:
1. Analyzing what changed in the PR (handlers, terraform)
2. Reading the current README
3. Querying KB for README documentation patterns
4. Using Claude to update the README with proper documentation
5. Committing the update to the PR branch
"""

import base64
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

import boto3
from botocore.config import Config

from github_api import (
    get_file,
    get_github_token,
    get_pr_changed_files,
    get_pr_diff,
    post_pr_comment,
    update_file,
)
from models import PRCommandMessage

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Environment variables
ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")

# Shared model client (dispatches Converse / Mantle Responses per ADR-035).
from shared.model_client import invoke as invoke_model

lambda_client = boto3.client("lambda")

CLAUDE_MODEL_ID = os.environ.get(
    "BEDROCK_ADVANCED_MODEL_ID", "us.anthropic.claude-sonnet-4-6"
)


def extract_handler_names(files: List[str]) -> List[str]:
    """
    Extract handler names from file paths.

    Example: lambda/hello/handler.py -> "hello"
    """
    handlers = []
    for file in files:
        if file.startswith("lambda/") and "/handler.py" in file and "/tests/" not in file:
            parts = file.split("/")
            if len(parts) >= 2:
                handlers.append(parts[1])
    return list(set(handlers))


def extract_infrastructure_names(files: List[str]) -> List[str]:
    """Extract infrastructure resource names from Terraform files."""
    infra_names = []
    for file in files:
        if file.startswith("terraform/") and file.endswith(".tf"):
            name = file.replace("terraform/", "").replace(".tf", "")
            infra_names.append(name)
    return infra_names


def extract_file_diff_from_full_diff(full_diff: str, file_path: str) -> str:
    """Extract a specific file's diff section from the full PR diff."""
    lines = full_diff.split("\n")
    file_diff_lines = []
    capturing = False

    for line in lines:
        if line.startswith("diff --git") and file_path in line:
            capturing = True
            file_diff_lines.append(line)
        elif capturing and line.startswith("diff --git"):
            break
        elif capturing:
            file_diff_lines.append(line)

    return "\n".join(file_diff_lines) if file_diff_lines else ""


def query_kb_for_readme_patterns(repo_name: str) -> str:
    """
    Query knowledge base for README documentation patterns.

    Returns combined results from relevant KB queries.
    """
    query_kb_function_name = f"{ENV}-{APP_NAME}-query-kb"

    queries = [
        "README documentation structure and format",
        "Lambda handler documentation patterns",
        "Project directory structure documentation",
    ]

    results = []
    for query in queries:
        try:
            payload = {
                "query": query,
                "topK": 3,
                "raw": True,
                "repo": repo_name
            }

            response = lambda_client.invoke(
                FunctionName=query_kb_function_name,
                InvocationType="RequestResponse",
                Payload=json.dumps(payload)
            )

            response_payload = json.loads(response["Payload"].read())

            if response.get("FunctionError"):
                continue

            if "body" in response_payload:
                body = json.loads(response_payload["body"])
                raw_results = body.get("results", [])

                for result in raw_results:
                    if result.get("score", 0) > 0.3:
                        results.append(f"Source: {result.get('source', 'Unknown')}\n{result.get('text', '')}")

        except Exception as e:
            logger.warning(f"[readme-fix] KB query failed: {e}")

    if results:
        return "\n\n---\n\n".join(results)
    return ""


def generate_readme_update(
    current_readme: str,
    handler_names: List[str],
    infra_names: List[str],
    handler_diffs: Dict[str, str],
    kb_patterns: str
) -> Optional[str]:
    """
    Use Claude to generate an updated README.

    Args:
        current_readme: Current README content
        handler_names: Names of handlers that changed
        infra_names: Names of terraform files that changed
        handler_diffs: Dict of handler name -> diff content
        kb_patterns: README patterns from knowledge base

    Returns:
        Updated README content, or None on failure
    """
    # Build context about what changed
    changes_context = []

    if handler_names:
        changes_context.append(f"Lambda handlers added/modified: {', '.join(handler_names)}")
        for name, diff in handler_diffs.items():
            if diff and len(diff) < 5000:  # Include diffs if not too large
                changes_context.append(f"\n### Diff for {name}:\n```diff\n{diff[:3000]}\n```")

    if infra_names:
        changes_context.append(f"Terraform files changed: {', '.join(infra_names)}")

    changes_text = "\n".join(changes_context)

    # Build KB context section
    kb_context_text = ""
    if kb_patterns:
        kb_context_text = f"""
## Documentation Standards (from Knowledge Base):
{kb_patterns[:4000]}
"""

    prompt = f"""{kb_context_text}

## Task: Update README to Document Changes

The following changes were made but the README does not adequately document them:

{changes_text}

## Current README:
```markdown
{current_readme}
```

## Instructions:
1. Update the README to document the new/changed Lambda handlers
2. Add entries to any existing tables or lists of Lambda functions
3. Update directory structure sections if present
4. Keep the existing README structure and style
5. Only add documentation for the specific changes listed above
6. Do NOT remove any existing content unless it's clearly incorrect

Output the COMPLETE updated README.md content.
Do NOT include markdown code fences around the output - output raw markdown only."""

    system_prompt = """You are updating a README.md file to document new Lambda handlers and infrastructure changes.

Rules:
- Preserve the existing README structure and formatting style
- Add documentation for the new components in appropriate sections
- If the README has a Lambda functions table, add entries there
- If there's a directory structure section, update it
- Be concise - add just enough documentation to explain what the new components do
- Output raw markdown, no code fences around the entire output"""

    try:
        # ADR-016 v1.2 -- code-gen workload attribution.
        resp = invoke_model(
            workload="code-gen",
            tier="advanced",
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            system=system_prompt,
            temperature=0.2,
            max_tokens=8000,
        )

        input_tokens = resp.usage.input_tokens
        output_tokens = resp.usage.output_tokens
        logger.info(f"[readme-fix] tokens: {input_tokens} in, {output_tokens} out")

        updated_readme = resp.text

        if not updated_readme or len(updated_readme) < 50:
            logger.warning("[readme-fix] Claude returned empty or very short content")
            return None

        # Clean up any markdown fences that might have been added
        updated_readme = re.sub(r'^```markdown\s*\n', '', updated_readme)
        updated_readme = re.sub(r'\n```\s*$', '', updated_readme)

        return updated_readme.strip()

    except Exception as e:
        logger.error(f"[readme-fix] Claude invocation failed: {e}")
        return None


def handle_fix_readme(message: PRCommandMessage) -> Dict[str, Any]:
    """
    Handle the fix_readme PR command.

    Args:
        message: PRCommandMessage with PR details

    Returns:
        Dict with status and details
    """
    logger.info(
        f"[readme-fix] Processing fix readme for PR #{message.pr_number} "
        f"in {message.repo_full_name}"
    )

    try:
        # Get GitHub token
        github_token = get_github_token()

        # Get PR changed files
        changed_files = get_pr_changed_files(
            message.repo_full_name,
            message.pr_number,
            github_token
        )

        if not changed_files:
            post_pr_comment(
                message.repo_full_name,
                message.pr_number,
                "## OutcomeOps: Fix README\n\nNo changed files found in this PR.",
                github_token
            )
            return {"status": "no_changes", "pr_number": message.pr_number}

        # Identify what changed
        handler_names = extract_handler_names(changed_files)
        infra_names = extract_infrastructure_names(changed_files)

        if not handler_names and not infra_names:
            post_pr_comment(
                message.repo_full_name,
                message.pr_number,
                "## OutcomeOps: Fix README\n\nNo Lambda handlers or Terraform changes detected that need documentation.",
                github_token
            )
            return {"status": "no_doc_needed", "pr_number": message.pr_number}

        logger.info(f"[readme-fix] Found handlers: {handler_names}, infra: {infra_names}")

        # Get current README
        readme_data = get_file(
            message.repo_full_name,
            "README.md",
            message.branch_name,
            github_token
        )

        if not readme_data:
            post_pr_comment(
                message.repo_full_name,
                message.pr_number,
                "## OutcomeOps: Fix README\n\nNo README.md found in the repository.",
                github_token
            )
            return {"status": "no_readme", "pr_number": message.pr_number}

        # Decode README content
        current_readme = base64.b64decode(readme_data.get("content", "")).decode("utf-8")
        logger.info(f"[readme-fix] Current README: {len(current_readme)} chars")

        # Get PR diff for handler files
        full_diff = get_pr_diff(message.repo_full_name, message.pr_number, github_token)

        handler_diffs = {}
        for handler in handler_names:
            handler_path = f"lambda/{handler}/handler.py"
            diff = extract_file_diff_from_full_diff(full_diff, handler_path)
            if diff:
                handler_diffs[handler] = diff

        # Query KB for README patterns
        repo_name = message.repo_full_name.split("/")[-1]
        kb_patterns = query_kb_for_readme_patterns(repo_name)
        logger.info(f"[readme-fix] KB patterns: {len(kb_patterns)} chars")

        # Generate updated README
        updated_readme = generate_readme_update(
            current_readme,
            handler_names,
            infra_names,
            handler_diffs,
            kb_patterns
        )

        if not updated_readme:
            post_pr_comment(
                message.repo_full_name,
                message.pr_number,
                "## OutcomeOps: Fix README\n\nFailed to generate README update. Please update manually.",
                github_token
            )
            return {"status": "generation_failed", "pr_number": message.pr_number}

        # Check if README actually changed
        if updated_readme.strip() == current_readme.strip():
            post_pr_comment(
                message.repo_full_name,
                message.pr_number,
                "## OutcomeOps: Fix README\n\nREADME appears to already document the changes adequately.",
                github_token
            )
            return {"status": "no_update_needed", "pr_number": message.pr_number}

        # Commit the updated README
        commit_message = f"docs: update README for {', '.join(handler_names or infra_names)}\n\nGenerated by OutcomeOps AI Assist"

        update_file(
            message.repo_full_name,
            "README.md",
            updated_readme,
            message.branch_name,
            commit_message,
            github_token
        )

        # Post success comment
        changes_summary = []
        if handler_names:
            changes_summary.append(f"- Documented Lambda handlers: {', '.join(handler_names)}")
        if infra_names:
            changes_summary.append(f"- Documented infrastructure: {', '.join(infra_names)}")

        success_comment = f"""## OutcomeOps: Fix README

README.md has been updated to document the following changes:

{chr(10).join(changes_summary)}

Please review the changes and adjust as needed.

---
*Powered by [OutcomeOps AI Assist](https://outcomeops.ai)*
"""

        post_pr_comment(
            message.repo_full_name,
            message.pr_number,
            success_comment,
            github_token
        )

        logger.info(f"[readme-fix] Successfully updated README for PR #{message.pr_number}")

        return {
            "status": "success",
            "pr_number": message.pr_number,
            "handlers_documented": handler_names,
            "infra_documented": infra_names
        }

    except Exception as e:
        logger.error(f"[readme-fix] Failed: {e}", exc_info=True)

        # Try to post error comment
        try:
            github_token = get_github_token()
            post_pr_comment(
                message.repo_full_name,
                message.pr_number,
                f"## OutcomeOps: Fix README\n\nAn error occurred while updating the README:\n```\n{str(e)}\n```",
                github_token
            )
        except Exception:
            pass

        return {"status": "error", "error": str(e), "pr_number": message.pr_number}
