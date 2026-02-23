"""
Plan generation flow.

Handles the initial plan generation when a GitHub issue is labeled for code generation.
Uses pluggable backends for language-specific plan generation.
"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional

from codegen_backends import CodeGeneratorBackend, get_backend_for_repo
from codegen_backends.base import StandardsContext
from bedrock_client import invoke_claude, extract_json_from_response
from github_api import get_github_token, commit_file, update_file, get_file, create_pull_request
from knowledge_base import (
    get_lambda_standards,
    get_standards_for_backend,
    get_summarization_cost,
    get_terraform_standards,
    get_testing_standards,
    reset_summarization_cost,
)
from models import ExecutionPlan, PlanStep, GitHubWebhookEvent, StepExecutionMessage
from plan_manager import serialize_plan_to_markdown
from repo_config import RepoConfig, load_repo_config_from_github
from sqs_client import send_step_message
from utils import generate_branch_name, calculate_cost

logger = logging.getLogger()


# ============================================================================
# Plan Generation Prompt (Legacy - kept for backwards compatibility)
# ============================================================================


PLAN_GENERATION_SYSTEM_PROMPT = """You are a senior software engineer planning the implementation of a GitHub issue.

Your task is to generate a step-by-step implementation plan that breaks down the work into small, focused steps.

CRITICAL REQUIREMENTS:
1. Each step should be completable in under 5 minutes
2. File granularity:
   - Large files (handlers, complex logic): 1 file per step
   - Small files (schemas, types): 2-3 files per step if related
   - Infrastructure: Separate step for Terraform changes
   - Documentation: Split into multiple steps if >500 lines expected
3. OUTPUT SIZE LIMIT (CRITICAL - prevents truncation):
   - Each step's generated code must fit in ~50K tokens output
   - If a file might exceed 500 lines, split into multiple steps
   - Documentation steps should cover ONE section at a time
   - Terraform steps: 1 resource/module per step for complex configs
4. Test step granularity (CRITICAL - prevents timeouts):
   - DO NOT create a single "Create unit tests for handler" step
   - BREAK DOWN test creation into multiple focused steps based on the testing standards provided:
     * Step N: Create unit tests for success/happy path cases (1-3 test functions)
     * Step N+1: Create unit tests for error handling (1-3 test functions)
     * Step N+2: Create unit tests for edge cases (1-3 test functions)
     * Additional steps for integration tests if needed
   - Each test step should generate 1-3 test functions maximum
   - This keeps each Claude invocation under 3 minutes and prevents timeouts
5. Include ONLY step-specific KB queries (general standards are already provided)
6. Build incrementally on previous steps

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
    Build the prompt for plan generation (legacy function).

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
# Backend-Aware Plan Generation
# ============================================================================


def build_plan_prompt_with_backend(
    issue_number: int,
    issue_title: str,
    issue_description: str,
    standards: StandardsContext,
    backend: CodeGeneratorBackend,
    repo_config: Optional[RepoConfig] = None,
) -> str:
    """
    Build the prompt for plan generation using backend-specific context.

    Args:
        issue_number: GitHub issue number
        issue_title: Issue title
        issue_description: Issue description
        standards: Standards context from knowledge base
        backend: Code generator backend
        repo_config: Optional repository configuration

    Returns:
        str: Complete prompt for Claude
    """
    # Build standards sections
    sections = []

    if standards.handler_standards:
        sections.append(f"## {backend.display_name} Handler Standards")
        sections.extend(standards.handler_standards)
        sections.append("")

    if standards.infrastructure_standards:
        sections.append("## Infrastructure Standards")
        sections.extend(standards.infrastructure_standards)
        sections.append("")

    if standards.testing_standards:
        sections.append("## Testing Standards")
        sections.extend(standards.testing_standards)
        sections.append("")

    if standards.language_standards:
        sections.append("## Language-Specific Standards")
        sections.extend(standards.language_standards)
        sections.append("")

    if standards.framework_standards:
        sections.append("## Framework Standards")
        sections.extend(standards.framework_standards)
        sections.append("")

    if standards.code_style_standards:
        sections.append("## Code Style Standards (IMPORTANT)")
        sections.extend(standards.code_style_standards)
        sections.append("")

    standards_text = chr(10).join(sections) if sections else "No standards available"

    # Add backend-specific guidelines
    guidelines = backend.get_plan_generation_guidelines()

    # Add repo-specific context
    repo_context = ""
    if repo_config and repo_config.project_context:
        repo_context = f"\n# Project Context\n{repo_config.project_context}\n"

    # Add custom guidelines from repo config
    custom_guidelines = ""
    if repo_config and repo_config.code_generation.custom_guidelines:
        custom_guidelines = f"\n# Custom Guidelines\n{repo_config.code_generation.custom_guidelines}\n"

    return f"""# User Story
Issue: #{issue_number}
Title: {issue_title}
Description:
{issue_description or 'No description provided'}
{repo_context}
# Available Standards
{standards_text}
{guidelines}
{custom_guidelines}
# Task
Generate a step-by-step implementation plan for this issue.

IMPORTANT: When creating test-related steps, break them into multiple focused steps with 1-3 test functions each:
- {chr(10).join(f"* {step}" for step in backend.get_test_step_breakdown())}

DO NOT create a single monolithic "Create unit tests" step.

Return the plan as JSON following the schema in the system prompt."""


