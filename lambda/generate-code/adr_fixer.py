"""
ADR Fixer module for PR comment commands.

Handles the "outcomeops: fix adr" command by:
1. Analyzing architectural changes in the PR (new patterns, modules, infrastructure)
2. Querying KB for existing ADRs and ADR patterns
3. Using Claude to generate or update ADR documentation
4. Committing the changes to the PR branch
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
    commit_file,
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

# Bedrock client for Claude
bedrock_config = Config(
    connect_timeout=60,
    read_timeout=300,
    retries={'max_attempts': 2}
)
bedrock_client = boto3.client("bedrock-runtime", config=bedrock_config)
lambda_client = boto3.client("lambda")

CLAUDE_MODEL_ID = os.environ.get(
    "BEDROCK_ADVANCED_MODEL_ID", "us.anthropic.claude-sonnet-4-6"
)


def categorize_architectural_changes(files: List[str], diff: str) -> Dict[str, List[str]]:
    """
    Categorize changed files by type of architectural change.

    Returns:
        Dict with categories: new_lambdas, modified_lambdas, new_infrastructure,
        modified_infrastructure, new_modules, api_changes, etc.
    """
    categories = {
        "new_lambdas": [],
        "modified_lambdas": [],
        "new_terraform": [],
        "modified_terraform": [],
        "new_modules": [],
        "api_changes": [],
        "dependency_changes": [],
        "config_changes": [],
        "other": []
    }

    for file in files:
        # Lambda handlers
        if file.startswith("lambda/") and "/handler.py" in file and "/tests/" not in file:
            # Check if it's a new file in the diff
            if f"+++ b/{file}" in diff and f"--- /dev/null" in diff:
                parts = file.split("/")
                if len(parts) >= 2:
                    categories["new_lambdas"].append(parts[1])
            else:
                parts = file.split("/")
                if len(parts) >= 2:
                    categories["modified_lambdas"].append(parts[1])

        # Terraform files
        elif file.startswith("terraform/") and file.endswith(".tf"):
            if f"+++ b/{file}" in diff and f"--- /dev/null" in diff:
                categories["new_terraform"].append(file)
            else:
                categories["modified_terraform"].append(file)

        # Python modules (non-handler)
        elif file.endswith(".py") and "handler.py" not in file and "/tests/" not in file:
            categories["new_modules"].append(file)

        # API-related changes
        elif "api" in file.lower() or "endpoint" in file.lower() or "route" in file.lower():
            categories["api_changes"].append(file)

        # Dependency files
        elif file in ("requirements.txt", "pyproject.toml", "package.json", "Pipfile"):
            categories["dependency_changes"].append(file)

        # Config files
        elif file.endswith((".yaml", ".yml", ".json", ".toml")) and "test" not in file.lower():
            categories["config_changes"].append(file)

        else:
            categories["other"].append(file)

    # Deduplicate
    for key in categories:
        categories[key] = list(set(categories[key]))

    return categories


def get_existing_adrs(
    repo_full_name: str,
    branch_name: str,
    github_token: str
) -> List[Dict[str, str]]:
    """
    Get list of existing ADRs from the repository's docs/adr/ directory.

    Returns:
        List of dicts with 'path', 'name', and 'number' for each ADR.
    """
    adrs = []

    # Try to list the docs/adr directory
    url = f"https://api.github.com/repos/{repo_full_name}/contents/docs/adr"
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    try:
        import requests
        response = requests.get(
            url,
            headers=headers,
            params={"ref": branch_name},
            timeout=30
        )

        if response.status_code == 200:
            contents = response.json()
            for item in contents:
                if item.get("type") == "file" and item.get("name", "").endswith(".md"):
                    name = item.get("name", "")
                    # Extract ADR number from name (e.g., "ADR-001-title.md" -> "001")
                    match = re.match(r"ADR-(\d+)", name, re.IGNORECASE)
                    adr_number = match.group(1) if match else None

                    adrs.append({
                        "path": item.get("path"),
                        "name": name,
                        "number": adr_number
                    })

            logger.info(f"[adr-fix] Found {len(adrs)} existing ADRs")

    except Exception as e:
        logger.warning(f"[adr-fix] Could not list existing ADRs: {e}")

    return adrs


def get_next_adr_number(existing_adrs: List[Dict[str, str]]) -> str:
    """
    Determine the next ADR number based on existing ADRs.

    Returns:
        Zero-padded string like "014" for the next available ADR number.
    """
    if not existing_adrs:
        return "001"

    max_number = 0
    for adr in existing_adrs:
        if adr.get("number"):
            try:
                num = int(adr["number"])
                max_number = max(max_number, num)
            except ValueError:
                pass

    return str(max_number + 1).zfill(3)


def query_kb_for_adr_context(repo_name: str) -> str:
    """
    Query knowledge base for ADR patterns and existing architectural context.

    Returns combined results from relevant KB queries.
    """
    query_kb_function_name = f"{ENV}-{APP_NAME}-query-kb"

    queries = [
        "ADR template and format standards",
        "Architecture decision record patterns",
        "Infrastructure and architectural patterns",
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
            logger.warning(f"[adr-fix] KB query failed: {e}")

    if results:
        return "\n\n---\n\n".join(results)
    return ""


def analyze_and_generate_adr(
    categories: Dict[str, List[str]],
    diff: str,
    existing_adrs: List[Dict[str, str]],
    next_adr_number: str,
    kb_context: str,
    guidance: Optional[str] = None
) -> Optional[Dict[str, str]]:
    """
    Use Claude to analyze architectural changes and generate ADR content.

    Args:
        categories: Categorized file changes
        diff: Full PR diff
        existing_adrs: List of existing ADRs
        next_adr_number: Next available ADR number
        kb_context: Context from knowledge base
        guidance: Optional user-provided guidance

    Returns:
        Dict with 'filename' and 'content' for the generated ADR, or None if no ADR needed.
    """
    # Build change summary
    changes_summary = []

    if categories["new_lambdas"]:
        changes_summary.append(f"New Lambda handlers: {', '.join(categories['new_lambdas'])}")
    if categories["modified_lambdas"]:
        changes_summary.append(f"Modified Lambda handlers: {', '.join(categories['modified_lambdas'])}")
    if categories["new_terraform"]:
        changes_summary.append(f"New Terraform files: {', '.join(categories['new_terraform'])}")
    if categories["modified_terraform"]:
        changes_summary.append(f"Modified Terraform: {', '.join(categories['modified_terraform'])}")
    if categories["new_modules"]:
        changes_summary.append(f"New/modified modules: {', '.join(categories['new_modules'][:10])}")
    if categories["dependency_changes"]:
        changes_summary.append(f"Dependency changes: {', '.join(categories['dependency_changes'])}")
    if categories["api_changes"]:
        changes_summary.append(f"API changes: {', '.join(categories['api_changes'])}")

    if not changes_summary:
        logger.info("[adr-fix] No significant architectural changes detected")
        return None

    changes_text = "\n- ".join([""] + changes_summary)

    # Build existing ADRs list
    existing_adrs_text = ""
    if existing_adrs:
        adr_list = [f"- {adr['name']}" for adr in existing_adrs[:20]]
        existing_adrs_text = f"\n\n## Existing ADRs in this repository:\n" + "\n".join(adr_list)

    # Build guidance section
    guidance_text = ""
    if guidance:
        guidance_text = f"\n\n## User Guidance:\n{guidance}"

    # Truncate diff if too long
    diff_text = diff[:15000] if len(diff) > 15000 else diff

    prompt = f"""Analyze the following PR changes and determine if an Architecture Decision Record (ADR) should be created or updated.

