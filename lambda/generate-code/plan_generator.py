"""
Plan generation flow.

Handles the initial plan generation when a GitHub issue is labeled for code generation.
"""

import json
import logging
from typing import Dict, Any

from bedrock_client import invoke_claude, extract_json_from_response
from github_api import get_github_token, commit_file
from knowledge_base import get_lambda_standards, get_terraform_standards, get_testing_standards
from models import ExecutionPlan, PlanStep, GitHubWebhookEvent, StepExecutionMessage
from plan_manager import serialize_plan_to_markdown
from sqs_client import send_step_message
from utils import generate_branch_name, calculate_cost

logger = logging.getLogger()


# ============================================================================
# Plan Generation Prompt
# ============================================================================


PLAN_GENERATION_SYSTEM_PROMPT = """You are a senior software engineer planning the implementation of a GitHub issue.

Your task is to generate a step-by-step implementation plan that breaks down the work into small, focused steps.

CRITICAL REQUIREMENTS:
1. Each step should be completable in under 5 minutes
2. File granularity:
   - Large files (handlers, complex logic): 1 file per step
   - Small files (schemas, types): 2-3 files per step if related
   - Infrastructure: Separate step for Terraform changes
3. Test step granularity (CRITICAL - prevents timeouts):
   - DO NOT create a single "Create unit tests for handler" step
   - BREAK DOWN test creation into multiple focused steps based on the testing standards provided:
     * Step N: Create unit tests for success/happy path cases (1-3 test functions)
     * Step N+1: Create unit tests for error handling (1-3 test functions)
     * Step N+2: Create unit tests for edge cases (1-3 test functions)
     * Additional steps for integration tests if needed
   - Each test step should generate 1-3 test functions maximum
   - This keeps each Claude invocation under 3 minutes and prevents timeouts
4. Include ONLY step-specific KB queries (general standards are already provided)
5. Build incrementally on previous steps

KB QUERY GUIDELINES:
- General standards (Lambda patterns, error handling, testing, Terraform) are ALREADY available
- ONLY include queries for step-specific context:
  - Specific patterns for this step (e.g., "Pydantic validation for DynamoDB pagination")
  - Examples relevant to this step (e.g., "Lambda handler examples using boto3 scan")
  - ADRs specific to this feature (e.g., "ADR for API pagination standards")
- DO NOT query for general standards like "Lambda error handling" or "Testing best practices"

DO NOT include steps for:
- Manual verification or compliance checks
- PR review or validation steps
- "Ensure handler follows patterns" type steps

The automated PR analysis system will handle all compliance checks after the MR is created.

Return ONLY valid JSON in this exact format:
{
  "steps": [
    {
      "stepNumber": 1,
      "title": "Brief step title",
      "description": "What this step accomplishes",
      "filesToCreate": ["path/to/file.py"],
      "kbQueries": ["Step-specific query (NOT general standards)"],
      "status": "pending"
    }
  ]
}"""


def build_plan_generation_prompt(
    issue_number: int,
    issue_title: str,
    issue_description: str,
    lambda_standards: list,
    terraform_standards: list,
    testing_standards: list
) -> str:
    """
    Build the prompt for plan generation.

    Args:
        issue_number: GitHub issue number
        issue_title: Issue title
        issue_description: Issue description
        lambda_standards: Lambda handler standards from KB
        terraform_standards: Terraform standards from KB
        testing_standards: Testing standards from KB

    Returns:
        str: Complete prompt for Claude
    """
    return f"""# User Story
Issue: #{issue_number}
Title: {issue_title}
Description:
{issue_description or 'No description provided'}

# Available Standards
## Lambda Handler Standards
{chr(10).join(lambda_standards) if lambda_standards else 'No Lambda standards available'}

## Terraform Standards
{chr(10).join(terraform_standards) if terraform_standards else 'No Terraform standards available'}

## Testing Standards
{chr(10).join(testing_standards) if testing_standards else 'No testing standards available'}

# Task
Generate a step-by-step implementation plan for this issue. Focus on creating Lambda handlers, Terraform configs, and tests based on the user story.

IMPORTANT: When creating test-related steps, break them into multiple focused steps (success cases, error handling, edge cases) with 1-3 test functions each. DO NOT create a single monolithic "Create unit tests" step.

Return the plan as JSON following the schema in the system prompt."""


# ============================================================================
# Plan Generation
# ============================================================================


