"""
Generate Code Lambda Handler.

Main handler that routes between three execution paths:
1. GitHub webhook (API Gateway) - Send message to SQS for async plan generation
2. Jira Automation (SSM direct invoke) - Send message to SQS for async plan generation
3. SQS message - Route to plan generation OR step execution based on action field

GitHub Webhook Event: https://docs.github.com/en/webhooks/webhook-events-and-payloads#label
Jira Automation: SSM Document invokes Lambda directly with Jira payload
"""

import json
import logging
import os
import re
from typing import Any, Dict, Optional
from uuid import uuid4

import boto3
from botocore.exceptions import ClientError

from github_api import create_branch, get_github_token, get_webhook_secret
from models import (
    APIGatewayProxyEvent,
    GitHubWebhookEvent,
    PRCommandMessage,
    SQSEvent,
    StepExecutionMessage
)
from plan_generator import handle_webhook
from step_executor import handle_step_message
from utils import verify_webhook_signature, generate_branch_name

# Logging setup
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Environment variables
ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")

# AWS clients
ssm_client = boto3.client("ssm")
sqs_client = boto3.client("sqs")


# ============================================================================
# SQS Helper Functions
# ============================================================================


def get_customer_id_from_cache() -> Optional[str]:
    """Get customer ID from cached license claims for usage tracking."""
    try:
        from outcomeops_license.cache import get_cached_claims
        claims = get_cached_claims()
        if claims:
            return claims.get("sub")
    except ImportError:
        pass
    except Exception:
        pass
    return None


def get_queue_url() -> str:
    """
    Fetch SQS queue URL from SSM Parameter Store.

    Returns:
        str: SQS queue URL for code generation

    Raises:
        Exception: If queue URL not found in SSM
    """
    param_name = f"/{ENV}/{APP_NAME}/sqs/code-generation-queue-url"

    try:
        response = ssm_client.get_parameter(
            Name=param_name,
            WithDecryption=False  # Queue URL is not encrypted
        )

        queue_url = response.get("Parameter", {}).get("Value")
        if not queue_url:
            raise Exception(f"Queue URL not found in parameter: {param_name}")

        logger.info(f"Retrieved queue URL from SSM: {param_name}")
        return queue_url

    except ClientError as e:
        logger.error(f"Failed to retrieve queue URL: {e}")
        raise