## Changes in this PR:{changes_text}

## PR Diff (truncated):
```diff
{diff_text}
```
{existing_adrs_text}
{guidance_text}

## Knowledge Base Context:
{kb_context[:5000] if kb_context else "No KB context available."}

## Task:
1. Analyze the architectural significance of these changes
2. If the changes warrant documentation, generate an ADR

## ADR Decision Criteria:
Create an ADR when:
- New architectural patterns are introduced
- New Lambda handlers with distinct responsibilities are added
- Infrastructure decisions are made (new resources, patterns)
- API contracts change significantly
- Major dependency changes with rationale
- Design decisions that future developers need to understand

Do NOT create an ADR for:
- Minor bug fixes
- Code refactoring without architectural impact
- Test file changes
- Documentation-only changes
- Simple updates to existing patterns

## Output Format:
If an ADR is needed, output ONLY a JSON object:
```json
{{
  "create_adr": true,
  "filename": "ADR-{next_adr_number}-kebab-case-title.md",
  "content": "Full ADR content in markdown..."
}}
```

If no ADR is needed, output:
```json
{{
  "create_adr": false,
  "reason": "Brief explanation why no ADR is needed"
}}
```

## ADR Template to follow:
```markdown
# ADR-NNN: Title

## Status: Accepted

## Context
What problem or situation does this address?

## Decision
What was decided and why?

## Consequences
### Positive
- Benefit 1

### Tradeoffs
- Tradeoff 1

## Implementation
How is this implemented?

## Related ADRs
- List any related ADRs

Version History:
- v1.0 (YYYY-MM-DD): Initial decision
```"""

    system_prompt = """You are an expert software architect analyzing code changes to determine if Architecture Decision Records (ADRs) should be created.

