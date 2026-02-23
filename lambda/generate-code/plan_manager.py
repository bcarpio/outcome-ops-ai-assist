"""
Plan management for code generation.

Handles serialization, parsing, and updating of execution plans stored as markdown.
"""

import json
import logging
import re
from datetime import datetime
from typing import List, Optional

from models import ExecutionPlan, PlanStep, TokenUsage

logger = logging.getLogger()


# ============================================================================
# Standards Serialization Helpers
# ============================================================================

# Delimiter used to separate multi-line standards in markdown
STANDARDS_DELIMITER = "\n---STANDARD---\n"


def _serialize_standards(standards: List[str]) -> str:
    """
    Serialize a list of standards to markdown format.

    Each standard can be multi-line (contains KB query + answer).
    We use a special delimiter to separate standards so they can be
    parsed back correctly.

    Args:
        standards: List of standard strings (may contain newlines)

    Returns:
        Markdown-formatted standards block
    """
    if not standards:
        return "None"

    # Join all standards with a clear delimiter
    return STANDARDS_DELIMITER.join(standards)


def _parse_standards(text: str) -> List[str]:
    """
    Parse standards from markdown format back to a list.

    Args:
        text: Text between section header and next section

    Returns:
        List of standard strings
    """
    if not text or text.strip() == "None":
        return []

    # Split by delimiter
    if STANDARDS_DELIMITER.strip() in text:
        standards = text.split(STANDARDS_DELIMITER.strip())
        return [s.strip() for s in standards if s.strip()]

    # Legacy format: try to parse as list items (for backwards compatibility)
    standards = []
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("- ") and line != "- None":
            standards.append(line[2:])
    return standards


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
    # Debug: log standards counts at serialization time
    logger.info(f"[plan-serialize] Standards in plan - lambda: {len(plan.lambda_standards)}, "
                f"terraform: {len(plan.terraform_standards)}, testing: {len(plan.testing_standards)}, "
                f"code_style: {len(plan.code_style_standards)}")

    # Pre-serialize and log for debugging
    lambda_ser = _serialize_standards(plan.lambda_standards)
    terraform_ser = _serialize_standards(plan.terraform_standards)
    testing_ser = _serialize_standards(plan.testing_standards)
    code_style_ser = _serialize_standards(plan.code_style_standards)
    logger.info(f"[plan-serialize] Serialized lengths - lambda: {len(lambda_ser)}, terraform: {len(terraform_ser)}")
    logger.info(f"[plan-serialize] Lambda first 100: {lambda_ser[:100]!r}")

    lines = [
        f"# Code Generation Plan",
        f"",
        f"**Issue:** #{plan.issue_number} - {plan.issue_title}",
        f"**Branch:** `{plan.branch_name}`",
        f"**Repository:** {plan.repo_full_name}",
        f"**Backend:** {plan.backend_name}",
        f"**Created:** {plan.created_at}",
        f"",
        f"## Issue Description",
        f"",
        f"{plan.issue_description or 'No description provided'}",
        f"",
        f"## Cached Standards",
        f"",
        f"<!-- Standards retrieved during plan generation (avoid re-querying) -->",
        f"<!-- STANDARDS_BEGIN -->",
        f"",
        f"### Lambda Standards",
        f"",
        lambda_ser,
        f"",
        f"### Terraform Standards",
        f"",
        terraform_ser,
        f"",
        f"### Testing Standards",
        f"",
        testing_ser,
        f"",
        f"### Code Style Standards",
        f"",
        code_style_ser,
        f"",
        f"<!-- STANDARDS_END -->",
        f"",
    ]

    # Plan generation section (before Implementation Steps)
    if plan.plan_generation_cost or plan.plan_completed_at:
        lines.append(f"## Plan Generation")
        lines.append(f"")
        if plan.plan_completed_at:
            lines.append(f"**Completed:** {plan.plan_completed_at}")
        if plan.plan_generation_cost:
            lines.append(
                f"**Cost:** ${plan.plan_generation_cost.total_cost_usd:.6f} "
                f"({plan.plan_generation_cost.input_tokens:,} input tokens, "
                f"{plan.plan_generation_cost.output_tokens:,} output tokens)"
            )
        lines.append(f"")

    lines.extend([
        f"## Implementation Steps",
        f""
    ])

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

        # Debug context (collapsible details for troubleshooting)
        if step.debug_context:
            lines.append(f"<details>")
            lines.append(f"<summary>Debug Context (click to expand)</summary>")
            lines.append(f"")
            lines.append(f"**Prompt Size:** {step.debug_context.get('prompt_chars', 'N/A')} chars")
            lines.append(f"**System Prompt Size:** {step.debug_context.get('system_prompt_chars', 'N/A')} chars")
            lines.append(f"")

            # Cached standards counts
            standards_counts = step.debug_context.get('cached_standards_counts', {})
            lines.append(f"**Cached Standards Counts:**")
            lines.append(f"- Lambda: {standards_counts.get('lambda', 0)}")
            lines.append(f"- Terraform: {standards_counts.get('terraform', 0)}")
            lines.append(f"- Testing: {standards_counts.get('testing', 0)}")
            lines.append(f"- Code Style: {standards_counts.get('code_style', 0)}")
            lines.append(f"")

            # Existing files context
            existing_files = step.debug_context.get('existing_files', [])
            if existing_files:
                lines.append(f"**Existing Files Provided as Context:**")
                for f in existing_files:
                    lines.append(f"- `{f}`")
                lines.append(f"")

            # Previous step files context
            prev_files = step.debug_context.get('previous_step_files', [])
            if prev_files:
                lines.append(f"**Previous Step Files Provided as Context:**")
                for f in prev_files:
                    lines.append(f"- `{f}`")
                lines.append(f"")

            # Step-specific KB results
            kb_results = step.debug_context.get('kb_results', [])
            if kb_results:
                lines.append(f"**Step-Specific KB Results ({len(kb_results)} items):**")
                lines.append(f"```")
                for i, result in enumerate(kb_results):
                    # Truncate long results
                    preview = result[:300] + "..." if len(result) > 300 else result
                    lines.append(f"[{i+1}] {preview}")
                    lines.append(f"")
                lines.append(f"```")

            # Terraform standards preview (for version debugging)
            tf_preview = step.debug_context.get('terraform_standards_preview', '')
            if tf_preview and tf_preview != 'None':
                lines.append(f"**Terraform Standards Preview (first 500 chars):**")
                lines.append(f"```")
                lines.append(tf_preview)
                lines.append(f"```")

            lines.append(f"</details>")
            lines.append(f"")

        lines.append(f"---")
        lines.append(f"")

    # Cost summary section
    has_any_cost = plan.total_cost or plan.summarization_cost or plan.plan_generation_cost
    if has_any_cost:
        lines.append(f"## Cost Summary")
        lines.append(f"")

        # Summarization cost (Haiku)
        if plan.summarization_cost:
            lines.append(f"### Standards Summarization (Haiku)")
            lines.append(f"")
            lines.append(f"**Cost:** ${plan.summarization_cost.total_cost_usd:.6f}")
            lines.append(f"**Input Tokens:** {plan.summarization_cost.input_tokens:,}")
            lines.append(f"**Output Tokens:** {plan.summarization_cost.output_tokens:,}")
            lines.append(f"")

        # Plan generation cost (Sonnet)
        if plan.plan_generation_cost:
            lines.append(f"### Plan Generation (Sonnet)")
            lines.append(f"")
            lines.append(f"**Cost:** ${plan.plan_generation_cost.total_cost_usd:.6f}")
            lines.append(f"**Input Tokens:** {plan.plan_generation_cost.input_tokens:,}")
            lines.append(f"**Output Tokens:** {plan.plan_generation_cost.output_tokens:,}")
            lines.append(f"")

        # Code generation cost (Sonnet) - accumulated from step execution
        if plan.total_cost:
            lines.append(f"### Code Generation (Sonnet)")
            lines.append(f"")
            lines.append(f"**Cost:** ${plan.total_cost.total_cost_usd:.6f}")
            lines.append(f"**Input Tokens:** {plan.total_cost.input_tokens:,}")
            lines.append(f"**Output Tokens:** {plan.total_cost.output_tokens:,}")
            lines.append(f"")

        # Grand total
        summarization_usd = plan.summarization_cost.total_cost_usd if plan.summarization_cost else 0.0
        plan_gen_usd = plan.plan_generation_cost.total_cost_usd if plan.plan_generation_cost else 0.0
        codegen_usd = plan.total_cost.total_cost_usd if plan.total_cost else 0.0
        grand_total = summarization_usd + plan_gen_usd + codegen_usd

        # Show grand total if we have costs from multiple phases
        cost_count = sum([1 for c in [summarization_usd, plan_gen_usd, codegen_usd] if c > 0])
        if cost_count > 1:
            lines.append(f"### Grand Total")
            lines.append(f"")
            lines.append(f"**Total Cost:** ${grand_total:.6f}")

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
    backend_name = None
    created_at = None
    issue_description = None
    lambda_standards = []
    terraform_standards = []
    testing_standards = []
    code_style_standards = []
    summarization_cost = None
    plan_generation_cost = None
    plan_completed_at = None
    total_cost = None

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

        elif line.startswith("**Backend:**"):
            backend_name = line.split("**Backend:**")[1].strip()

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

        elif line.startswith("### Lambda Standards"):
            # Extract lambda standards - only stop at next standards section or end marker
            # NOTE: Standards content may contain ## headers, so we only stop at specific markers
            content_lines = []
            for j in range(i + 2, len(lines)):  # Skip header and blank line
                if lines[j].startswith("### Terraform Standards"):
                    break
                if lines[j].startswith("<!-- STANDARDS_END"):
                    break
                content_lines.append(lines[j])
            lambda_standards = _parse_standards("\n".join(content_lines))

        elif line.startswith("### Terraform Standards"):
            # Extract terraform standards - only stop at next standards section or end marker
            content_lines = []
            for j in range(i + 2, len(lines)):
                if lines[j].startswith("### Testing Standards"):
                    break
                if lines[j].startswith("<!-- STANDARDS_END"):
                    break
                content_lines.append(lines[j])
            terraform_standards = _parse_standards("\n".join(content_lines))

        elif line.startswith("### Testing Standards"):
            # Extract testing standards - only stop at next standards section or end marker
            content_lines = []
            for j in range(i + 2, len(lines)):
                if lines[j].startswith("### Code Style Standards"):
                    break
                if lines[j].startswith("<!-- STANDARDS_END"):
                    break
                content_lines.append(lines[j])
            testing_standards = _parse_standards("\n".join(content_lines))

        elif line.startswith("### Code Style Standards"):
            # Extract code style standards - only stop at end marker or Implementation Steps
            content_lines = []
            for j in range(i + 2, len(lines)):
                if lines[j].startswith("<!-- STANDARDS_END"):
                    break
                if lines[j].startswith("## Implementation Steps"):
                    break
                if lines[j].startswith("## Plan Generation"):
                    break
                content_lines.append(lines[j])
            code_style_standards = _parse_standards("\n".join(content_lines))

        # Parse Plan Generation section (before Implementation Steps)
        elif line.startswith("## Plan Generation"):
            for j in range(i + 1, min(i + 10, len(lines))):
                if lines[j].startswith("## "):  # Next section
                    break
                plan_line = lines[j]
                if plan_line.startswith("**Completed:**"):
                    plan_completed_at = plan_line.split("**Completed:**")[1].strip()
                elif plan_line.startswith("**Cost:**"):
                    # Parse format: **Cost:** $0.028395 (7,865 input tokens, 320 output tokens)
                    cost_match = re.search(r'\$([0-9.]+).*\(([0-9,]+) input tokens, ([0-9,]+) output tokens\)', plan_line)
                    if cost_match:
                        plan_generation_cost = TokenUsage(
                            input_tokens=int(cost_match.group(2).replace(",", "")),
                            output_tokens=int(cost_match.group(3).replace(",", "")),
                            total_cost_usd=float(cost_match.group(1))
                        )

        # Parse Cost Summary section
        elif line.startswith("### Standards Summarization (Haiku)"):
            # Parse Haiku summarization cost
            for j in range(i + 1, min(i + 10, len(lines))):
                if lines[j].startswith("### "):  # Next section
                    break
                cost_line = lines[j]
                if cost_line.startswith("**Cost:**"):
                    cost_match = re.search(r'\$([0-9.]+)', cost_line)
                    if cost_match:
                        if summarization_cost is None:
                            summarization_cost = TokenUsage(total_cost_usd=float(cost_match.group(1)))
                        else:
                            summarization_cost = TokenUsage(
                                input_tokens=summarization_cost.input_tokens,
                                output_tokens=summarization_cost.output_tokens,
                                total_cost_usd=float(cost_match.group(1))
                            )
                elif cost_line.startswith("**Input Tokens:**"):
                    tokens_match = re.search(r'([0-9,]+)', cost_line)
                    if tokens_match:
                        input_tokens = int(tokens_match.group(1).replace(",", ""))
                        if summarization_cost is None:
                            summarization_cost = TokenUsage(input_tokens=input_tokens)
                        else:
                            summarization_cost = TokenUsage(
                                input_tokens=input_tokens,
                                output_tokens=summarization_cost.output_tokens,
                                total_cost_usd=summarization_cost.total_cost_usd
                            )
                elif cost_line.startswith("**Output Tokens:**"):
                    tokens_match = re.search(r'([0-9,]+)', cost_line)
                    if tokens_match:
                        output_tokens = int(tokens_match.group(1).replace(",", ""))
                        if summarization_cost is None:
                            summarization_cost = TokenUsage(output_tokens=output_tokens)
                        else:
                            summarization_cost = TokenUsage(
                                input_tokens=summarization_cost.input_tokens,
                                output_tokens=output_tokens,
                                total_cost_usd=summarization_cost.total_cost_usd
                            )

        elif line.startswith("### Plan Generation (Sonnet)"):
            # Parse Sonnet plan generation cost from Cost Summary section
            for j in range(i + 1, min(i + 10, len(lines))):
                if lines[j].startswith("### "):  # Next section
                    break
                cost_line = lines[j]
                if cost_line.startswith("**Cost:**"):
                    cost_match = re.search(r'\$([0-9.]+)', cost_line)
                    if cost_match:
                        if plan_generation_cost is None:
                            plan_generation_cost = TokenUsage(total_cost_usd=float(cost_match.group(1)))
                        else:
                            plan_generation_cost = TokenUsage(
                                input_tokens=plan_generation_cost.input_tokens,
                                output_tokens=plan_generation_cost.output_tokens,
                                total_cost_usd=float(cost_match.group(1))
                            )
                elif cost_line.startswith("**Input Tokens:**"):
                    tokens_match = re.search(r'([0-9,]+)', cost_line)
                    if tokens_match:
                        input_tokens = int(tokens_match.group(1).replace(",", ""))
                        if plan_generation_cost is None:
                            plan_generation_cost = TokenUsage(input_tokens=input_tokens)
                        else:
                            plan_generation_cost = TokenUsage(
                                input_tokens=input_tokens,
                                output_tokens=plan_generation_cost.output_tokens,
                                total_cost_usd=plan_generation_cost.total_cost_usd
                            )
                elif cost_line.startswith("**Output Tokens:**"):
                    tokens_match = re.search(r'([0-9,]+)', cost_line)
                    if tokens_match:
                        output_tokens = int(tokens_match.group(1).replace(",", ""))
                        if plan_generation_cost is None:
                            plan_generation_cost = TokenUsage(output_tokens=output_tokens)
                        else:
                            plan_generation_cost = TokenUsage(
                                input_tokens=plan_generation_cost.input_tokens,
                                output_tokens=output_tokens,
                                total_cost_usd=plan_generation_cost.total_cost_usd
                            )

        elif line.startswith("### Code Generation (Sonnet)"):
            # Parse Sonnet code generation cost (total_cost is calculated from steps,
            # but we also store it here for plans that were updated externally)
            for j in range(i + 1, min(i + 10, len(lines))):
                if lines[j].startswith("### "):  # Next section
                    break
                cost_line = lines[j]
                if cost_line.startswith("**Cost:**"):
                    cost_match = re.search(r'\$([0-9.]+)', cost_line)
                    if cost_match:
                        if total_cost is None:
                            total_cost = TokenUsage(total_cost_usd=float(cost_match.group(1)))
                        else:
                            total_cost = TokenUsage(
                                input_tokens=total_cost.input_tokens,
                                output_tokens=total_cost.output_tokens,
                                total_cost_usd=float(cost_match.group(1))
                            )
                elif cost_line.startswith("**Input Tokens:**"):
                    tokens_match = re.search(r'([0-9,]+)', cost_line)
                    if tokens_match:
                        input_tokens = int(tokens_match.group(1).replace(",", ""))
                        if total_cost is None:
                            total_cost = TokenUsage(input_tokens=input_tokens)
                        else:
                            total_cost = TokenUsage(
                                input_tokens=input_tokens,
                                output_tokens=total_cost.output_tokens,
                                total_cost_usd=total_cost.total_cost_usd
                            )
                elif cost_line.startswith("**Output Tokens:**"):
                    tokens_match = re.search(r'([0-9,]+)', cost_line)
                    if tokens_match:
                        output_tokens = int(tokens_match.group(1).replace(",", ""))
                        if total_cost is None:
                            total_cost = TokenUsage(output_tokens=output_tokens)
                        else:
                            total_cost = TokenUsage(
                                input_tokens=total_cost.input_tokens,
                                output_tokens=output_tokens,
                                total_cost_usd=total_cost.total_cost_usd
                            )

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

    # Build the plan with parsed values
    # For backend_name, use parsed value or fall back to default "python"
    # This handles legacy plans that don't have backend_name in the markdown
    plan_kwargs = {
        "issue_number": issue_number,
        "issue_title": issue_title,
        "issue_description": issue_description,
        "branch_name": branch_name,
        "repo_full_name": repo_full_name,
        "steps": steps,
        "created_at": created_at or datetime.utcnow().isoformat(),
        "lambda_standards": lambda_standards,
        "terraform_standards": terraform_standards,
        "testing_standards": testing_standards,
        "code_style_standards": code_style_standards,
    }

    # Only include backend_name if it was parsed (avoids overriding default with None)
    if backend_name:
        # Normalize backend_name - handle cases where display name was stored
        # e.g., "Python Backend" -> "python", "Python" -> "python"
        backend_name_lower = backend_name.lower().replace(" backend", "").strip()
        plan_kwargs["backend_name"] = backend_name_lower

    # Include summarization cost if parsed from Cost Summary section
    if summarization_cost:
        plan_kwargs["summarization_cost"] = summarization_cost

    # Include plan generation cost if parsed
    if plan_generation_cost:
        plan_kwargs["plan_generation_cost"] = plan_generation_cost
    if plan_completed_at:
        plan_kwargs["plan_completed_at"] = plan_completed_at

    # Include total_cost if parsed (note: update_step_in_plan recalculates from steps)
    if total_cost:
        plan_kwargs["total_cost"] = total_cost

    return ExecutionPlan(**plan_kwargs)


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
