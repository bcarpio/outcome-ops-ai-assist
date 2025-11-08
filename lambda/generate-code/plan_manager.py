"""
Plan management for code generation.

Handles serialization, parsing, and updating of execution plans stored as markdown.
"""

import json
import logging
import re
from datetime import datetime
from typing import Optional

from models import ExecutionPlan, PlanStep, TokenUsage

logger = logging.getLogger()


# ============================================================================
# Plan Serialization
# ============================================================================


def serialize_plan_to_markdown(plan: ExecutionPlan) -> str:
    """
    Convert execution plan to markdown format.

    The markdown includes:
    - Plan metadata (issue, branch, created date)
    - Step-by-step breakdown with status tracking
    - Cost tracking per step and total

    Args:
        plan: Execution plan to serialize

    Returns:
        str: Markdown-formatted plan
    """
    lines = [
        f"# Code Generation Plan",
        f"",
        f"**Issue:** #{plan.issue_number} - {plan.issue_title}",
        f"**Branch:** `{plan.branch_name}`",
        f"**Repository:** {plan.repo_full_name}",
        f"**Created:** {plan.created_at}",
        f"",
        f"## Issue Description",
        f"",
        f"{plan.issue_description or 'No description provided'}",
        f"",
        f"## Implementation Steps",
        f""
    ]

    for step in plan.steps:
        # Step header with status emoji
        status_emoji = {
            "pending": "⏳",
            "in_progress": "🔄",
            "completed": "✅",
            "failed": "❌"
        }.get(step.status, "❓")

        lines.append(f"### Step {step.step_number}: {step.title} {status_emoji}")
        lines.append(f"")
        lines.append(f"**Status:** {step.status}")
        lines.append(f"**Description:** {step.description}")
        lines.append(f"")

        # Files to create
        if step.files_to_create:
            lines.append(f"**Files:**")
            for file_path in step.files_to_create:
                lines.append(f"- `{file_path}`")
            lines.append(f"")

        # KB queries
        if step.kb_queries:
            lines.append(f"**KB Queries:**")
            for query in step.kb_queries:
                lines.append(f"- {query}")
            lines.append(f"")

        # Completed timestamp
        if step.completed_at:
            lines.append(f"**Completed:** {step.completed_at}")
            lines.append(f"")

        # Error
        if step.error:
            lines.append(f"**Error:** {step.error}")
            lines.append(f"")

        # Cost
        if step.cost:
            lines.append(
                f"**Cost:** ${step.cost.total_cost_usd:.6f} "
                f"({step.cost.input_tokens} input tokens, {step.cost.output_tokens} output tokens)"
            )
            lines.append(f"")

        lines.append(f"---")
        lines.append(f"")

    # Total cost summary
    if plan.total_cost:
        lines.append(f"## Total Cost")
        lines.append(f"")
        lines.append(f"**Total:** ${plan.total_cost.total_cost_usd:.6f}")
        lines.append(f"**Input Tokens:** {plan.total_cost.input_tokens:,}")
        lines.append(f"**Output Tokens:** {plan.total_cost.output_tokens:,}")

    return "\n".join(lines)


# ============================================================================
# Plan Parsing
# ============================================================================