Rules:
- Be selective - only create ADRs for genuinely architectural decisions
- Follow the exact template format
- Use clear, concise language
- Include code examples where helpful
- Output valid JSON only"""

    try:
        response = bedrock_client.converse(
            modelId=CLAUDE_MODEL_ID,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            system=[{"text": system_prompt}],
            inferenceConfig={"temperature": 0.3, "maxTokens": 8000}
        )

        usage = response.get("usage", {})
        input_tokens = usage.get("inputTokens", 0)
        output_tokens = usage.get("outputTokens", 0)
        logger.info(f"[adr-fix] Claude tokens: {input_tokens} in, {output_tokens} out")

        response_text = response.get("output", {}).get("message", {}).get("content", [{}])[0].get("text", "")

        # Extract JSON from response
        json_match = re.search(r'\{[\s\S]*\}', response_text)
        if not json_match:
            logger.warning("[adr-fix] Could not extract JSON from Claude response")
            return None

        result = json.loads(json_match.group())

        if not result.get("create_adr", False):
            logger.info(f"[adr-fix] No ADR needed: {result.get('reason', 'No reason given')}")
            return None

        return {
            "filename": result.get("filename", f"ADR-{next_adr_number}-architectural-decision.md"),
            "content": result.get("content", "")
        }

    except json.JSONDecodeError as e:
        logger.error(f"[adr-fix] Failed to parse Claude JSON response: {e}")
        return None
    except Exception as e:
        logger.error(f"[adr-fix] Claude invocation failed: {e}")
        return None


def handle_fix_adr(message: PRCommandMessage) -> Dict[str, Any]:
    """
    Handle the fix_adr PR command.

    Args:
        message: PRCommandMessage with PR details

    Returns:
        Dict with status and details
    """
    logger.info(
        f"[adr-fix] Processing fix ADR for PR #{message.pr_number} "
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
                "## OutcomeOps: Fix ADR\n\nNo changed files found in this PR.",
                github_token
            )
            return {"status": "no_changes", "pr_number": message.pr_number}

        logger.info(f"[adr-fix] Found {len(changed_files)} changed files")

        # Get PR diff for analysis
        full_diff = get_pr_diff(message.repo_full_name, message.pr_number, github_token)

        # Categorize the changes
        categories = categorize_architectural_changes(changed_files, full_diff)
        logger.info(f"[adr-fix] Change categories: {json.dumps({k: len(v) for k, v in categories.items()})}")

        # Get existing ADRs
        existing_adrs = get_existing_adrs(
            message.repo_full_name,
            message.branch_name,
            github_token
        )

        # Determine next ADR number
        next_adr_number = get_next_adr_number(existing_adrs)
        logger.info(f"[adr-fix] Next ADR number: {next_adr_number}")

        # Query KB for ADR patterns and context
        repo_name = message.repo_full_name.split("/")[-1]
        kb_context = query_kb_for_adr_context(repo_name)
        logger.info(f"[adr-fix] KB context: {len(kb_context)} chars")

        # Analyze changes and generate ADR
        adr_result = analyze_and_generate_adr(
            categories,
            full_diff,
            existing_adrs,
            next_adr_number,
            kb_context,
            message.guidance
        )

        if not adr_result:
            post_pr_comment(
                message.repo_full_name,
                message.pr_number,
                """## OutcomeOps: Fix ADR

