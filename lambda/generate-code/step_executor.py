"""
Step execution flow.

Handles executing individual code generation steps from SQS messages.
"""

import json
import logging
import os
import time
from typing import Dict, Any, List, Optional

import boto3
from botocore.exceptions import ClientError
from bedrock_client import invoke_claude, extract_json_from_response
from github_api import (
    get_github_token,
    get_file,
    commit_file,
    update_file,
    create_pull_request,
    post_pr_comment,
    get_branch_head_sha
)
from knowledge_base import query_knowledge_base
from models import (
    StepExecutionMessage,
    ExecutionPlan,
    PlanStep,
    GeneratedFilesResponse,
    GeneratedFile
)
from plan_manager import parse_plan_from_markdown, update_step_in_plan, serialize_plan_to_markdown
from sqs_client import send_step_message
from utils import calculate_cost, generate_branch_name

logger = logging.getLogger()

events_client = boto3.client("events")
EVENT_BUS_NAME = os.environ.get("EVENT_BUS_NAME", "default")
ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")


# ============================================================================
# Code Generation Prompt
# ============================================================================


CODE_GENERATION_SYSTEM_PROMPT = """You are a senior software engineer implementing code based on a step-by-step plan.

Your task is to generate production-ready code for the files specified in the current step.

CRITICAL REQUIREMENTS:
1. Generate complete, working code that follows the standards provided
2. Use base64 encoding for file content to avoid JSON escaping issues
3. Return ONLY valid JSON in this exact format:

{
  "files": [
    {
      "path": "path/to/file.py",
      "contentBase64": "BASE64_ENCODED_FILE_CONTENT"
    }
  ]
}

IMPORTANT:
- Encode the file content using base64 encoding
- Do NOT include raw code in the JSON
- This avoids all JSON escaping issues with quotes, newlines, and backslashes"""


def build_code_generation_prompt(
    step: PlanStep,
    issue_description: str,
    current_step: int,
    total_steps: int,
    kb_results: List[str],
    lambda_standards: List[str] = None,
    terraform_standards: List[str] = None,
    testing_standards: List[str] = None
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

    if kb_results:
        all_context.append("## Step-Specific Context")
        all_context.extend(kb_results)

    context_text = chr(10).join(all_context) if all_context else 'No context available'

    return f"""You are implementing step {current_step} of {total_steps}.

# User Story
{issue_description or 'No description provided'}

# Current Step
**Title:** {step.title}
**Description:** {step.description}
**Files to create:** {', '.join(step.files_to_create)}

# Knowledge Base Context
{context_text}

# Task
Generate the code for the files listed in this step. Return ONLY valid JSON following the schema in the system prompt.

CRITICAL: Use base64 encoding for the file content. Generate the complete file content, base64 encode it, and put the base64 string in the "contentBase64" field."""


# ============================================================================
# Step Execution
# ============================================================================


def execute_step(
    step_message: StepExecutionMessage,
    plan: ExecutionPlan,
    github_token: str
) -> Dict[str, Any]:
    """
    Execute a single step from the plan.

    Steps:
    1. Mark step as in-progress and update plan
    2. Query KB with step's queries
    3. Invoke Claude to generate code
    4. Commit generated files to branch
    5. Mark step as completed and update plan
    6. If more steps: send next step to SQS
    7. If all steps done: create PR

    Args:
        step_message: SQS message with step info
        plan: Execution plan
        github_token: GitHub token

    Returns:
        dict: Execution result

    Raises:
        Exception: If step execution fails
    """
    step_number = step_message.current_step
    step = next((s for s in plan.steps if s.step_number == step_number), None)

    if not step:
        raise Exception(f"Step {step_number} not found in plan")

    logger.info(
        f"[step-exec] Executing step {step_number}/{step_message.total_steps}: {step.title}"
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
    kb_results = query_knowledge_base(step.kb_queries, top_k=3)
    logger.info("[step-exec] Using cached standards from plan (no re-querying)")

    # Step 3: Build prompt and invoke Claude
    prompt = build_code_generation_prompt(
        step=step,
        issue_description=plan.issue_description,
        current_step=step_number,
        total_steps=step_message.total_steps,
        kb_results=kb_results,
        lambda_standards=plan.lambda_standards,
        terraform_standards=plan.terraform_standards,
        testing_standards=plan.testing_standards
    )

    logger.info("[step-exec] Invoking Claude to generate code")

    response = invoke_claude(
        prompt=prompt,
        system_prompt=CODE_GENERATION_SYSTEM_PROMPT,
        temperature=0.3,
        max_tokens=64000  # Bedrock limit for Claude Sonnet 4.5 via Converse API
    )

    # Check for truncation
    if response.stop_reason == "max_tokens":
        error_msg = (
            f"Code generation truncated for step {step_number}. "
            f"Step may be too large - consider splitting into smaller steps."
        )
        logger.error(f"[step-exec] {error_msg}")

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
        raise Exception(error_msg)

    # Step 4: Extract and parse generated files
    try:
        files_json = extract_json_from_response(response.text)
        generated_files = GeneratedFilesResponse(**files_json)
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

    # Step 5: Commit generated files to branch
    logger.info(f"[step-exec] Committing {len(generated_files.files)} files")

    for file in generated_files.files:
        file_content = file.decoded_content

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

    # Step 6: Mark step as completed with cost
    cost = calculate_cost(response.usage.inputTokens, response.usage.outputTokens)

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
            base_branch=step_message.base_branch
        )

        # Add delay to avoid Bedrock rate limiting
        # Cross-region inference profiles have low quotas, spacing out requests helps
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
    """
    detail = {
        "issueNumber": plan.issue_number,
        "issueTitle": plan.issue_title,
        "repoFullName": plan.repo_full_name,
        "branchName": plan.branch_name,
        "baseBranch": step_message.base_branch,
        "planFile": plan_file_path,
        "commitSha": commit_sha,
        "eventVersion": "2024-11-09",
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
