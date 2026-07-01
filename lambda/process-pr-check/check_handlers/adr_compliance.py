"""
ADR Compliance check handler.

Analyzes Lambda handlers and Terraform files against ADR standards using Claude.
Fetches PR file diffs from GitHub or Azure DevOps and uses Bedrock Claude to
analyze compliance.
"""

import json
import logging
from typing import Dict, List, Any, Literal, Optional

import boto3
import requests
from botocore.exceptions import ClientError

from .bedrock_utils import call_bedrock, UsageTracker, SONNET_MODEL_ID
from .ado_context import refresh_ado_token

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients
lambda_client = boto3.client("lambda")
ssm_client = boto3.client("ssm")

CheckStatus = Literal["PASS", "WARN", "FAIL"]


def get_github_token(parameter_name: str) -> str:
    """Fetch GitHub token from SSM Parameter Store."""
    try:
        response = ssm_client.get_parameter(
            Name=parameter_name,
            WithDecryption=True
        )

        if not response.get("Parameter", {}).get("Value"):
            raise Exception(f"GitHub token not found in parameter: {parameter_name}")

        logger.info(f"Retrieved GitHub token from SSM: {parameter_name}")
        return response["Parameter"]["Value"]

    except ClientError as e:
        logger.error(f"Failed to retrieve GitHub token: {e}")
        raise


def fetch_pr_file_diff(repository: str, pr_number: int, file_path: str, token: str) -> str:
    """
    Fetch a single file's diff from GitHub PR.

    GitHub doesn't provide per-file diffs directly, so we fetch the full PR diff
    and extract the relevant file section.
    """
    # Fetch PR diff in unified diff format
    url = f"https://api.github.com/repos/{repository}/pulls/{pr_number}"

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3.diff",  # Request diff format
        "X-GitHub-Api-Version": "2022-11-28"
    }

    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()

        full_diff = response.text

        # Extract the diff for the specific file
        # Diffs are separated by "diff --git" headers
        file_diff = extract_file_diff_from_full_diff(full_diff, file_path)

        return file_diff if file_diff else f"diff --git a/{file_path} b/{file_path}\n(No diff available)"

    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch PR diff: {e}")
        raise Exception(f"GitHub API error: {str(e)}")


def extract_file_diff_from_full_diff(full_diff: str, file_path: str) -> str:
    """Extract a specific file's diff section from the full PR diff."""
    lines = full_diff.split("\n")

    # Find the start of this file's diff
    file_diff_lines = []
    capturing = False

    for i, line in enumerate(lines):
        # Check if this is the start of our file's diff
        if line.startswith("diff --git") and file_path in line:
            capturing = True
            file_diff_lines.append(line)
        # Check if we've hit the next file's diff
        elif capturing and line.startswith("diff --git"):
            break
        # Capture lines for our file
        elif capturing:
            file_diff_lines.append(line)

    return "\n".join(file_diff_lines) if file_diff_lines else ""


def _fetch_file_diff(
    *,
    provider: str,
    file_path: str,
    repository: str,
    pr_number: int,
    # GitHub context (unused for ADO)
    github_token: Optional[str] = None,
    # ADO context (unused for GitHub)
    ado_org: Optional[str] = None,
    ado_project: Optional[str] = None,
    ado_repo: Optional[str] = None,
    ado_access_token: Optional[str] = None,
    ado_base_sha: Optional[str] = None,
    ado_head_sha: Optional[str] = None,
) -> str:
    """Provider-dispatching per-file diff fetcher.

    On GitHub, uses the existing ``fetch_pr_file_diff`` which pulls the
    server-side unified diff via ``Accept: application/vnd.github.v3.diff``.

    On Azure DevOps, ADO has no equivalent server-side diff endpoint, so
    we fetch base and head file bytes at the SHAs already computed by
    the caller (via ``shared.pr_metadata.fetch_pr_metadata``) and produce
    a unified diff client-side via ``shared.pr_diff.compute_file_diff``.
    """
    if provider == "github":
        return fetch_pr_file_diff(repository, pr_number, file_path, github_token)

    if provider == "azure_devops":
        # Late imports so unit tests can load this module without the
        # shared/ package being on sys.path at import time (the parent
        # handler.py sets that up).
        from shared.source_fetch import fetch_file_at_sha, SourceFetchError
        from shared.pr_diff import compute_file_diff

        base_bytes: Optional[bytes]
        head_bytes: Optional[bytes]
        try:
            base_bytes = fetch_file_at_sha(
                "azure_devops", file_path, ado_base_sha,
                ado_org=ado_org, ado_project=ado_project, ado_repo=ado_repo,
                ado_access_token=ado_access_token,
            )
        except SourceFetchError:
            base_bytes = None  # File added — did not exist in base
        try:
            head_bytes = fetch_file_at_sha(
                "azure_devops", file_path, ado_head_sha,
                ado_org=ado_org, ado_project=ado_project, ado_repo=ado_repo,
                ado_access_token=ado_access_token,
            )
        except SourceFetchError:
            head_bytes = None  # File deleted in head

        return compute_file_diff(base_bytes, head_bytes, file_path)

    raise ValueError(f"Unknown provider: {provider}")