def parse_plan_from_markdown(markdown: str) -> ExecutionPlan:
    """
    Parse execution plan from markdown format.

    Args:
        markdown: Markdown-formatted plan

    Returns:
        ExecutionPlan: Parsed execution plan

    Raises:
        ValueError: If markdown cannot be parsed
    """
    lines = markdown.split("\n")

    # Extract metadata
    issue_number = None
    issue_title = None
    branch_name = None
    repo_full_name = None
    created_at = None
    issue_description = None

    for i, line in enumerate(lines):
        if line.startswith("**Issue:**"):
            match = re.search(r'#(\d+)\s*-\s*(.+)', line)
            if match:
                issue_number = int(match.group(1))
                issue_title = match.group(2)

        elif line.startswith("**Branch:**"):
            match = re.search(r'`([^`]+)`', line)
            if match:
                branch_name = match.group(1)

        elif line.startswith("**Repository:**"):
            repo_full_name = line.split("**Repository:**")[1].strip()

        elif line.startswith("**Created:**"):
            created_at = line.split("**Created:**")[1].strip()

        elif line.startswith("## Issue Description"):
            # Extract issue description (multi-line until next ##)
            desc_lines = []
            for j in range(i + 2, len(lines)):  # Skip header and blank line
                if lines[j].startswith("##"):
                    break
                desc_lines.append(lines[j])
            issue_description = "\n".join(desc_lines).strip()

    if not all([issue_number, issue_title, branch_name, repo_full_name]):
        raise ValueError("Missing required metadata in plan markdown")

    # Extract steps
    steps = []
    current_step = None

    for line in lines:
        # Step header
        step_match = re.search(r'^### Step (\d+): (.+?)(?:\s+[⏳🔄✅❌❓])?$', line)
        if step_match:
            if current_step:
                steps.append(current_step)

            step_number = int(step_match.group(1))
            title = step_match.group(2).strip()

            current_step = PlanStep(
                step_number=step_number,
                title=title,
                description="",
                files_to_create=[],
                kb_queries=[]
            )
            continue

        if not current_step:
            continue

        # Status
        if line.startswith("**Status:**"):
            status = line.split("**Status:**")[1].strip()
            current_step.status = status

        # Description
        elif line.startswith("**Description:**"):
            current_step.description = line.split("**Description:**")[1].strip()

        # Files
        elif line.startswith("- `") and "**Files:**" in "\n".join(lines[max(0, lines.index(line) - 5):lines.index(line)]):
            file_path = re.search(r'`([^`]+)`', line)
            if file_path:
                current_step.files_to_create.append(file_path.group(1))

        # KB Queries
        elif line.startswith("- ") and "**KB Queries:**" in "\n".join(lines[max(0, lines.index(line) - 5):lines.index(line)]):
            query = line[2:].strip()
            current_step.kb_queries.append(query)

        # Completed
        elif line.startswith("**Completed:**"):
            current_step.completed_at = line.split("**Completed:**")[1].strip()

        # Error
        elif line.startswith("**Error:**"):
            current_step.error = line.split("**Error:**")[1].strip()

        # Cost
        elif line.startswith("**Cost:**"):
            cost_match = re.search(r'\$([0-9.]+).*\((\d+) input tokens, (\d+) output tokens\)', line)
            if cost_match:
                current_step.cost = TokenUsage(
                    input_tokens=int(cost_match.group(2)),
                    output_tokens=int(cost_match.group(3)),
                    total_cost_usd=float(cost_match.group(1))
                )

    # Add last step
    if current_step:
        steps.append(current_step)

    return ExecutionPlan(
        issue_number=issue_number,
        issue_title=issue_title,
        issue_description=issue_description,
        branch_name=branch_name,
        repo_full_name=repo_full_name,
        steps=steps,
        created_at=created_at or datetime.utcnow().isoformat()
    )


# ============================================================================
# Plan Updates
# ============================================================================


def update_step_in_plan(
    plan: ExecutionPlan,
    step_number: int,
    status: Optional[str] = None,
    error: Optional[str] = None,
    cost: Optional[TokenUsage] = None
) -> ExecutionPlan:
    """
    Update a step's status in the execution plan.

    Args:
        plan: Execution plan to update
        step_number: Step number to update (1-indexed)
        status: New status (pending, in_progress, completed, failed)
        error: Error message (if failed)
        cost: Token usage and cost (if completed)

    Returns:
        ExecutionPlan: Updated plan

    Raises:
        ValueError: If step not found
    """
    step = next((s for s in plan.steps if s.step_number == step_number), None)

    if not step:
        raise ValueError(f"Step {step_number} not found in plan")

    if status:
        step.status = status

    if error:
        step.error = error

    if cost:
        step.cost = cost

    if status == "completed":
        step.completed_at = datetime.utcnow().isoformat()

    # Recalculate total cost
    total_input = sum(s.cost.input_tokens for s in plan.steps if s.cost)
    total_output = sum(s.cost.output_tokens for s in plan.steps if s.cost)
    total_cost_usd = sum(s.cost.total_cost_usd for s in plan.steps if s.cost)

    plan.total_cost = TokenUsage(
        input_tokens=total_input,
        output_tokens=total_output,
        total_cost_usd=total_cost_usd
    )

    return plan