# ============================================================================
# Plan Generation
# ============================================================================


def generate_execution_plan(
    webhook_event: GitHubWebhookEvent,
    backend: Optional[CodeGeneratorBackend] = None,
    repo_config: Optional[RepoConfig] = None,
) -> ExecutionPlan:
    """
    Generate execution plan from GitHub issue.

    Steps:
    1. Load repo config and backend (if not provided)
    2. Query KB for standards using backend
    3. Invoke Claude to generate plan
    4. Parse plan from JSON response

    Args:
        webhook_event: GitHub webhook event
        backend: Optional code generator backend (loaded from repo config if not provided)
        repo_config: Optional repository configuration

    Returns:
        ExecutionPlan: Generated execution plan

    Raises:
        Exception: If plan generation fails
    """
    logger.info(
        f"[plan-gen] Generating plan for issue #{webhook_event.issue.number}"
    )

    # Reset summarization cost tracker for this plan
    reset_summarization_cost()

    # Step 1: Load repo config and backend if not provided
    if repo_config is None:
        github_token = get_github_token()
        repo_config = load_repo_config_from_github(
            repo_full_name=webhook_event.repository.full_name,
            branch_name=webhook_event.repository.default_branch,
            github_token=github_token,
        )

    if backend is None:
        config_dict = repo_config.model_dump() if repo_config else None
        backend = get_backend_for_repo(repo_config=config_dict)

    logger.info(f"[plan-gen] Using backend: {backend.display_name}")

    # Step 2: Query knowledge base for standards using backend
    # Pass target repo for prioritized queries (repo-specific ADRs first, then global)
    logger.info("[plan-gen] Querying knowledge base for standards")

    additional_queries = None
    required_adrs = None
    if repo_config:
        additional_queries = repo_config.code_generation.additional_kb_queries
        required_adrs = repo_config.required_adrs

    # Extract repo name from full path (e.g., "owner/repo" -> "repo")
    target_repo = webhook_event.repository.full_name

    standards = get_standards_for_backend(
        backend,
        additional_queries,
        target_repo=target_repo,
        required_adrs=required_adrs
    )

    # Also get legacy standards for backwards compatibility in ExecutionPlan
    lambda_standards = standards.handler_standards
    terraform_standards = standards.infrastructure_standards
    testing_standards = standards.testing_standards
    code_style_standards = standards.code_style_standards

    # Debug logging for standards assignment
    logger.info(f"[plan-gen] Standards counts - lambda: {len(lambda_standards)}, "
                f"terraform: {len(terraform_standards)}, testing: {len(testing_standards)}, "
                f"code_style: {len(code_style_standards)}")

    # Step 3: Build prompt using backend
    prompt = build_plan_prompt_with_backend(
        issue_number=webhook_event.issue.number,
        issue_title=webhook_event.issue.title,
        issue_description=webhook_event.issue.body,
        standards=standards,
        backend=backend,
        repo_config=repo_config,
    )

    # Get system prompt from backend
    system_prompt = backend.get_plan_generation_system_prompt()

    # Step 4: Invoke Claude
    logger.info("[plan-gen] Invoking Claude to generate plan")

    response = invoke_claude(
        prompt=prompt,
        system_prompt=system_prompt,
        temperature=0.3,
        max_tokens=4000
    )

    # Check for truncation
    if response.stop_reason == "max_tokens":
        raise Exception(
            "Plan generation response truncated. Issue may be too complex. "
            "Try breaking it into smaller issues."
        )

    # Step 5: Extract JSON from response
    try:
        plan_json = extract_json_from_response(response.text)
    except ValueError as e:
        logger.error(f"[plan-gen] Failed to extract JSON from response: {e}")
        logger.error(f"[plan-gen] Response text: {response.text[:500]}")
        raise

    # Step 6: Parse steps
    steps_data = plan_json.get("steps", [])
    if not steps_data:
        raise Exception("No steps in plan JSON")

    steps = []
    for step in steps_data:
        kb_queries = step.get("kbQueries", [])
        files_to_create = step.get("filesToCreate", [])

        # Enhance dependency file steps with backend-specific queries
        for dep_file in backend.dependency_files:
            has_dep_file = any(f.endswith(dep_file) for f in files_to_create)
            if has_dep_file:
                # Add language-specific dependency management query (generic, no ADR numbers)
                if dep_file == "requirements.txt":
                    dep_query = "Python dependency management standards"
                elif dep_file in ("pom.xml", "build.gradle"):
                    dep_query = "Java dependency management standards"
                elif dep_file == "package.json":
                    dep_query = "Node.js dependency management standards"
                else:
                    dep_query = f"{dep_file} dependency management standards"

                if dep_query not in kb_queries:
                    logger.info(
                        f"[plan-gen] Adding dependency query to step {step['stepNumber']} "
                        f"(creates {dep_file})"
                    )
                    kb_queries.append(dep_query)

        steps.append(
            PlanStep(
                step_number=step["stepNumber"],
                title=step["title"],
                description=step["description"],
                files_to_create=files_to_create,
                kb_queries=kb_queries,
                status="pending"
            )
        )

    # Step 7: Create execution plan
    branch_name = generate_branch_name(webhook_event.issue.number, webhook_event.issue.title)

    # Get summarization cost accumulated during KB queries
    summarization_cost = get_summarization_cost()

    # Calculate plan generation cost (Sonnet)
    plan_gen_cost = calculate_cost(
        response.usage.inputTokens,
        response.usage.outputTokens,
        model="sonnet"
    )

    plan = ExecutionPlan(
        issue_number=webhook_event.issue.number,
        issue_title=webhook_event.issue.title,
        issue_description=webhook_event.issue.body,
        branch_name=branch_name,
        repo_full_name=webhook_event.repository.full_name,
        steps=steps,
        lambda_standards=lambda_standards,
        terraform_standards=terraform_standards,
        testing_standards=testing_standards,
        code_style_standards=code_style_standards,
        # Store backend name for step execution
        backend_name=backend.name,
        # Store Haiku summarization cost (separate from Sonnet code gen cost)
        summarization_cost=summarization_cost if summarization_cost.total_cost_usd > 0 else None,
        # Store plan generation cost (Sonnet)
        plan_generation_cost=plan_gen_cost,
        plan_completed_at=datetime.utcnow().isoformat(),
    )

    logger.info(f"[plan-gen] Plan generated with {len(steps)} steps using {backend.display_name}")
    logger.info(
        f"[plan-gen] Plan generation cost (Sonnet): "
        f"{response.usage.inputTokens} input tokens, "
        f"{response.usage.outputTokens} output tokens"
    )
    if summarization_cost.total_cost_usd > 0:
        logger.info(
            f"[plan-gen] Standards summarization cost (Haiku): "
            f"${summarization_cost.total_cost_usd:.6f} "
            f"({summarization_cost.input_tokens} in, {summarization_cost.output_tokens} out)"
        )

    return plan


