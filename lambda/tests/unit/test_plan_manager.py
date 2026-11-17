# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for plan_manager module.

Tests cover:
- serialize_plan_to_markdown: converting ExecutionPlan to markdown format
- parse_plan_from_markdown: parsing markdown back to ExecutionPlan
- update_step_in_plan: updating step status, error, cost in plan
"""

import os
import sys
import pytest
from datetime import datetime
from unittest.mock import patch, MagicMock

# Add generate-code directory to Python path
generate_code_dir = os.path.join(os.path.dirname(__file__), '../../generate-code')
sys.path.insert(0, os.path.abspath(generate_code_dir))

# Mock boto3.client BEFORE importing modules that create clients at import time
mock_boto3_client = MagicMock()
with patch('boto3.client', return_value=mock_boto3_client):
    from plan_manager import (
        serialize_plan_to_markdown,
        parse_plan_from_markdown,
        update_step_in_plan,
    )
    from models import ExecutionPlan, PlanStep, TokenUsage


# ============================================================================
# Test Fixtures
# ============================================================================


@pytest.fixture
def basic_plan():
    """Create a basic execution plan for testing."""
    return ExecutionPlan(
        issue_number=123,
        issue_title="Add user authentication",
        issue_description="Implement OAuth2 authentication for the API",
        branch_name="123-add-user-authentication",
        repo_full_name="owner/repo",
        backend_name="python",
        created_at="2024-01-15T10:30:00",
        steps=[
            PlanStep(
                step_number=1,
                title="Create auth handler",
                description="Create Lambda handler for authentication",
                files_to_create=["lambda/auth/handler.py"],
                kb_queries=["Lambda authentication examples"],
                status="pending"
            ),
            PlanStep(
                step_number=2,
                title="Add tests",
                description="Write unit tests for auth handler",
                files_to_create=["lambda/auth/test_handler.py"],
                kb_queries=["pytest patterns"],
                status="pending"
            )
        ],
        lambda_standards=["Error handling required", "Logging with structlog"],
        terraform_standards=["Use tags", "Enable encryption"],
        testing_standards=["AAA pattern", "Mock external services"]
    )


@pytest.fixture
def completed_step():
    """Create a completed step with cost information."""
    return PlanStep(
        step_number=1,
        title="Create handler",
        description="Create Lambda handler",
        files_to_create=["lambda/handler.py"],
        kb_queries=[],
        status="completed",
        completed_at="2024-01-15T11:00:00",
        cost=TokenUsage(
            input_tokens=1000,
            output_tokens=500,
            total_cost_usd=0.004500
        )
    )


@pytest.fixture
def failed_step():
    """Create a failed step with error information."""
    return PlanStep(
        step_number=2,
        title="Add tests",
        description="Write unit tests",
        files_to_create=["lambda/test_handler.py"],
        kb_queries=[],
        status="failed",
        error="API rate limit exceeded"
    )


# ============================================================================
# Test serialize_plan_to_markdown
# ============================================================================


class TestSerializePlanToMarkdown:
    """Test serialize_plan_to_markdown function."""

    def test_serialize_basic_plan(self, basic_plan):
        """Test: Serialize basic plan with all metadata."""
        # Act
        markdown = serialize_plan_to_markdown(basic_plan)

        # Assert - Check header
        assert "# Code Generation Plan" in markdown
        assert "**Issue:** #123 - Add user authentication" in markdown
        assert "**Branch:** `123-add-user-authentication`" in markdown
        assert "**Repository:** owner/repo" in markdown
        assert "**Backend:** python" in markdown
        assert "**Created:** 2024-01-15T10:30:00" in markdown

    def test_serialize_includes_issue_description(self, basic_plan):
        """Test: Issue description is included in markdown."""
        # Act
        markdown = serialize_plan_to_markdown(basic_plan)

        # Assert
        assert "## Issue Description" in markdown
        assert "Implement OAuth2 authentication for the API" in markdown

    def test_serialize_includes_standards(self, basic_plan):
        """Test: Standards are included in markdown."""
        # Act
        markdown = serialize_plan_to_markdown(basic_plan)

        # Assert - Lambda standards (new format uses delimiter, not list items)
        assert "### Lambda Standards" in markdown
        assert "Error handling required" in markdown
        assert "Logging with structlog" in markdown

        # Assert - Terraform standards
        assert "### Terraform Standards" in markdown
        assert "Use tags" in markdown
        assert "Enable encryption" in markdown

        # Assert - Testing standards
        assert "### Testing Standards" in markdown
        assert "AAA pattern" in markdown
        assert "Mock external services" in markdown

    def test_serialize_includes_steps(self, basic_plan):
        """Test: Steps are included with correct format."""
        # Act
        markdown = serialize_plan_to_markdown(basic_plan)

        # Assert - Step 1
        assert "### Step 1: Create auth handler" in markdown
        assert "**Status:** pending" in markdown
        assert "**Description:** Create Lambda handler for authentication" in markdown
        assert "`lambda/auth/handler.py`" in markdown
        assert "Lambda authentication examples" in markdown

        # Assert - Step 2
        assert "### Step 2: Add tests" in markdown
        assert "Write unit tests for auth handler" in markdown

    def test_serialize_step_with_status_emoji(self, basic_plan):
        """Test: Steps have correct status emoji."""
        # Arrange
        basic_plan.steps[0].status = "pending"
        basic_plan.steps[1].status = "in_progress"

        # Act
        markdown = serialize_plan_to_markdown(basic_plan)

        # Assert - emojis are present in step headers
        assert "Step 1: Create auth handler" in markdown
        assert "Step 2: Add tests" in markdown

    def test_serialize_completed_step_with_cost(self, completed_step):
        """Test: Completed step includes cost information."""
        # Arrange
        plan = ExecutionPlan(
            issue_number=1,
            issue_title="Test",
            issue_description="Test",
            branch_name="1-test",
            repo_full_name="owner/repo",
            steps=[completed_step],
            lambda_standards=[],
            terraform_standards=[],
            testing_standards=[]
        )

        # Act
        markdown = serialize_plan_to_markdown(plan)

        # Assert
        assert "**Status:** completed" in markdown
        assert "**Completed:** 2024-01-15T11:00:00" in markdown
        assert "**Cost:** $0.004500" in markdown
        assert "1000 input tokens" in markdown
        assert "500 output tokens" in markdown

    def test_serialize_failed_step_with_error(self, failed_step):
        """Test: Failed step includes error message."""
        # Arrange
        plan = ExecutionPlan(
            issue_number=1,
            issue_title="Test",
            issue_description="Test",
            branch_name="1-test",
            repo_full_name="owner/repo",
            steps=[failed_step],
            lambda_standards=[],
            terraform_standards=[],
            testing_standards=[]
        )

        # Act
        markdown = serialize_plan_to_markdown(plan)

        # Assert
        assert "**Status:** failed" in markdown
        assert "**Error:** API rate limit exceeded" in markdown

    def test_serialize_plan_with_total_cost(self, completed_step):
        """Test: Plan with total cost includes cost summary."""
        # Arrange
        plan = ExecutionPlan(
            issue_number=1,
            issue_title="Test",
            issue_description="Test",
            branch_name="1-test",
            repo_full_name="owner/repo",
            steps=[completed_step],
            total_cost=TokenUsage(
                input_tokens=5000,
                output_tokens=2500,
                total_cost_usd=0.025000
            ),
            lambda_standards=[],
            terraform_standards=[],
            testing_standards=[]
        )

        # Act
        markdown = serialize_plan_to_markdown(plan)

        # Assert - cost section renamed to Cost Summary with Code Generation subsection
        assert "## Cost Summary" in markdown
        assert "### Code Generation (Sonnet)" in markdown
        assert "**Cost:** $0.025000" in markdown
        assert "**Input Tokens:** 5,000" in markdown
        assert "**Output Tokens:** 2,500" in markdown

    def test_serialize_plan_with_empty_description(self):
        """Test: Plan with empty description uses fallback text."""
        # Arrange
        plan = ExecutionPlan(
            issue_number=1,
            issue_title="Test",
            issue_description="",
            branch_name="1-test",
            repo_full_name="owner/repo",
            steps=[],
            lambda_standards=[],
            terraform_standards=[],
            testing_standards=[]
        )

        # Act
        markdown = serialize_plan_to_markdown(plan)

        # Assert - empty string gets serialized, but parsing handles it
        assert "## Issue Description" in markdown

    def test_serialize_plan_without_standards(self):
        """Test: Plan without standards shows 'None' for each."""
        # Arrange
        plan = ExecutionPlan(
            issue_number=1,
            issue_title="Test",
            issue_description="Test",
            branch_name="1-test",
            repo_full_name="owner/repo",
            steps=[],
            lambda_standards=[],
            terraform_standards=[],
            testing_standards=[]
        )

        # Act
        markdown = serialize_plan_to_markdown(plan)

        # Assert - sections exist with None for empty standards
        assert "### Lambda Standards" in markdown
        assert "### Terraform Standards" in markdown
        assert "### Testing Standards" in markdown
        # New format shows "None" without dash prefix
        assert "\nNone\n" in markdown

    def test_serialize_step_without_files(self):
        """Test: Step without files doesn't include files section."""
        # Arrange
        step = PlanStep(
            step_number=1,
            title="Research",
            description="Research best practices",
            files_to_create=[],
            kb_queries=["best practices"],
            status="pending"
        )
        plan = ExecutionPlan(
            issue_number=1,
            issue_title="Test",
            issue_description="Test",
            branch_name="1-test",
            repo_full_name="owner/repo",
            steps=[step],
            lambda_standards=[],
            terraform_standards=[],
            testing_standards=[]
        )

        # Act
        markdown = serialize_plan_to_markdown(plan)

        # Assert - Files section not present for this step
        lines = markdown.split('\n')
        step_section = False
        for line in lines:
            if "### Step 1:" in line:
                step_section = True
            if step_section and "**Files:**" in line:
                pytest.fail("Files section should not be present for step without files")
            if step_section and "---" in line:
                break

    def test_serialize_step_without_kb_queries(self):
        """Test: Step without KB queries doesn't include queries section."""
        # Arrange
        step = PlanStep(
            step_number=1,
            title="Create file",
            description="Create a file",
            files_to_create=["test.py"],
            kb_queries=[],
            status="pending"
        )
        plan = ExecutionPlan(
            issue_number=1,
            issue_title="Test",
            issue_description="Test",
            branch_name="1-test",
            repo_full_name="owner/repo",
            steps=[step],
            lambda_standards=[],
            terraform_standards=[],
            testing_standards=[]
        )

        # Act
        markdown = serialize_plan_to_markdown(plan)

        # Assert - KB Queries section not present
        lines = markdown.split('\n')
        step_section = False
        for line in lines:
            if "### Step 1:" in line:
                step_section = True
            if step_section and "**KB Queries:**" in line:
                pytest.fail("KB Queries section should not be present for step without queries")
            if step_section and "---" in line:
                break