After analyzing the changes in this PR, I determined that no new ADR is needed.

ADRs are typically created for:
- New architectural patterns or significant design decisions
- Major infrastructure changes
- New Lambda handlers with distinct responsibilities
- Important API changes

The changes in this PR don't appear to require formal architectural documentation.

---
*Powered by [OutcomeOps AI Assist](https://outcomeops.ai)*
""",
                github_token
            )
            return {"status": "no_adr_needed", "pr_number": message.pr_number}

        # Validate ADR content
        if not adr_result.get("content") or len(adr_result.get("content", "")) < 100:
            logger.warning("[adr-fix] Generated ADR content too short")
            post_pr_comment(
                message.repo_full_name,
                message.pr_number,
                "## OutcomeOps: Fix ADR\n\nFailed to generate ADR content. Please create the ADR manually.",
                github_token
            )
            return {"status": "generation_failed", "pr_number": message.pr_number}

        # Commit the new ADR
        adr_path = f"docs/adr/{adr_result['filename']}"
        commit_message = f"docs(adr): add {adr_result['filename']}\n\nGenerated by OutcomeOps AI Assist"

        # Check if file already exists (shouldn't, but handle it)
        existing_file = get_file(
            message.repo_full_name,
            adr_path,
            message.branch_name,
            github_token
        )

        if existing_file:
            # Update existing file
            update_file(
                message.repo_full_name,
                adr_path,
                adr_result["content"],
                message.branch_name,
                commit_message,
                github_token
            )
        else:
            # Create new file
            commit_file(
                message.repo_full_name,
                adr_path,
                adr_result["content"],
                message.branch_name,
                commit_message,
                github_token
            )

        # Post success comment
        success_comment = f"""## OutcomeOps: Fix ADR

I've created a new Architecture Decision Record based on the changes in this PR:

**New ADR:** `{adr_path}`

### Summary
The ADR documents the architectural decisions made in this PR. Please review and adjust as needed.

### Next Steps
1. Review the generated ADR for accuracy
2. Update the content if needed
3. The ADR will be automatically ingested into the knowledge base on merge

---
*Powered by [OutcomeOps AI Assist](https://outcomeops.ai)*
"""

        post_pr_comment(
            message.repo_full_name,
            message.pr_number,
            success_comment,
            github_token
        )

        logger.info(f"[adr-fix] Successfully created ADR: {adr_path}")

        return {
            "status": "success",
            "pr_number": message.pr_number,
            "adr_path": adr_path
        }

    except Exception as e:
        logger.error(f"[adr-fix] Failed: {e}", exc_info=True)

        # Try to post error comment
        try:
            github_token = get_github_token()
            post_pr_comment(
                message.repo_full_name,
                message.pr_number,
                f"## OutcomeOps: Fix ADR\n\nAn error occurred while generating the ADR:\n```\n{str(e)}\n```",
                github_token
            )
        except Exception:
            pass

        return {"status": "error", "error": str(e), "pr_number": message.pr_number}