def send_plan_generation_message(
    issue_number: int,
    issue_title: str,
    issue_description: str,
    repo_full_name: str,
    branch_name: str,
    base_branch: str,
    plan_only: bool = False,
    customer_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Send plan generation message to SQS queue.

    Args:
        issue_number: GitHub issue number
        issue_title: GitHub issue title
        issue_description: GitHub issue body/description
        repo_full_name: Full repository name (owner/repo)
        branch_name: Branch to create code in
        base_branch: Base branch (usually main)
        plan_only: If True, skip code generation and create PR with just the plan
        customer_id: Optional license customer ID for usage tracking in DLQ

    Returns:
        dict: SQS SendMessage response

    Raises:
        Exception: If message send fails
    """
    queue_url = get_queue_url()

    message_body = StepExecutionMessage(
        action="generate_plan",
        issue_number=issue_number,
        issue_title=issue_title,
        issue_description=issue_description,
        repo_full_name=repo_full_name,
        branch_name=branch_name,
        current_step=0,
        total_steps=0,
        base_branch=base_branch,
        plan_only=plan_only,
        customer_id=customer_id
    ).model_dump_json()

    try:
        response = sqs_client.send_message(
            QueueUrl=queue_url,
            MessageBody=message_body,
            MessageGroupId=f"issue-{issue_number}",  # FIFO queue requirement
            MessageDeduplicationId=str(uuid4())  # Unique ID for deduplication
        )

        logger.info(
            f"Sent plan generation message to SQS for issue #{issue_number}. "
            f"MessageId: {response['MessageId']}"
        )

        return response

    except ClientError as e:
        logger.error(f"Failed to send message to SQS: {e}")
        raise


# ============================================================================
# Event Type Detection
# ============================================================================


def is_sqs_event(event: Dict[str, Any]) -> bool:
    """
    Check if event is from SQS.

    Args:
        event: Lambda event

    Returns:
        bool: True if SQS event
    """
    return "Records" in event and event["Records"] and "eventSource" in event["Records"][0] and event["Records"][0]["eventSource"] == "aws:sqs"


def is_api_gateway_event(event: Dict[str, Any]) -> bool:
    """
    Check if event is from API Gateway.

    Args:
        event: Lambda event

    Returns:
        bool: True if API Gateway event
    """
    return "requestContext" in event and "body" in event


def is_jira_event(event: Dict[str, Any]) -> bool:
    """
    Check if event is from Jira Automation (SSM direct invoke).

    Jira events have a 'source' field set to 'jira' and contain
    issueKey, issueSummary, issueDescription, and componentKey fields.

    The componentKey is the Jira component name which should match the
    GitHub repo path (e.g., "owner/repo"). This is set via the Jira
    Smart Value {{issue.components.name}}.

    Args:
        event: Lambda event

    Returns:
        bool: True if Jira Automation event
    """
    return (
        event.get("source") == "jira"
        and "issueKey" in event
        and "componentKey" in event
    )


# ============================================================================
# Jira Integration Helpers
# ============================================================================


def extract_issue_number_from_jira_key(issue_key: str) -> int:
    """
    Extract the numeric part from a Jira issue key.

    Args:
        issue_key: Jira issue key (e.g., "PROJ-123")

    Returns:
        int: The issue number (e.g., 123)
    """
    match = re.search(r"-(\d+)$", issue_key)
    if match:
        return int(match.group(1))
    # Fallback: hash the issue key to get a stable number
    return abs(hash(issue_key)) % 100000


# ============================================================================
# Webhook Handler (API Gateway Path)
# ============================================================================


def handle_api_gateway_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    Handle GitHub webhook event from API Gateway.

    Flow:
    1. Verify webhook signature
    2. Parse webhook payload
    3. Create branch (or verify it exists)
    4. Send plan generation message to SQS (returns immediately)

    Args:
        event: API Gateway event

    Returns:
        dict: API Gateway response (always 200 for webhooks)
    """
    logger.info(f"[generate-code] Received API Gateway event")

    try:
        # Parse API Gateway event
        gateway_event = APIGatewayProxyEvent(**event)

        # Verify webhook signature
        signature = gateway_event.headers.get("x-hub-signature-256", "")
        webhook_secret = get_webhook_secret()

        if not verify_webhook_signature(gateway_event.body, signature, webhook_secret):
            logger.warning("Webhook signature validation failed")
            return {
                "statusCode": 200,
                "body": json.dumps({"message": "Invalid signature"})
            }

        # Parse webhook payload
        try:
            webhook_event = GitHubWebhookEvent(**gateway_event.parsed_body)
        except ValueError as e:
            # Not a "labeled" action or wrong label - ignore silently
            logger.info(f"Ignoring event: {str(e)}")
            return {
                "statusCode": 200,
                "body": json.dumps({"message": "Event ignored"})
            }

        logger.info(
            f"Processing issue #{webhook_event.issue.number} "
            f"in {webhook_event.repository.full_name}"
        )

        # Generate branch name
        branch_name = generate_branch_name(
            webhook_event.issue.number,
            webhook_event.issue.title
        )

        logger.info(f"Generated branch name: {branch_name}")

        # Get GitHub token
        github_token = get_github_token()

        # Create branch
        result = create_branch(
            repo_full_name=webhook_event.repository.full_name,
            branch_name=branch_name,
            base_branch=webhook_event.repository.default_branch,
            github_token=github_token
        )

        if not result.get("success"):
            logger.error(f"Failed to create branch: {result.get('error')}")
            return {
                "statusCode": 200,
                "body": json.dumps({
                    "message": "Failed to create branch",
                    "error": result.get("error")
                })
            }

        # Check if this is a plan-only request (approved-for-plan label)
        plan_only = webhook_event.is_plan_only

        # Send plan generation message to SQS for async processing
        send_plan_generation_message(
            issue_number=webhook_event.issue.number,
            issue_title=webhook_event.issue.title,
            issue_description=webhook_event.issue.body or "",
            repo_full_name=webhook_event.repository.full_name,
            branch_name=branch_name,
            base_branch=webhook_event.repository.default_branch,
            plan_only=plan_only,
            customer_id=get_customer_id_from_cache()
        )

        mode = "plan only" if plan_only else "full code generation"
        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": f"Code generation started ({mode})",
                "issue_number": webhook_event.issue.number,
                "branch_name": branch_name,
                "plan_only": plan_only
            })
        }

    except Exception as e:
        logger.error(f"Error processing webhook: {e}", exc_info=True)
        # Always return 200 to prevent webhook retries
        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": "Internal error",
                "error": str(e)
            })
        }