def generate_execution_plan(webhook_event: GitHubWebhookEvent) -> ExecutionPlan:
    """
    Generate execution plan from GitHub issue.

    Steps:
    1. Query KB for standards
    2. Invoke Claude to generate plan
    3. Parse plan from JSON response

    Args:
        webhook_event: GitHub webhook event

    Returns:
        ExecutionPlan: Generated execution plan

    Raises:
        Exception: If plan generation fails
    """
    logger.info(
        f"[plan-gen] Generating plan for issue #{webhook_event.issue.number}"
    )

    # Step 1: Query knowledge base for standards
    logger.info("[plan-gen] Querying knowledge base for standards")

    lambda_standards = get_lambda_standards()
    terraform_standards = get_terraform_standards()
    testing_standards = get_testing_standards()

    # Step 2: Build prompt
    prompt = build_plan_generation_prompt(
        issue_number=webhook_event.issue.number,
        issue_title=webhook_event.issue.title,
        issue_description=webhook_event.issue.body,
        lambda_standards=lambda_standards,
        terraform_standards=terraform_standards,
        testing_standards=testing_standards
    )

    # Step 3: Invoke Claude
    logger.info("[plan-gen] Invoking Claude to generate plan")

    response = invoke_claude(
        prompt=prompt,
        system_prompt=PLAN_GENERATION_SYSTEM_PROMPT,
        temperature=0.3,
        max_tokens=4000
    )

    # Check for truncation
    if response.stop_reason == "max_tokens":
        raise Exception(
            "Plan generation response truncated. Issue may be too complex. "
            "Try breaking it into smaller issues."
        )

    # Step 4: Extract JSON from response
    try:
        plan_json = extract_json_from_response(response.text)
    except ValueError as e:
        logger.error(f"[plan-gen] Failed to extract JSON from response: {e}")
        logger.error(f"[plan-gen] Response text: {response.text[:500]}")
        raise

    # Step 5: Parse steps
    steps_data = plan_json.get("steps", [])
    if not steps_data:
        raise Exception("No steps in plan JSON")

    steps = [
        PlanStep(
            step_number=step["stepNumber"],
            title=step["title"],
            description=step["description"],
            files_to_create=step.get("filesToCreate", []),
            kb_queries=step.get("kbQueries", []),
            status="pending"
        )
        for step in steps_data
    ]

    # Step 6: Create execution plan
    branch_name = generate_branch_name(webhook_event.issue.number, webhook_event.issue.title)

    plan = ExecutionPlan(
        issue_number=webhook_event.issue.number,
        issue_title=webhook_event.issue.title,
        issue_description=webhook_event.issue.body,
        branch_name=branch_name,
        repo_full_name=webhook_event.repository.full_name,
        steps=steps,
        lambda_standards=lambda_standards,
        terraform_standards=terraform_standards,
        testing_standards=testing_standards
    )

    logger.info(f"[plan-gen] Plan generated with {len(steps)} steps")
    logger.info(
        f"[plan-gen] Plan generation cost: "
        f"{response.usage.inputTokens} input tokens, "
        f"{response.usage.outputTokens} output tokens"
    )

    return plan


# ============================================================================
# Webhook Handler
# ============================================================================


def handle_webhook(webhook_event: GitHubWebhookEvent) -> Dict[str, Any]:
    """
    Handle GitHub webhook event for approved-for-generation label.

    Flow:
    1. Generate execution plan (query KB + invoke Claude)
    2. Commit plan as markdown to branch
    3. Send first step message to SQS queue

    Args:
        webhook_event: GitHub webhook event

    Returns:
        dict: Response with plan summary and branch URL
    """
    logger.info(
        f"[plan-gen] Handling webhook for issue #{webhook_event.issue.number} "
        f"in {webhook_event.repository.full_name}"
    )

    # Generate execution plan
    plan = generate_execution_plan(webhook_event)

    # Serialize plan to markdown
    plan_markdown = serialize_plan_to_markdown(plan)

    # Commit plan to branch
    github_token = get_github_token()

    # Use same naming pattern as branch: {issue_number}-{kebab-case-title}-plan.md
    base_name = generate_branch_name(plan.issue_number, plan.issue_title)
    plan_file_path = f"issues/{base_name}-plan.md"

    logger.info(f"[plan-gen] Committing plan to {plan_file_path}")

    commit_file(
        repo_full_name=plan.repo_full_name,
        file_path=plan_file_path,
        content=plan_markdown,
        branch_name=plan.branch_name,
        commit_message=f"docs: add code generation plan for issue #{plan.issue_number}",
        github_token=github_token
    )

    # Send first step message to SQS
    if plan.steps:
        first_step_message = StepExecutionMessage(
            issue_number=plan.issue_number,
            issue_title=plan.issue_title,
            issue_description=plan.issue_description,
            repo_full_name=plan.repo_full_name,
            branch_name=plan.branch_name,
            current_step=1,
            total_steps=len(plan.steps),
            base_branch=webhook_event.repository.default_branch
        )

        logger.info("[plan-gen] Sending first step message to SQS")
        send_step_message(first_step_message)

    branch_url = f"https://github.com/{plan.repo_full_name}/tree/{plan.branch_name}"
    plan_url = f"https://github.com/{plan.repo_full_name}/blob/{plan.branch_name}/{plan_file_path}"

    return {
        "success": True,
        "message": "Code generation plan created",
        "issue_number": plan.issue_number,
        "branch_name": plan.branch_name,
        "branch_url": branch_url,
        "plan_url": plan_url,
        "total_steps": len(plan.steps)
    }
