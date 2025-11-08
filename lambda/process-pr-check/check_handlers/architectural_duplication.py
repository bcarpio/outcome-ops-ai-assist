"""
Architectural Duplication check handler.

Identifies related functionality across repositories to help engineers
make informed decisions about code reuse and cross-team collaboration.
This is purely informational and empowers engineers with platform-wide context.
"""

import json
import logging
from typing import Dict, List, Any

import boto3
import requests
from botocore.config import Config
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Configure retry strategy for Bedrock to handle throttling better
bedrock_config = Config(
    retries={
        'max_attempts': 10,
        'mode': 'adaptive'
    }
)

# AWS clients
bedrock_client = boto3.client("bedrock-runtime", region_name="us-east-1", config=bedrock_config)
lambda_client = boto3.client("lambda")
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
    # Fetch PR diff in unified diff format
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


def query_knowledge_base(lambda_name: str, query: str, top_k: int = 20) -> Dict[str, Any]:
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

        return {
            "answer": response_payload.get("answer", ""),
            "sources": response_payload.get("sources", [])
        }

    except ClientError as e:
        logger.error(f"Failed to query knowledge base: {e}")
        raise


def summarize_pr_with_claude(
    repository: str,
    handler_files: List[str],
    file_diffs: Dict[str, str]
) -> str:
    """
    Use Claude to summarize the PR changes at a high level.

    Args:
        repository: GitHub repository (owner/repo)
        handler_files: List of handler file paths
        file_diffs: Dict mapping file paths to their diffs

    Returns:
        High-level summary of what the PR does
    """
    # Collect all relevant diffs
    diffs = []
    for file in handler_files:
        if file in file_diffs:
            diffs.append(f"File: {file}\n{file_diffs[file]}")

    combined_diff = "\n\n---\n\n".join(diffs)

    prompt = f"""You are analyzing a GitHub pull request to understand what functionality is being added or modified.

REPOSITORY: {repository}
FILES CHANGED: {", ".join(handler_files)}

DIFFS:
```diff
{combined_diff}
```

TASK:
Provide a concise, high-level summary of what functionality this PR is adding or modifying. Focus on:
1. What does this code DO (not how it does it)
2. What problem is it solving
3. Key technologies or integrations involved (e.g., DynamoDB, S3, GitHub API, Bedrock)

IMPORTANT:
- Keep it to 2-3 sentences maximum
- Focus on functionality, not implementation details
- Be specific about what the code accomplishes

Example: "This PR adds a Lambda handler that processes GitHub webhook events to track pull request metadata. It validates incoming events, stores PR data in DynamoDB, and integrates with the GitHub API to fetch additional context."

Your summary:"""

    logger.info("Calling Claude to summarize PR functionality...")

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
                "maxTokens": 300,
                "temperature": 0.2
            }
        )

        output_message = response.get("output", {}).get("message", {})
        content = output_message.get("content", [])

        if not content or not content[0].get("text"):
            raise Exception("No content in Claude response")

        summary = content[0]["text"].strip()
        logger.info(f"PR Summary: {summary}")

        return summary

    except Exception as e:
        logger.error(f"Error calling Claude for summary: {e}")
        raise


def analyze_similarity_with_claude(pr_summary: str, kb_context: str) -> Dict[str, Any]:
    """
    Use Claude to analyze KB results for similar functionality.

    Returns:
        dict with keys: hasSimilar (bool), findings (list of str)
    """
    prompt = f"""You are identifying related functionality across repositories to help engineers make informed decisions about code reuse and collaboration opportunities.

NEW FUNCTIONALITY BEING ADDED:
{pr_summary}

EXISTING CODE FROM OTHER REPOS (from knowledge base):
{kb_context}

TASK:
Identify any existing code that has related functionality. Look for:
- Similar purpose or use case that could enable code reuse
- Overlapping functionality that might benefit from consolidation
- Existing utilities/services that could be extended rather than recreated
- Opportunities for cross-team collaboration on shared solutions

IMPORTANT GUIDELINES:
1. ONLY identify meaningful relationships in functionality and purpose
2. Do NOT identify things just because they use the same technology (e.g., both use DynamoDB)
3. Do NOT identify standard boilerplate or common patterns
4. Be specific about WHAT is related and WHY it might be relevant
5. Frame findings as opportunities, not problems

RESPONSE FORMAT:
Respond with ONLY a JSON object (no markdown, no code blocks):
{{
  "hasSimilar": true/false,
  "findings": [
    "Specific finding 1 with repo/file reference",
    "Specific finding 2 with repo/file reference"
  ]
}}

If no related functionality found, return:
{{"hasSimilar": false, "findings": []}}

Examples of what to identify:
- Related: Both handle GitHub webhook validation and event processing
- NOT related: Both use DynamoDB (that's just a common tool)
- Related: Both implement retry logic with exponential backoff for external APIs
- NOT related: Both are Lambda functions (that's just the deployment pattern)"""

    logger.info("Calling Claude to analyze similarity...")

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
                "maxTokens": 1000,
                "temperature": 0.1
            }
        )

        output_message = response.get("output", {}).get("message", {})
        content = output_message.get("content", [])

        if not content or not content[0].get("text"):
            raise Exception("No content in Claude response")

        response_text = content[0]["text"].strip()
        logger.info(f"Claude similarity analysis: {response_text}")

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
                "hasSimilar": parsed.get("hasSimilar", False),
                "findings": parsed.get("findings", []) if isinstance(parsed.get("findings"), list) else []
            }

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Claude response as JSON: {response_text}")
            logger.error(f"Attempted to parse: {json_text}")
            logger.error(f"Parse error: {e}")

            # Fallback: return no findings
            return {
                "hasSimilar": False,
                "findings": ["Could not parse similarity analysis"]
            }

    except Exception as e:
        logger.error(f"Error calling Claude for similarity analysis: {e}")
        raise


