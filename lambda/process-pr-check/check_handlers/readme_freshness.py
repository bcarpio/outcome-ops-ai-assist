"""
README Freshness check handler.

Checks:
- If terraform files changed, README.md should be updated
- If new handlers added, README.md should document them
- Uses Claude to analyze if README updates are meaningful
"""

import json
import logging
from typing import Dict, List, Any, Set

import boto3
import requests
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients
bedrock_client = boto3.client("bedrock-runtime", region_name="us-east-1")
ssm_client = boto3.client("ssm")


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
    """Fetch a single file's diff from GitHub PR."""
    url = f"https://api.github.com/repos/{repository}/pulls/{pr_number}"

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3.diff",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()

        full_diff = response.text

        # Extract the diff for the specific file
        file_diff = extract_file_diff_from_full_diff(full_diff, file_path)

        return file_diff if file_diff else f"diff --git a/{file_path} b/{file_path}\n(No diff available)"

    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch PR diff: {e}")
        raise Exception(f"GitHub API error: {str(e)}")


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


def extract_handler_names(files: List[str]) -> List[str]:
    """
    Extract handler names from file paths.

    Example: lambda/hello/handler.py -> "hello"
    """
    handlers = []

    for file in files:
        # Filter Lambda handler files (exclude tests and shared code)
        if (file.startswith("lambda/") and file.endswith("/handler.py") and "/tests/" not in file):
            parts = file.split("/")
            # Get handler directory name (e.g., "hello" from lambda/hello/handler.py)
            if len(parts) >= 3:
                handlers.append(parts[1])

    # Remove duplicates
    return list(set(handlers))


def extract_infrastructure_names(files: List[str]) -> List[str]:
    """Extract infrastructure resource names from Terraform files."""
    infra_names = []

    for file in files:
        if file.startswith("terraform/") and file.endswith(".tf"):
            # Extract filename without extension
            name = file.replace("terraform/", "").replace(".tf", "")
            infra_names.append(name)

    return infra_names


def analyze_readme_with_claude(
    readme_diff: str,
    handler_names: List[str],
    infra_names: List[str]
) -> Dict[str, Any]:
    """
    Ask Claude to analyze if README diff adequately documents the changes.

    Returns:
        dict with keys: adequate (bool), explanation (str)
    """
    changes_summary = []
    if handler_names:
        changes_summary.append(f"- New/modified Lambda handlers: {', '.join(handler_names)}")
    if infra_names:
        changes_summary.append(f"- Infrastructure changes: {', '.join(infra_names)}")

    changes_text = "\n".join(changes_summary)

    prompt = f"""You are analyzing a README.md update in a pull request to determine if it adequately documents the changes being made.

CHANGES IN THIS PR:
{changes_text}

README.MD DIFF (what was added/removed):
```diff
{readme_diff}
```

TASK:
Analyze ONLY the provided README diff above. Determine if it adequately documents the specific changes listed (new Lambda handlers and/or infrastructure changes).

IMPORTANT RULES:
1. ONLY use information from the README diff provided above
2. Do NOT make assumptions about what might be documented elsewhere
3. Do NOT hallucinate or invent documentation that isn't in the diff
4. Check if the specific handler names or infrastructure components are mentioned in the added lines (+ lines in diff)

RESPONSE FORMAT:
Respond with ONLY a JSON object (no markdown, no code blocks):
{{"adequate": true/false, "explanation": "brief explanation of what's documented or missing"}}

Examples:
- If handler "hello" is added to README: {{"adequate": true, "explanation": "README documents the new hello Lambda handler in the Lambda list and directory structure"}}
- If no handler mentioned: {{"adequate": false, "explanation": "README was modified but does not mention the new 'hello' Lambda handler"}}
- If only minor typo fix: {{"adequate": false, "explanation": "README changes appear unrelated to the new Lambda handler being added"}}"""

    logger.info("Calling Claude to analyze README adequacy...")

    try:
        response = bedrock_client.converse(
            modelId="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            messages=[
                {
                    "role": "user",
                    "content": [{"text": prompt}]
                }
            ],
            inferenceConfig={
                "maxTokens": 500,
                "temperature": 0.1  # Very low temperature for factual analysis
            }
        )

        output_message = response.get("output", {}).get("message", {})
        content = output_message.get("content", [])

        if not content or not content[0].get("text"):
            raise Exception("No content in Claude response")

        response_text = content[0]["text"].strip()
        logger.info(f"Claude response: {response_text}")

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
                "adequate": parsed.get("adequate", False),
                "explanation": parsed.get("explanation", "No explanation provided")
            }

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Claude response as JSON: {response_text}")
            logger.error(f"Attempted to parse: {json_text}")
            logger.error(f"Parse error: {e}")

            # Fallback: try to extract explanation from text
            adequate = "adequate" in response_text.lower() and "true" in response_text.lower()

            return {
                "adequate": adequate,
                "explanation": "Could not parse analysis response"
            }

    except Exception as e:
        logger.error(f"Error calling Claude for README analysis: {e}")
        raise