# ============================================================================
# Jira Automation Handler (Direct Invoke Path)
# ============================================================================


def handle_jira_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    Handle Jira Automation event (SSM direct invoke).

    Flow:
    1. Extract Jira issue info from event
    2. Use componentKey directly as GitHub repo path (set via Jira Smart Value)
    3. Determine plan_only mode from label parameter
    4. Create branch (or verify it exists)
    5. Send plan generation message to SQS (returns immediately)

    The componentKey is the Jira component name which should match the GitHub
    repo path (e.g., "owner/repo"). Teams create a Jira component with this name
    and map it via Jira Automation Smart Value: {{issue.components.name}}

    The label parameter determines the generation mode:
    - "approved-for-generation": Full code generation (plan + code + tests + PR)
    - "approved-for-plan": Plan-only mode (generates plan and PR, no code generation)

    Args:
        event: Jira event with issueKey, issueSummary, issueDescription, componentKey, label

    Returns:
        dict: Response indicating success or failure
    """
    logger.info(f"[generate-code] Received Jira Automation event")

    try:
        # Extract Jira fields
        issue_key = event.get("issueKey", "")
        issue_summary = event.get("issueSummary", "")
        issue_description = event.get("issueDescription", "")
        # componentKey is the Jira component name = GitHub repo path (e.g., "owner/repo")
        repo_full_name = event.get("componentKey", "")
        # label determines the generation mode (defaults to full generation)
        label = event.get("label", "approved-for-generation")
        plan_only = label == "approved-for-plan"

        mode = "plan only" if plan_only else "full code generation"
        logger.info(f"Processing Jira issue {issue_key} for repo {repo_full_name} ({mode})")

        # Validate componentKey looks like a valid repo path
        if not repo_full_name or "/" not in repo_full_name:
            error_msg = f"Invalid componentKey: '{repo_full_name}'. Must be in format 'owner/repo'"
            logger.error(error_msg)
            return {
                "statusCode": 400,
                "body": json.dumps({
                    "message": "Configuration error",
                    "error": error_msg,
                    "hint": "Ensure Jira component name matches GitHub repo path (e.g., 'owner/repo')"
                })
            }

        # Default branch - could be enhanced to fetch from GitHub API if needed
        default_branch = "main"

        # Extract issue number from Jira key for branch naming
        issue_number = extract_issue_number_from_jira_key(issue_key)

        # Generate branch name (use Jira key in branch for traceability)
        branch_name = generate_branch_name(
            issue_number,
            f"{issue_key}-{issue_summary}"
        )

        logger.info(
            f"Jira {issue_key} → GitHub {repo_full_name}, branch: {branch_name}"
        )

        # Get GitHub token
        github_token = get_github_token()

        # Create branch
        result = create_branch(
            repo_full_name=repo_full_name,
            branch_name=branch_name,
            base_branch=default_branch,
            github_token=github_token
        )

        if not result.get("success"):
            logger.error(f"Failed to create branch: {result.get('error')}")
            return {
                "statusCode": 500,
                "body": json.dumps({
                    "message": "Failed to create branch",
                    "error": result.get("error")
                })
            }

        # Send plan generation message to SQS for async processing
        send_plan_generation_message(
            issue_number=issue_number,
            issue_title=f"[{issue_key}] {issue_summary}",
            issue_description=issue_description,
            repo_full_name=repo_full_name,
            branch_name=branch_name,
            base_branch=default_branch,
            plan_only=plan_only,
            customer_id=get_customer_id_from_cache()
        )

        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": f"{'Plan generation' if plan_only else 'Code generation'} started from Jira",
                "jira_issue": issue_key,
                "github_repo": repo_full_name,
                "branch_name": branch_name,
                "mode": mode
            })
        }

    except Exception as e:
        logger.error(f"Error processing Jira event: {e}", exc_info=True)
        return {
            "statusCode": 500,
            "body": json.dumps({
                "message": "Internal error",
                "error": str(e)
            })
        }


# ============================================================================
# SQS Handler (Step Execution Path)
# ============================================================================


def handle_sqs_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    Handle SQS message (plan generation, step execution, or PR commands).

    Flow depends on message action field:

    For action="generate_plan":
    1. Parse SQS message
    2. Query knowledge base for context
    3. Generate execution plan with Claude
    4. Commit plan to branch
    5. Send first step to SQS

    For action="execute_step":
    1. Parse SQS message
    2. Get plan from branch
    3. Execute current step (query KB, generate code, commit files)
    4. Update plan
    5. If more steps: send next step to SQS
    6. If all steps done: create PR

    For action="fix_readme" (PR command):
    1. Get PR changed files
    2. Clone repo, checkout PR branch
    3. Analyze changes and update README
    4. Commit and push
    5. Post result comment

    Args:
        event: SQS event

    Returns:
        dict: SQS response with batchItemFailures
    """
    logger.info(f"[generate-code] Received SQS event")

    sqs_event = SQSEvent(**event)

    # Track failures for partial batch failure reporting
    batch_item_failures = []

    for record in sqs_event.Records:
        try:
            # Peek at the action to determine message type
            parsed_body = record.parsed_body
            action = parsed_body.get("action", "")

            # Route PR commands to their handlers
            if action in ("fix_readme", "fix_tests", "fix_adr", "regenerate"):
                pr_message = PRCommandMessage(**parsed_body)
                logger.info(
                    f"Processing PR command '{action}' for PR #{pr_message.pr_number} "
                    f"in {pr_message.repo_full_name}"
                )

                if action == "fix_readme":
                    from readme_fixer import handle_fix_readme
                    result = handle_fix_readme(pr_message)
                    logger.info(f"Fix README result: {result}")
                elif action == "fix_tests":
                    from tests_fixer import handle_fix_tests
                    result = handle_fix_tests(pr_message)
                    logger.info(f"Fix tests result: {result}")
                elif action == "fix_adr":
                    from adr_fixer import handle_fix_adr
                    result = handle_fix_adr(pr_message)
                    logger.info(f"Fix ADR result: {result}")
                else:
                    # Other PR commands not yet implemented
                    logger.warning(f"PR command '{action}' not yet implemented")
                    result = {"status": "not_implemented", "action": action}

                continue

            # Otherwise, treat as step execution message
            step_message = StepExecutionMessage(**parsed_body)

            # Route based on action field
            if step_message.action == "generate_plan":
                logger.info(
                    f"Generating plan for issue #{step_message.issue_number}"
                )

                # Convert StepExecutionMessage to GitHubWebhookEvent format
                # This is a bit hacky but allows reuse of existing plan_generator code
                from models import GitHubIssue, GitHubRepository

                mock_webhook_event = type('obj', (object,), {
                    'issue': GitHubIssue(
                        number=step_message.issue_number,
                        title=step_message.issue_title,
                        body=step_message.issue_description,
                        html_url=f"https://github.com/{step_message.repo_full_name}/issues/{step_message.issue_number}",
                        state="open"
                    ),
                    'repository': GitHubRepository(
                        name=step_message.repo_full_name.split('/')[-1],
                        full_name=step_message.repo_full_name,
                        owner={"login": step_message.repo_full_name.split('/')[0]},
                        default_branch=step_message.base_branch
                    )
                })()

                # Generate execution plan
                # If plan_only=True, creates PR immediately (no code generation)
                # If plan_only=False, sends first step to SQS for code generation
                result = handle_webhook(
                    mock_webhook_event,
                    plan_only=step_message.plan_only,
                    customer_id=step_message.customer_id
                )

                logger.info(f"Plan generation result: {result}")

            elif step_message.action == "execute_step":
                logger.info(
                    f"Executing step {step_message.current_step}/{step_message.total_steps} "
                    f"for issue #{step_message.issue_number}"
                )

                # Execute step
                result = handle_step_message(step_message)

                logger.info(f"Step execution result: {result}")

            else:
                logger.error(f"Unknown action: {step_message.action}")
                raise ValueError(f"Unknown action: {step_message.action}")

        except Exception as e:
            logger.error(f"Error processing SQS message: {e}", exc_info=True)

            # Add to batch failures (SQS will retry)
            batch_item_failures.append({
                "itemIdentifier": record.messageId
            })

    # Return batch item failures for SQS to retry
    return {
        "batchItemFailures": batch_item_failures
    }