def query_knowledge_base(lambda_name: str, query: str, top_k: int = 3) -> Dict[str, Any]:
    """Query knowledge base via query-kb Lambda."""
    payload = {
        "query": query,
        "topK": top_k
    }

    try:
        response = lambda_client.invoke(
            FunctionName=lambda_name,
            Payload=json.dumps(payload)
        )

        response_payload = json.loads(response["Payload"].read())

        # Parse the body field (query-kb returns answer/sources inside body)
        body = json.loads(response_payload.get("body", "{}"))

        return {
            "answer": body.get("answer", ""),
            "sources": body.get("sources", [])
        }

    except ClientError as e:
        logger.error(f"Failed to query knowledge base: {e}")
        raise


def analyze_code_with_claude(
    file_path: str,
    file_diff: str,
    standards: str,
    file_type: Literal["lambda", "terraform"],
    usage_tracker: UsageTracker
) -> Dict[str, Any]:
    """
    Ask Claude to analyze code against ADR standards.

    Returns:
        dict with keys: compliant (bool), explanation (str), suggestions (list)
    """
    file_type_label = "Lambda handler" if file_type == "lambda" else "Terraform file"

    focus_areas = (
        "Pydantic schemas, error handling, type safety, proper exports, logging"
        if file_type == "lambda"
        else "terraform-aws-modules usage, version pinning, KMS encryption, IAM policies, naming conventions"
    )

    prompt = f"""You are analyzing a {file_type_label} in a pull request to check compliance with our ADR standards.

FILE: {file_path}

ADR STANDARDS:
{standards}

FILE DIFF (changes being made):
```diff
{file_diff}
```

TASK:
Analyze ONLY the provided diff above. Determine if the code changes follow our ADR standards.

IMPORTANT RULES:
1. ONLY analyze what you can see in the diff
2. Do NOT make assumptions about code not shown in the diff
3. Do NOT hallucinate or invent code that isn't in the diff
4. Focus on what's being ADDED (+ lines) in the diff
5. Look for: {focus_areas}

RESPONSE FORMAT:
Respond with ONLY a JSON object (no markdown, no code blocks):
{{"compliant": true/false, "explanation": "brief explanation", "suggestions": ["suggestion 1", "suggestion 2"]}}

IMPORTANT: Only set compliant to false and provide suggestions if there are ACTUAL ADR violations visible in the diff. Do NOT make suggestions for:
- Things that cannot be verified from the diff alone
- General best practices that are not ADR requirements
- Assumptions about code outside the diff

Examples:
- If code follows standards: {{"compliant": true, "explanation": "Handler uses Pydantic schemas and proper error handling", "suggestions": []}}
- If missing standards: {{"compliant": false, "explanation": "Handler is missing Pydantic schema validation", "suggestions": ["Add a Pydantic model for request validation", "Add try-except error handling"]}}
- If cannot determine from diff: {{"compliant": true, "explanation": "Changes maintain existing patterns, no violations detected in diff", "suggestions": []}}"""

    logger.info(f"Calling Claude to analyze {file_type} compliance for {file_path}...")

    try:
        response_text, usage = call_bedrock(
            prompt=prompt,
            model_id=SONNET_MODEL_ID,
            max_tokens=1000,
            temperature=0.1
        )
        usage_tracker.add(usage)

        logger.info(f"Claude response for {file_path}: {response_text}")

        # Parse JSON response (handle markdown code blocks)
        json_text = response_text

        # Remove markdown code blocks if present
        json_text = json_text.replace("```json", "").replace("```", "").strip()

        # Extract JSON object if surrounded by other text
        if "{" in json_text and "}" in json_text:
            start = json_text.index("{")
            end = json_text.rindex("}") + 1
            json_text = json_text[start:end]

        try:
            parsed = json.loads(json_text)
            return {
                "compliant": parsed.get("compliant", False),
                "explanation": parsed.get("explanation", "No explanation provided"),
                "suggestions": parsed.get("suggestions", []) if isinstance(parsed.get("suggestions"), list) else []
            }

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Claude response as JSON: {response_text}")
            logger.error(f"Attempted to parse: {json_text}")
            logger.error(f"Parse error: {e}")

            # Fallback: try to extract fields from text
            compliant = "compliant" in response_text.lower() and "true" in response_text.lower()

            return {
                "compliant": compliant,
                "explanation": "Could not parse compliance analysis",
                "suggestions": []
            }

    except Exception as e:
        logger.error(f"Error calling Claude: {e}")
        raise