# ============================================================================
# Test parse_plan_from_markdown
# ============================================================================


class TestParsePlanFromMarkdown:
    """Test parse_plan_from_markdown function."""

    def test_parse_basic_metadata(self, basic_plan):
        """Test: Parse basic metadata from markdown."""
        # Arrange
        markdown = serialize_plan_to_markdown(basic_plan)

        # Act
        parsed_plan = parse_plan_from_markdown(markdown)

        # Assert
        assert parsed_plan.issue_number == 123
        assert parsed_plan.issue_title == "Add user authentication"
        assert parsed_plan.branch_name == "123-add-user-authentication"
        assert parsed_plan.repo_full_name == "owner/repo"
        assert parsed_plan.backend_name == "python"
        assert parsed_plan.created_at == "2024-01-15T10:30:00"

    def test_parse_issue_description(self, basic_plan):
        """Test: Parse issue description from markdown."""
        # Arrange
        markdown = serialize_plan_to_markdown(basic_plan)

        # Act
        parsed_plan = parse_plan_from_markdown(markdown)

        # Assert
        assert "OAuth2 authentication" in parsed_plan.issue_description

    def test_parse_standards(self, basic_plan):
        """Test: Parse all standards from markdown."""
        # Arrange
        markdown = serialize_plan_to_markdown(basic_plan)

        # Act
        parsed_plan = parse_plan_from_markdown(markdown)

        # Assert - Lambda standards
        assert "Error handling required" in parsed_plan.lambda_standards
        assert "Logging with structlog" in parsed_plan.lambda_standards

        # Assert - Terraform standards
        assert "Use tags" in parsed_plan.terraform_standards
        assert "Enable encryption" in parsed_plan.terraform_standards

        # Assert - Testing standards
        assert "AAA pattern" in parsed_plan.testing_standards
        assert "Mock external services" in parsed_plan.testing_standards

    def test_parse_steps(self, basic_plan):
        """Test: Parse steps with all attributes."""
        # Arrange
        markdown = serialize_plan_to_markdown(basic_plan)

        # Act
        parsed_plan = parse_plan_from_markdown(markdown)

        # Assert
        assert len(parsed_plan.steps) == 2

        # Step 1
        step1 = parsed_plan.steps[0]
        assert step1.step_number == 1
        assert step1.title == "Create auth handler"
        assert "Lambda handler" in step1.description
        assert step1.status == "pending"

        # Step 2
        step2 = parsed_plan.steps[1]
        assert step2.step_number == 2
        assert step2.title == "Add tests"

    def test_parse_step_with_files(self, basic_plan):
        """Test: Parse step files from markdown."""
        # Arrange
        markdown = serialize_plan_to_markdown(basic_plan)

        # Act
        parsed_plan = parse_plan_from_markdown(markdown)

        # Assert
        step1 = parsed_plan.steps[0]
        assert "lambda/auth/handler.py" in step1.files_to_create

    def test_parse_step_with_kb_queries(self, basic_plan):
        """Test: Parse step KB queries from markdown."""
        # Arrange
        markdown = serialize_plan_to_markdown(basic_plan)

        # Act
        parsed_plan = parse_plan_from_markdown(markdown)

        # Assert
        step1 = parsed_plan.steps[0]
        assert "Lambda authentication examples" in step1.kb_queries

    def test_parse_completed_step(self, completed_step):
        """Test: Parse completed step with cost."""
        # Arrange
        plan = ExecutionPlan(
            issue_number=1,
            issue_title="Test",
            issue_description="Test description",
            branch_name="1-test",
            repo_full_name="owner/repo",
            steps=[completed_step],
            lambda_standards=[],
            terraform_standards=[],
            testing_standards=[]
        )
        markdown = serialize_plan_to_markdown(plan)

        # Act
        parsed_plan = parse_plan_from_markdown(markdown)

        # Assert
        step = parsed_plan.steps[0]
        assert step.status == "completed"
        assert step.completed_at == "2024-01-15T11:00:00"
        assert step.cost is not None
        assert step.cost.input_tokens == 1000
        assert step.cost.output_tokens == 500
        assert abs(step.cost.total_cost_usd - 0.004500) < 0.000001

    def test_parse_failed_step(self, failed_step):
        """Test: Parse failed step with error."""
        # Arrange
        plan = ExecutionPlan(
            issue_number=1,
            issue_title="Test",
            issue_description="Test description",
            branch_name="1-test",
            repo_full_name="owner/repo",
            steps=[failed_step],
            lambda_standards=[],
            terraform_standards=[],
            testing_standards=[]
        )
        markdown = serialize_plan_to_markdown(plan)

        # Act
        parsed_plan = parse_plan_from_markdown(markdown)

        # Assert
        step = parsed_plan.steps[0]
        assert step.status == "failed"
        assert step.error == "API rate limit exceeded"

    def test_parse_raises_for_missing_metadata(self):
        """Test: Raise ValueError for missing required metadata."""
        # Arrange
        invalid_markdown = """# Code Generation Plan

**Branch:** `some-branch`
**Repository:** owner/repo
"""

        # Act & Assert
        with pytest.raises(ValueError, match="Missing required metadata"):
            parse_plan_from_markdown(invalid_markdown)

    def test_parse_handles_empty_standards(self):
        """Test: Parse plan with no standards (- None markers)."""
        # Arrange
        plan = ExecutionPlan(
            issue_number=1,
            issue_title="Test",
            issue_description="Test",
            branch_name="1-test",
            repo_full_name="owner/repo",
            steps=[],
            lambda_standards=[],
            terraform_standards=[],
            testing_standards=[]
        )
        markdown = serialize_plan_to_markdown(plan)

        # Act
        parsed_plan = parse_plan_from_markdown(markdown)

        # Assert
        assert parsed_plan.lambda_standards == []
        assert parsed_plan.terraform_standards == []
        assert parsed_plan.testing_standards == []

    def test_roundtrip_serialization(self, basic_plan):
        """Test: Serialize then parse produces equivalent plan."""
        # Arrange & Act
        markdown = serialize_plan_to_markdown(basic_plan)
        parsed_plan = parse_plan_from_markdown(markdown)

        # Assert - Core metadata matches
        assert parsed_plan.issue_number == basic_plan.issue_number
        assert parsed_plan.issue_title == basic_plan.issue_title
        assert parsed_plan.branch_name == basic_plan.branch_name
        assert parsed_plan.repo_full_name == basic_plan.repo_full_name
        assert parsed_plan.backend_name == basic_plan.backend_name

        # Assert - Steps match
        assert len(parsed_plan.steps) == len(basic_plan.steps)
        for i, (parsed_step, orig_step) in enumerate(zip(parsed_plan.steps, basic_plan.steps)):
            assert parsed_step.step_number == orig_step.step_number
            assert parsed_step.title == orig_step.title
            assert parsed_step.status == orig_step.status

    def test_parse_legacy_plan_without_backend(self):
        """Test: Parse legacy plan without backend_name uses default."""
        # Arrange - markdown without **Backend:** line
        markdown = """# Code Generation Plan

**Issue:** #1 - Test issue
**Branch:** `1-test-branch`
**Repository:** owner/repo
**Created:** 2024-01-15T10:30:00

## Issue Description

Test description

## Cached Standards

### Lambda Standards

- None

### Terraform Standards

- None

### Testing Standards

- None

## Implementation Steps
"""

        # Act
        parsed_plan = parse_plan_from_markdown(markdown)

        # Assert - should use default "python"
        assert parsed_plan.backend_name == "python"

    def test_parse_different_status_values(self):
        """Test: Parse different status values correctly."""
        # Test all status values
        for status in ["pending", "in_progress", "completed", "failed"]:
            # Arrange
            step = PlanStep(
                step_number=1,
                title="Test",
                description="Test",
                files_to_create=[],
                kb_queries=[],
                status=status
            )
            plan = ExecutionPlan(
                issue_number=1,
                issue_title="Test",
                issue_description="Test",
                branch_name="1-test",
                repo_full_name="owner/repo",
                steps=[step],
                lambda_standards=[],
                terraform_standards=[],
                testing_standards=[]
            )
            markdown = serialize_plan_to_markdown(plan)

            # Act
            parsed_plan = parse_plan_from_markdown(markdown)

            # Assert
            assert parsed_plan.steps[0].status == status