# ============================================================================
# Lambda Handler (Entry Point)
# ============================================================================


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Lambda handler for GitHub webhook, Jira Automation, and SQS step execution.

    Routes to appropriate handler based on event type:
    - API Gateway → GitHub webhook handler (plan generation)
    - Jira (source=jira) → Jira Automation handler (plan generation)
    - SQS → Step execution handler

    Args:
        event: Lambda event (API Gateway, Jira, or SQS)
        context: Lambda context (unused)

    Returns:
        dict: Response based on event type
    """
    # Validate OutcomeOps license before processing any requests
    try:
        from outcomeops_license import validate_license
        claims = validate_license()
        logger.info(f"[generate-code] License validated: {claims.get('sub')} (tier: {claims.get('license', {}).get('tier')})")
    except ImportError:
        # License layer not attached - this is a configuration issue
        logger.warning("[generate-code] License validator not available - layer not attached")
    except Exception as e:
        logger.error(f"[generate-code] License validation failed: {e}")
        return {
            "statusCode": 403,
            "body": json.dumps({"error": "License validation failed", "message": str(e)})
        }

    # Check PR limit before processing (ADR-019)
    # Only check for API Gateway/Jira events (new PR generation requests)
    # SQS events are continuations of already-approved work
    if is_api_gateway_event(event) or is_jira_event(event):
        try:
            from outcomeops_license import check_pr_limit
            result = check_pr_limit()
            if not result.allowed:
                logger.warning(f"[generate-code] PR limit reached: {result.reason}")
                return {
                    "statusCode": 429,
                    "body": json.dumps({
                        "error": "PR limit reached",
                        "message": result.reason or "Monthly PR limit exceeded",
                        "limit": result.limit,
                        "current": result.current,
                        "upgrade_url": result.upgrade_url
                    })
                }
            # Log any threshold alerts (soft enforcement)
            for alert in result.threshold_alerts:
                logger.warning(f"[generate-code] PR limit alert: {alert}")
        except ImportError:
            logger.debug("[generate-code] License layer not available, skipping enforcement check")
        except Exception as e:
            logger.warning(f"[generate-code] Enforcement check failed: {e}")

    # Route based on event type (log event type, not full payload to reduce CloudWatch verbosity)
    if is_sqs_event(event):
        logger.info("[generate-code] Routing to SQS handler (step execution)")
        return handle_sqs_event(event)

    elif is_jira_event(event):
        logger.info("[generate-code] Routing to Jira Automation handler")
        return handle_jira_event(event)

    elif is_api_gateway_event(event):
        logger.info("[generate-code] Routing to API Gateway handler (webhook)")
        return handle_api_gateway_event(event)

    else:
        logger.error(f"[generate-code] Unknown event type: {event}")
        return {
            "statusCode": 400,
            "body": json.dumps({"message": "Unknown event type"})
        }