# ============================================================================
# Webhook Handler
# ============================================================================


def handle_webhook(
    webhook_event: GitHubWebhookEvent,
    plan_only: bool = False,
    customer_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Handle GitHub webhook event for approved-for-generation or approved-for-plan label.

    Flow:
    1. Generate execution plan (query KB + invoke Claude)
    2. Commit plan as markdown to branch
    3. If plan_only=False: Send first step message to SQS queue
       If plan_only=True: Create PR immediately with just the plan (skip code generation)

    Args:
        webhook_event: GitHub webhook event
        plan_only: If True, skip code generation and create PR with just the plan
        customer_id: Optional license customer ID for usage tracking in DLQ

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

    # Check if plan file already exists (re-run scenario)
    existing_plan = get_file(
        repo_full_name=plan.repo_full_name,
        file_path=plan_file_path,
        branch_name=plan.branch_name,
        github_token=github_token
    )

    if existing_plan:
        logger.info(f"[plan-gen] Plan file exists, updating: {plan_file_path}")
        update_file(
            repo_full_name=plan.repo_full_name,
            file_path=plan_file_path,
            content=plan_markdown,
            branch_name=plan.branch_name,
            commit_message=f"docs: update code generation plan for issue #{plan.issue_number}",
            github_token=github_token
        )
    else:
        commit_file(
            repo_full_name=plan.repo_full_name,
            file_path=plan_file_path,
            content=plan_markdown,
            branch_name=plan.branch_name,
            commit_message=f"docs: add code generation plan for issue #{plan.issue_number}",
            github_token=github_token
        )

    branch_url = f"https://github.com/{plan.repo_full_name}/tree/{plan.branch_name}"
    plan_url = f"https://github.com/{plan.repo_full_name}/blob/{plan.branch_name}/{plan_file_path}"

    # Plan-only mode: Create PR immediately without code generation
    if plan_only:
        logger.info("[plan-gen] Plan-only mode: Creating PR with plan (skipping code generation)")

        # Build PR body with plan summary
        pr_body = f"""## Plan for Issue #{plan.issue_number}

This PR contains the execution plan for: **{plan.issue_title}**

### Summary
{plan.issue_description or 'No description provided'}

### Execution Plan
This plan was generated using the `approved-for-plan` label (plan-only mode).
Code generation was skipped - only the plan document was created.

**Plan file:** [{plan_file_path}]({plan_url})

**Total steps:** {len(plan.steps)}

### Steps Overview
{chr(10).join(f"- **Step {step.step_number}:** {step.title}" for step in plan.steps)}

---
*Generated by OutcomeOps AI Assist (plan-only mode)*
"""

        # Track usage for plan-only mode (work was done regardless of PR creation success)
        try:
            from outcomeops_license import track_pr_generated
            track_pr_generated(
                repo=plan.repo_full_name,
                issue_number=plan.issue_number
            )
            logger.info(f"[plan-gen] Tracked usage for plan generation: {plan.repo_full_name}")
        except ImportError:
            logger.debug("[plan-gen] License layer not available, skipping usage tracking")
        except Exception as e:
            # Non-blocking - usage tracking failures shouldn't stop plan generation
            logger.warning(f"[plan-gen] Failed to track usage: {e}")

        try:
            pr_result = create_pull_request(
                repo_full_name=plan.repo_full_name,
                title=f"[Plan] {plan.issue_title} (#{plan.issue_number})",
                body=pr_body,
                head_branch=plan.branch_name,
                base_branch=webhook_event.repository.default_branch,
                github_token=github_token
            )

            logger.info(f"[plan-gen] PR created: {pr_result.get('html_url')}")

            return {
                "success": True,
                "message": "Plan-only PR created (no code generation)",
                "issue_number": plan.issue_number,
                "branch_name": plan.branch_name,
                "branch_url": branch_url,
                "plan_url": plan_url,
                "pr_url": pr_result.get("html_url"),
                "pr_number": pr_result.get("number"),
                "total_steps": len(plan.steps),
                "plan_only": True
            }

        except Exception as e:
            logger.error(f"[plan-gen] Failed to create PR: {e}")
            # Return success for plan generation even if PR creation fails
            return {
                "success": True,
                "message": "Plan created but PR creation failed",
                "issue_number": plan.issue_number,
                "branch_name": plan.branch_name,
                "branch_url": branch_url,
                "plan_url": plan_url,
                "total_steps": len(plan.steps),
                "plan_only": True,
                "pr_error": str(e)
            }

    # Normal mode: Send first step message to SQS for code generation
    if plan.steps:
        first_step_message = StepExecutionMessage(
            issue_number=plan.issue_number,
            issue_title=plan.issue_title,
            issue_description=plan.issue_description,
            repo_full_name=plan.repo_full_name,
            branch_name=plan.branch_name,
            current_step=1,
            total_steps=len(plan.steps),
            base_branch=webhook_event.repository.default_branch,
            customer_id=customer_id
        )

        logger.info("[plan-gen] Sending first step message to SQS")
        send_step_message(first_step_message)

    return {
        "success": True,
        "message": "Code generation plan created",
        "issue_number": plan.issue_number,
        "branch_name": plan.branch_name,
        "branch_url": branch_url,
        "plan_url": plan_url,
        "total_steps": len(plan.steps)
    }