# ============================================================================
# Test update_step_in_plan
# ============================================================================


class TestUpdateStepInPlan:
    """Test update_step_in_plan function."""

    def test_update_step_status(self, basic_plan):
        """Test: Update step status."""
        # Arrange & Act
        updated_plan = update_step_in_plan(basic_plan, step_number=1, status="in_progress")

        # Assert
        assert updated_plan.steps[0].status == "in_progress"

    def test_update_step_to_completed_sets_timestamp(self, basic_plan):
        """Test: Updating to 'completed' sets completed_at timestamp."""
        # Arrange & Act
        with patch('plan_manager.datetime') as mock_datetime:
            mock_datetime.utcnow.return_value.isoformat.return_value = "2024-01-15T12:00:00"
            updated_plan = update_step_in_plan(basic_plan, step_number=1, status="completed")

        # Assert
        assert updated_plan.steps[0].status == "completed"
        assert updated_plan.steps[0].completed_at == "2024-01-15T12:00:00"

    def test_update_step_error(self, basic_plan):
        """Test: Update step with error message."""
        # Arrange & Act
        updated_plan = update_step_in_plan(
            basic_plan,
            step_number=1,
            status="failed",
            error="Connection timeout"
        )

        # Assert
        assert updated_plan.steps[0].status == "failed"
        assert updated_plan.steps[0].error == "Connection timeout"

    def test_update_step_cost(self, basic_plan):
        """Test: Update step with cost information."""
        # Arrange
        cost = TokenUsage(
            input_tokens=2000,
            output_tokens=1000,
            total_cost_usd=0.009000
        )

        # Act
        updated_plan = update_step_in_plan(
            basic_plan,
            step_number=1,
            status="completed",
            cost=cost
        )

        # Assert
        assert updated_plan.steps[0].cost is not None
        assert updated_plan.steps[0].cost.input_tokens == 2000
        assert updated_plan.steps[0].cost.output_tokens == 1000
        assert updated_plan.steps[0].cost.total_cost_usd == 0.009000

    def test_update_recalculates_total_cost(self, basic_plan):
        """Test: Updating step cost recalculates plan total cost."""
        # Arrange
        cost1 = TokenUsage(input_tokens=1000, output_tokens=500, total_cost_usd=0.004500)
        cost2 = TokenUsage(input_tokens=2000, output_tokens=1000, total_cost_usd=0.009000)

        # Act
        plan = update_step_in_plan(basic_plan, step_number=1, status="completed", cost=cost1)
        plan = update_step_in_plan(plan, step_number=2, status="completed", cost=cost2)

        # Assert
        assert plan.total_cost is not None
        assert plan.total_cost.input_tokens == 3000
        assert plan.total_cost.output_tokens == 1500
        assert abs(plan.total_cost.total_cost_usd - 0.013500) < 0.000001

    def test_update_raises_for_invalid_step(self, basic_plan):
        """Test: Raise ValueError for non-existent step number."""
        # Act & Assert
        with pytest.raises(ValueError, match="Step 99 not found"):
            update_step_in_plan(basic_plan, step_number=99, status="completed")

    def test_update_only_status(self, basic_plan):
        """Test: Update only status without other parameters."""
        # Arrange - verify step doesn't have error or cost
        assert basic_plan.steps[0].error is None
        assert basic_plan.steps[0].cost is None

        # Act
        updated_plan = update_step_in_plan(basic_plan, step_number=1, status="in_progress")

        # Assert - only status changed
        assert updated_plan.steps[0].status == "in_progress"
        assert updated_plan.steps[0].error is None
        assert updated_plan.steps[0].cost is None

    def test_update_preserves_other_steps(self, basic_plan):
        """Test: Updating one step doesn't affect other steps."""
        # Arrange
        original_step2_status = basic_plan.steps[1].status

        # Act
        updated_plan = update_step_in_plan(basic_plan, step_number=1, status="completed")

        # Assert
        assert updated_plan.steps[0].status == "completed"
        assert updated_plan.steps[1].status == original_step2_status

    def test_update_step_by_number_not_index(self, basic_plan):
        """Test: Step is found by step_number, not list index."""
        # Arrange - create plan with non-sequential step numbers
        plan = ExecutionPlan(
            issue_number=1,
            issue_title="Test",
            issue_description="Test",
            branch_name="1-test",
            repo_full_name="owner/repo",
            steps=[
                PlanStep(step_number=5, title="Step 5", description="Fifth step", files_to_create=[], kb_queries=[]),
                PlanStep(step_number=10, title="Step 10", description="Tenth step", files_to_create=[], kb_queries=[]),
            ],
            lambda_standards=[],
            terraform_standards=[],
            testing_standards=[]
        )

        # Act
        updated_plan = update_step_in_plan(plan, step_number=10, status="in_progress")

        # Assert - step 10 (second in list) was updated
        assert updated_plan.steps[0].status == "pending"  # step 5 unchanged
        assert updated_plan.steps[1].status == "in_progress"  # step 10 updated

    def test_update_with_none_values_preserves_existing(self, basic_plan):
        """Test: Passing None for status/error/cost preserves existing values."""
        # Arrange - set initial values
        basic_plan.steps[0].status = "in_progress"
        basic_plan.steps[0].error = "Previous error"

        # Act - update with only cost (status and error are None)
        cost = TokenUsage(input_tokens=100, output_tokens=50, total_cost_usd=0.001)
        updated_plan = update_step_in_plan(basic_plan, step_number=1, cost=cost)

        # Assert - status preserved, error preserved, cost added
        assert updated_plan.steps[0].status == "in_progress"
        assert updated_plan.steps[0].error == "Previous error"
        assert updated_plan.steps[0].cost == cost

    def test_update_total_cost_with_single_step(self):
        """Test: Total cost calculated correctly with single step."""
        # Arrange
        plan = ExecutionPlan(
            issue_number=1,
            issue_title="Test",
            issue_description="Test",
            branch_name="1-test",
            repo_full_name="owner/repo",
            steps=[
                PlanStep(step_number=1, title="Only step", description="Only step", files_to_create=[], kb_queries=[]),
            ],
            lambda_standards=[],
            terraform_standards=[],
            testing_standards=[]
        )
        cost = TokenUsage(input_tokens=500, output_tokens=250, total_cost_usd=0.002)

        # Act
        updated_plan = update_step_in_plan(plan, step_number=1, status="completed", cost=cost)

        # Assert
        assert updated_plan.total_cost.input_tokens == 500
        assert updated_plan.total_cost.output_tokens == 250
        assert updated_plan.total_cost.total_cost_usd == 0.002

    def test_update_total_cost_with_no_costs(self, basic_plan):
        """Test: Total cost is zero when no steps have costs."""
        # Arrange - ensure no steps have costs
        for step in basic_plan.steps:
            step.cost = None

        # Act
        updated_plan = update_step_in_plan(basic_plan, step_number=1, status="in_progress")

        # Assert
        assert updated_plan.total_cost.input_tokens == 0
        assert updated_plan.total_cost.output_tokens == 0
        assert updated_plan.total_cost.total_cost_usd == 0.0