def check_readme_freshness(
    check_type: str,
    pr_number: int,
    repository: str,
    changed_files: List[str],
    github_token_param: str
) -> Dict[str, Any]:
    """
    README Freshness check handler.

    Checks:
    - If terraform files changed, README.md should be updated
    - If new handlers added, README.md should document them
    - Uses Claude to analyze if README updates are meaningful
    """
    logger.info(f"Running README freshness check for PR #{pr_number}")

    terraform_files = [f for f in changed_files if f.startswith("terraform/") and f.endswith(".tf")]
    handler_files = [
        f for f in changed_files
        if f.startswith("lambda/") and f.endswith("/handler.py") and "/tests/" not in f
    ]
    readme_updated = "README.md" in changed_files

    # If no terraform or handler changes, pass immediately
    if not terraform_files and not handler_files:
        return {
            "checkType": check_type,
            "status": "PASS",
            "message": "No infrastructure or handler changes detected",
            "details": []
        }

    # If README not updated at all, immediate WARN
    if not readme_updated:
        details = []
        if terraform_files:
            details.append(
                f"Infrastructure files changed ({len(terraform_files)} file(s)) but README.md was not updated."
            )
        if handler_files:
            details.append(
                f"New Lambda handlers added ({len(handler_files)} file(s)) but README.md was not updated."
            )
        details.append("Consider documenting new resources, handlers, and their purposes.")

        return {
            "checkType": check_type,
            "status": "WARN",
            "message": "README.md not updated",
            "details": details
        }

    # README was updated - use Claude to check if it's meaningful
    logger.info("README.md was updated, analyzing adequacy with Claude...")

    try:
        # Fetch GitHub token
        token = get_github_token(github_token_param)

        # Fetch README diff from GitHub
        readme_diff = fetch_pr_file_diff(repository, pr_number, "README.md", token)

        if not readme_diff or readme_diff == f"diff --git a/README.md b/README.md\n(No diff available)":
            logger.warning("README.md in changed files but no diff found")
            return {
                "checkType": check_type,
                "status": "PASS",
                "message": "README.md updated",
                "details": ["README.md was modified (diff not available for analysis)"]
            }

        # Extract names of changed handlers and infrastructure
        handler_names = extract_handler_names(changed_files)
        infra_names = extract_infrastructure_names(changed_files)

        # Ask Claude to analyze
        analysis = analyze_readme_with_claude(readme_diff, handler_names, infra_names)

        if analysis["adequate"]:
            return {
                "checkType": check_type,
                "status": "PASS",
                "message": "README.md adequately documents changes",
                "details": [analysis["explanation"]]
            }
        else:
            return {
                "checkType": check_type,
                "status": "WARN",
                "message": "README.md may not adequately document changes",
                "details": [
                    analysis["explanation"],
                    "",
                    "Suggested improvements:",
                    "- Ensure new Lambda handlers are listed in the Lambda functions section",
                    "- Update directory structure diagrams",
                    "- Document the purpose and usage of new components"
                ]
            }

    except Exception as e:
        logger.error(f"Error analyzing README with Claude: {e}", exc_info=True)
        # Fallback to simple check on error
        return {
            "checkType": check_type,
            "status": "WARN",
            "message": "README.md updated (could not verify adequacy)",
            "details": [
                "README.md was modified, but automated analysis failed.",
                "Please manually verify the documentation is adequate.",
                f"Error: {str(e)}"
            ]
        }