def check_architectural_duplication(
    check_type: str,
    pr_number: int,
    repository: str,
    changed_files: List[str],
    query_kb_lambda_name: str,
    github_token_param: str
) -> Dict[str, Any]:
    """
    Architectural Duplication check handler.

    Identifies related functionality across repositories to help engineers
    make informed decisions about code reuse and cross-team collaboration.

    Process:
    1. Summarize what the PR does (using Claude)
    2. Query knowledge base for related functionality across repos
    3. Analyze KB results to identify meaningful relationships (using Claude)
    4. Present findings as opportunities for consideration
    """
    logger.info(f"Running architectural duplication check for PR #{pr_number}")

    # Filter to handler files only
    handler_files = [
        f for f in changed_files
        if f.startswith("lambda/") and f.endswith("/handler.py")
        and "/tests/" not in f
    ]

    # If no handler files, pass immediately
    if not handler_files:
        return {
            "checkType": check_type,
            "status": "PASS",
            "message": "No handler files changed",
            "details": []
        }

    try:
        # Fetch GitHub token
        token = get_github_token(github_token_param)

        # Fetch diffs for all handler files
        file_diffs = {}
        for file in handler_files:
            try:
                file_diffs[file] = fetch_pr_file_diff(repository, pr_number, file, token)
            except Exception as e:
                logger.warning(f"Could not fetch diff for {file}: {e}")
                file_diffs[file] = "(Diff unavailable)"

        # Step 1: Summarize what the PR does
        pr_summary = summarize_pr_with_claude(repository, handler_files, file_diffs)

        # Step 2: Query knowledge base for similar functionality
        # Use high topK (20) to cast a wide net across all repos
        kb_query = f"""Search the knowledge base for handlers, services, or components with similar functionality to: {pr_summary}

Look for things that handle similar use cases, solve similar problems, or provide overlapping functionality."""

        kb_result = query_knowledge_base(query_kb_lambda_name, kb_query, top_k=20)

        logger.info(f"KB search results: {kb_result['answer']}")

        # Step 3: Analyze KB results with Claude to determine if there are genuine similarities
        analysis = analyze_similarity_with_claude(pr_summary, kb_result["answer"])

        # Step 4: Return results
        details = []

        if analysis["hasSimilar"] and analysis["findings"]:
            details.append("**Related functionality identified across the platform:**")
            details.append("")
            for finding in analysis["findings"]:
                details.append(f"- {finding}")
            details.append("")
            details.append("**Questions to consider:**")
            details.append("- Could any of these existing implementations be reused or extended?")
            details.append("- Would consolidating this functionality into a shared library benefit multiple teams?")
            details.append("- Are there opportunities to collaborate with other teams on a unified approach?")
            details.append("- Does this implementation offer distinct advantages or serve different requirements?")
            details.append("")
            details.append(
                "This check helps surface opportunities for code reuse and cross-team collaboration. "
                "You have the context to make the best architectural decision for your use case."
            )

            return {
                "checkType": check_type,
                "status": "WARN",
                "message": "Related functionality identified across repos",
                "details": details
            }
        else:
            details.append("This functionality is unique across the platform.")
            details.append("No related implementations were identified in other repositories.")

            return {
                "checkType": check_type,
                "status": "PASS",
                "message": "No related functionality identified",
                "details": details
            }

    except Exception as e:
        logger.error(f"Error in architectural duplication check: {e}", exc_info=True)
        return {
            "checkType": check_type,
            "status": "WARN",
            "message": "Architectural duplication check failed",
            "details": [
                "Could not complete automated analysis",
                "Please manually review for similar functionality in other repos",
                f"Error: {str(e)}"
            ]
        }