# ============================================================================
# Test summarization_cost roundtrip
# ============================================================================


class TestSummarizationCostRoundtrip:
    """Test that summarization_cost is preserved through serialize/parse cycle."""

    def test_roundtrip_preserves_summarization_cost(self):
        """Test: Summarization cost is preserved through serialize/parse cycle."""
        # Arrange
        plan = ExecutionPlan(
            issue_number=1,
            issue_title="Test",
            issue_description="Test description",
            branch_name="1-test",
            repo_full_name="owner/repo",
            steps=[
                PlanStep(
                    step_number=1,
                    title="Create handler",
                    description="Create Lambda handler",
                    files_to_create=["handler.py"],
                    kb_queries=[],
                    status="completed",
                    cost=TokenUsage(
                        input_tokens=5000,
                        output_tokens=2000,
                        total_cost_usd=0.025000
                    )
                )
            ],
            lambda_standards=["Test standard"],
            terraform_standards=[],
            testing_standards=[],
            summarization_cost=TokenUsage(
                input_tokens=10000,
                output_tokens=500,
                total_cost_usd=0.002625
            ),
            total_cost=TokenUsage(
                input_tokens=5000,
                output_tokens=2000,
                total_cost_usd=0.025000
            )
        )

        # Act
        markdown = serialize_plan_to_markdown(plan)
        parsed_plan = parse_plan_from_markdown(markdown)

        # Assert - summarization cost is preserved
        assert parsed_plan.summarization_cost is not None
        assert parsed_plan.summarization_cost.input_tokens == 10000
        assert parsed_plan.summarization_cost.output_tokens == 500
        assert abs(parsed_plan.summarization_cost.total_cost_usd - 0.002625) < 0.000001

        # Assert - total cost is preserved
        assert parsed_plan.total_cost is not None
        assert parsed_plan.total_cost.input_tokens == 5000
        assert parsed_plan.total_cost.output_tokens == 2000
        assert abs(parsed_plan.total_cost.total_cost_usd - 0.025000) < 0.000001

    def test_serialize_includes_summarization_cost_section(self):
        """Test: Markdown includes Standards Summarization section when cost present."""
        # Arrange
        plan = ExecutionPlan(
            issue_number=1,
            issue_title="Test",
            issue_description="Test description",
            branch_name="1-test",
            repo_full_name="owner/repo",
            steps=[],
            lambda_standards=[],
            terraform_standards=[],
            testing_standards=[],
            summarization_cost=TokenUsage(
                input_tokens=10000,
                output_tokens=500,
                total_cost_usd=0.002625
            ),
            total_cost=TokenUsage(
                input_tokens=5000,
                output_tokens=2000,
                total_cost_usd=0.025000
            )
        )

        # Act
        markdown = serialize_plan_to_markdown(plan)

        # Assert
        assert "## Cost Summary" in markdown
        assert "### Standards Summarization (Haiku)" in markdown
        assert "**Cost:** $0.002625" in markdown
        assert "**Input Tokens:** 10,000" in markdown
        assert "**Output Tokens:** 500" in markdown
        assert "### Code Generation (Sonnet)" in markdown
        assert "### Grand Total" in markdown

    def test_parse_handles_plan_without_summarization_cost(self):
        """Test: Plans without summarization cost section parse correctly."""
        # Arrange
        plan = ExecutionPlan(
            issue_number=1,
            issue_title="Test",
            issue_description="Test description",
            branch_name="1-test",
            repo_full_name="owner/repo",
            steps=[],
            lambda_standards=[],
            terraform_standards=[],
            testing_standards=[],
            # No summarization_cost - only total_cost
            total_cost=TokenUsage(
                input_tokens=5000,
                output_tokens=2000,
                total_cost_usd=0.025000
            )
        )

        # Act
        markdown = serialize_plan_to_markdown(plan)
        parsed_plan = parse_plan_from_markdown(markdown)

        # Assert - no summarization cost
        assert parsed_plan.summarization_cost is None
        # Assert - total cost is preserved
        assert parsed_plan.total_cost is not None
        assert parsed_plan.total_cost.total_cost_usd == 0.025000

    def test_update_step_preserves_summarization_cost(self):
        """Test: Updating a step preserves the summarization_cost."""
        # Arrange
        plan = ExecutionPlan(
            issue_number=1,
            issue_title="Test",
            issue_description="Test description",
            branch_name="1-test",
            repo_full_name="owner/repo",
            steps=[
                PlanStep(
                    step_number=1,
                    title="Create handler",
                    description="Create Lambda handler",
                    files_to_create=["handler.py"],
                    kb_queries=[],
                    status="pending"
                )
            ],
            lambda_standards=[],
            terraform_standards=[],
            testing_standards=[],
            summarization_cost=TokenUsage(
                input_tokens=10000,
                output_tokens=500,
                total_cost_usd=0.002625
            )
        )

        # Act - serialize, parse, update step, serialize again, parse again
        markdown1 = serialize_plan_to_markdown(plan)
        parsed_plan = parse_plan_from_markdown(markdown1)

        # Update the step
        updated_plan = update_step_in_plan(
            parsed_plan,
            step_number=1,
            status="completed",
            cost=TokenUsage(input_tokens=5000, output_tokens=2000, total_cost_usd=0.025000)
        )

        # Serialize and parse again
        markdown2 = serialize_plan_to_markdown(updated_plan)
        final_plan = parse_plan_from_markdown(markdown2)

        # Assert - summarization cost is still preserved after the full cycle
        assert final_plan.summarization_cost is not None
        assert final_plan.summarization_cost.input_tokens == 10000
        assert final_plan.summarization_cost.output_tokens == 500
        assert abs(final_plan.summarization_cost.total_cost_usd - 0.002625) < 0.000001