def analyze_lambda_handlers(
    repository: str,
    pr_number: int,
    files: List[str],
    query_kb_lambda_name: str,
    github_token: Optional[str],
    usage_tracker: UsageTracker,
    *,
    provider: str = "github",
    ado_org: Optional[str] = None,
    ado_project: Optional[str] = None,
    ado_repo: Optional[str] = None,
    ado_access_token: Optional[str] = None,
    ado_base_sha: Optional[str] = None,
    ado_head_sha: Optional[str] = None,
) -> Dict[str, Any]:
    """Analyze Lambda handlers against ADR standards using Claude."""
    # Filter for Lambda handler files (exclude tests, shared libs)
    handler_files = [
        f for f in files
        if f.startswith("lambda/") and f.endswith("/handler.py")
        and "/tests/" not in f
    ]

    if not handler_files:
        return {
            "status": "PASS",
            "message": "No Lambda handler files changed",
            "details": []
        }

    # Query KB for Lambda standards
    kb_result = query_knowledge_base(
        query_kb_lambda_name,
        "What are our Lambda handler standards? Include requirements for Pydantic validation, error handling, documentation, and testing.",
        top_k=5
    )

    logger.info(f"Lambda KB Result: {kb_result['answer']}")

    # Analyze each handler file with Claude
    details = []
    has_non_compliant = False

    for file in handler_files:
        try:
            # Fetch file diff from the source provider
            file_diff = _fetch_file_diff(
                provider=provider,
                file_path=file,
                repository=repository,
                pr_number=pr_number,
                github_token=github_token,
                ado_org=ado_org,
                ado_project=ado_project,
                ado_repo=ado_repo,
                ado_access_token=ado_access_token,
                ado_base_sha=ado_base_sha,
                ado_head_sha=ado_head_sha,
            )

            # Analyze with Claude
            analysis = analyze_code_with_claude(
                file, file_diff, kb_result["answer"], "lambda", usage_tracker
            )

            if not analysis["compliant"]:
                has_non_compliant = True
                details.append(f"**{file}**: {analysis['explanation']}")
                if analysis["suggestions"]:
                    details.append("  Suggestions:")
                    for suggestion in analysis["suggestions"]:
                        details.append(f"  - {suggestion}")
            # Don't show details for compliant files - keep output clean

        except Exception as e:
            logger.error(f"Error analyzing {file}: {e}")
            details.append(f"**{file}**: Analysis failed - {str(e)}")

    # Determine overall status
    if has_non_compliant:
        status = "WARN"  # Use WARN for advisory-only
        message = f"Found compliance issues in Lambda handlers"
    else:
        status = "PASS"
        message = f"All Lambda handlers follow ADR standards"

    return {
        "status": status,
        "message": message,
        "details": details
    }


def analyze_terraform_files(
    repository: str,
    pr_number: int,
    files: List[str],
    query_kb_lambda_name: str,
    github_token: Optional[str],
    usage_tracker: UsageTracker,
    *,
    provider: str = "github",
    ado_org: Optional[str] = None,
    ado_project: Optional[str] = None,
    ado_repo: Optional[str] = None,
    ado_access_token: Optional[str] = None,
    ado_base_sha: Optional[str] = None,
    ado_head_sha: Optional[str] = None,
) -> Dict[str, Any]:
    """Analyze Terraform files against module standards using Claude."""
    terraform_files = [f for f in files if f.startswith("terraform/") and f.endswith(".tf")]

    if not terraform_files:
        return {
            "status": "PASS",
            "message": "No Terraform files changed",
            "details": []
        }

    # Query KB for Terraform standards
    kb_result = query_knowledge_base(
        query_kb_lambda_name,
        "What are our Terraform module standards? Include requirements for terraform-aws-modules, version pinning, and naming conventions.",
        top_k=5
    )

    logger.info(f"Terraform KB Result: {kb_result['answer']}")

    # Analyze each Terraform file with Claude
    details = []
    has_non_compliant = False

    for file in terraform_files:
        try:
            # Fetch file diff from the source provider
            file_diff = _fetch_file_diff(
                provider=provider,
                file_path=file,
                repository=repository,
                pr_number=pr_number,
                github_token=github_token,
                ado_org=ado_org,
                ado_project=ado_project,
                ado_repo=ado_repo,
                ado_access_token=ado_access_token,
                ado_base_sha=ado_base_sha,
                ado_head_sha=ado_head_sha,
            )

            # Analyze with Claude
            analysis = analyze_code_with_claude(
                file, file_diff, kb_result["answer"], "terraform", usage_tracker
            )

            if not analysis["compliant"]:
                has_non_compliant = True
                details.append(f"**{file}**: {analysis['explanation']}")
                if analysis["suggestions"]:
                    details.append("  Suggestions:")
                    for suggestion in analysis["suggestions"]:
                        details.append(f"  - {suggestion}")
            # Don't show details for compliant files - keep output clean

        except Exception as e:
            logger.error(f"Error analyzing {file}: {e}")
            details.append(f"**{file}**: Analysis failed - {str(e)}")

    # Determine overall status
    if has_non_compliant:
        status = "WARN"
        message = f"Found compliance issues in Terraform files"
    else:
        status = "PASS"
        message = f"All Terraform files follow module standards"

    return {
        "status": status,
        "message": message,
        "details": details
    }


def check_adr_compliance(
    check_type: str,
    pr_number: int,
    repository: str,
    changed_files: List[str],
    query_kb_lambda_name: str,
    github_token_param: str,
    *,
    provider: str = "github",
    connection_id: str | None = None,
    ado_org: str | None = None,
    ado_project: str | None = None,
    ado_repo: str | None = None,
) -> Dict[str, Any]:
    """
    ADR Compliance check handler.

    Checks:
    - Lambda handlers follow ADR standards (Pydantic validation, error handling, documentation)
    - Terraform files follow module standards (terraform-aws-modules, version pinning)
    - Uses Claude to analyze actual code diffs against KB standards

    Provider-aware: on ``provider="github"`` fetches per-file diffs
    via GitHub's server-side unified-diff endpoint. On
    ``provider="azure_devops"`` refreshes the customer's ADO OAuth
    token, fetches PR head/base commit SHAs via
    ``shared.pr_metadata.fetch_pr_metadata``, then computes each
    file's diff client-side from base+head file bytes via
    ``shared.pr_diff.compute_file_diff``.
    """
    logger.info(
        f"Running ADR compliance check for PR #{pr_number} "
        f"(provider={provider})"
    )

    # Create usage tracker
    usage_tracker = UsageTracker()

    try:
        # Provider-specific auth + PR context prep.
        github_token: Optional[str] = None
        ado_access_token: Optional[str] = None
        ado_base_sha: Optional[str] = None
        ado_head_sha: Optional[str] = None

        if provider == "github":
            github_token = get_github_token(github_token_param)
        elif provider == "azure_devops":
            # Late import so unit tests can load this module without
            # the shared/ package on sys.path at import time.
            from shared.pr_metadata import fetch_pr_metadata

            ado_access_token = refresh_ado_token(connection_id)
            pr = fetch_pr_metadata(
                "azure_devops", pr_number,
                ado_org=ado_org, ado_project=ado_project, ado_repo=ado_repo,
                ado_access_token=ado_access_token,
            )
            ado_base_sha = pr.base_sha
            ado_head_sha = pr.head_sha
        else:
            raise ValueError(f"Unknown provider: {provider}")

        # Analyze Lambda handlers and Terraform files with Claude
        lambda_result = analyze_lambda_handlers(
            repository, pr_number, changed_files, query_kb_lambda_name,
            github_token, usage_tracker,
            provider=provider,
            ado_org=ado_org, ado_project=ado_project, ado_repo=ado_repo,
            ado_access_token=ado_access_token,
            ado_base_sha=ado_base_sha, ado_head_sha=ado_head_sha,
        )

        terraform_result = analyze_terraform_files(
            repository, pr_number, changed_files, query_kb_lambda_name,
            github_token, usage_tracker,
            provider=provider,
            ado_org=ado_org, ado_project=ado_project, ado_repo=ado_repo,
            ado_access_token=ado_access_token,
            ado_base_sha=ado_base_sha, ado_head_sha=ado_head_sha,
        )

        # Combine results
        all_details = lambda_result["details"] + terraform_result["details"]

        # Determine overall status (FAIL > WARN > PASS)
        if lambda_result["status"] == "FAIL" or terraform_result["status"] == "FAIL":
            overall_status = "FAIL"
        elif lambda_result["status"] == "WARN" or terraform_result["status"] == "WARN":
            overall_status = "WARN"
        else:
            overall_status = "PASS"

        message = (
            "All files follow ADR standards"
            if overall_status == "PASS"
            else f"{lambda_result['message']}. {terraform_result['message']}"
        )

        return {
            "checkType": check_type,
            "status": overall_status,
            "message": message,
            "details": all_details,
            "usage": usage_tracker
        }

    except Exception as e:
        logger.error(f"Error in ADR compliance check: {e}", exc_info=True)
        return {
            "checkType": check_type,
            "status": "WARN",
            "message": "ADR compliance check failed",
            "details": [
                "Could not complete automated analysis",
                "Please manually review code for ADR compliance",
                f"Error: {str(e)}"
            ],
            "usage": usage_tracker
        }
