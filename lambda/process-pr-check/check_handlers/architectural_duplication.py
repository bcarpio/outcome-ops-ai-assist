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

from .bedrock_utils import call_bedrock, UsageTracker, SONNET_MODEL_ID

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients
# Lambda client with extended timeout for query-kb invocations (can take 5+ minutes with large context)
lambda_config = Config(
    read_timeout=600,  # 10 minutes
    connect_timeout=10,
    retries={"max_attempts": 0}  # No retries for Lambda invoke
)
lambda_client = boto3.client("lambda", config=lambda_config)
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


def query_knowledge_base(
    lambda_name: str,
    query: str,
    top_k: int = 20,
    doc_types: List[str] = None
) -> Dict[str, Any]:
    """
    Query knowledge base via query-kb Lambda using raw mode.

    Uses raw=true to skip Claude synthesis in query-kb, since we'll analyze
    the results with Claude directly in analyze_similarity_with_claude().
    This avoids a double Claude call.

    Args:
        lambda_name: Name of the query-kb Lambda
        query: Search query
        top_k: Number of results to return
        doc_types: Optional list of doc types to filter (e.g., ["code-map"])
    """
    payload = {
        "query": query,
        "topK": top_k,
        "raw": True  # Skip Claude synthesis - we'll analyze directly
    }

    # Filter to specific document types if provided
    if doc_types:
        payload["docTypes"] = doc_types

    try:
        response = lambda_client.invoke(
            FunctionName=lambda_name,
            Payload=json.dumps(payload)
        )

        response_payload = json.loads(response["Payload"].read())

        # Check for Lambda errors
        if response_payload.get("statusCode") != 200:
            error_body = response_payload.get("body", "{}")
            if isinstance(error_body, str):
                error_body = json.loads(error_body)
            error_msg = error_body.get("error", "Unknown error")
            raise Exception(f"query-kb Lambda failed: {error_msg}")

        # Parse body (may be string or dict)
        body = response_payload.get("body", "{}")
        if isinstance(body, str):
            body = json.loads(body)

        # Raw mode returns {"results": [...]} - format into context string
        results = body.get("results", [])
        sources = []

        # Format raw results into a context string for Claude analysis
        context_parts = []
        for doc in results:
            source = doc.get("source", "Unknown")
            text = doc.get("text", "")
            score = doc.get("score", 0.0)
            sources.append(source)

            # Format each document clearly
            context_parts.append(f"[Source: {source}] (Relevance: {score:.2f})\n{text}")

        context_str = "\n\n---\n\n".join(context_parts) if context_parts else "No relevant documents found."

        return {
            "answer": context_str,
            "sources": sources
        }

    except ClientError as e:
        logger.error(f"Failed to query knowledge base: {e}")
        raise


def summarize_pr_with_claude(
    repository: str,
    handler_files: List[str],
    file_diffs: Dict[str, str],
    usage_tracker: UsageTracker
) -> str:
    """
    Use Claude to summarize the PR changes at a high level.

    Args:
        repository: GitHub repository (owner/repo)
        handler_files: List of handler file paths
        file_diffs: Dict mapping file paths to their diffs
        usage_tracker: UsageTracker to record API usage

    Returns:
        High-level summary of what the PR does
    """
    # Collect all relevant diffs
    diffs = []
    for file in handler_files:
        if file in file_diffs:
            diffs.append(f"File: {file}\n{file_diffs[file]}")

    combined_diff = "\n\n---\n\n".join(diffs)

    prompt = f"""You are analyzing a GitHub pull request to understand its BUSINESS PURPOSE.

REPOSITORY: {repository}
FILES CHANGED: {", ".join(handler_files)}

DIFFS:
```diff
{combined_diff}
```

TASK:
Describe the BUSINESS FUNCTION of this code in domain-specific terms.

RULES:
- Focus ONLY on WHAT business problem this solves, not HOW it's implemented
- Use domain-specific language (e.g., "lists knowledge base documents" not "scans DynamoDB")
- DO NOT mention technologies (DynamoDB, S3, Lambda, API Gateway, etc.)
- DO NOT mention implementation patterns (pagination, sorting, filtering, etc.)
- Describe it as you would explain to a product manager, not an engineer

EXAMPLES:
- GOOD: "Lists recently ingested documents in the knowledge base for the UI"
- BAD: "Scans DynamoDB with timestamp-based sorting and pagination"
- GOOD: "Processes GitHub pull requests to check for code quality issues"
- BAD: "Lambda handler that calls GitHub API and stores results in DynamoDB"

Keep it to 1-2 sentences maximum. Your summary:"""

    logger.info("Calling Claude to summarize PR functionality...")

    try:
        summary, usage = call_bedrock(
            prompt=prompt,
            model_id=SONNET_MODEL_ID,
            max_tokens=300,
            temperature=0.2
        )
        usage_tracker.add(usage)

        logger.info(f"PR Summary: {summary}")

        return summary

    except Exception as e:
        logger.error(f"Error calling Claude for summary: {e}")
        raise


def extract_functional_keywords(
    pr_summary: str,
    file_diffs: Dict[str, str],
    usage_tracker: UsageTracker
) -> List[str]:
    """
    Extract functional keywords from PR for targeted KB searches.

    Returns a list of search queries that capture the business function
    from different angles to improve vector search coverage.

    Args:
        pr_summary: High-level summary of what the PR does
        file_diffs: Dict mapping file paths to their diffs
        usage_tracker: UsageTracker to record API usage

    Returns:
        List of 3-5 search queries for KB lookup
    """
    # Combine diffs for context (limit to avoid token explosion)
    combined_diff = "\n".join(list(file_diffs.values())[:3])[:5000]

    prompt = f"""You are extracting search queries to find similar code implementations.

PR SUMMARY:
{pr_summary}

CODE CHANGES:
```diff
{combined_diff}
```

TASK:
Generate 3-5 search queries that would find OTHER code doing the SAME business function.

RULES:
- Focus on WHAT the code does, not HOW (no tech: DynamoDB, Lambda, S3, API Gateway)
- Include the domain/context (e.g., "knowledge base", "user management", "game characters")
- Vary the wording to catch different descriptions of the same function
- Think: "What would someone else call code that does this?"

EXAMPLES:
For a "list recent documents" handler:
- "list documents handler"
- "document listing service"
- "retrieve recent documents"
- "knowledge base document query"

For a "process GitHub webhooks" handler:
- "github webhook processor"
- "pull request event handler"
- "git event processing"
- "repository webhook listener"

OUTPUT FORMAT (JSON array only, no markdown):
["query 1", "query 2", "query 3", "query 4"]

Your search queries:"""

    logger.info("Calling Claude to extract functional keywords...")

    try:
        response_text, usage = call_bedrock(
            prompt=prompt,
            model_id=SONNET_MODEL_ID,
            max_tokens=200,
            temperature=0.3
        )
        usage_tracker.add(usage)

        # Parse JSON response
        json_text = response_text.strip()
        json_text = json_text.replace("```json", "").replace("```", "").strip()

        # Extract array if surrounded by other text
        if "[" in json_text and "]" in json_text:
            start = json_text.index("[")
            end = json_text.rindex("]") + 1
            json_text = json_text[start:end]

        try:
            keywords = json.loads(json_text)
            if isinstance(keywords, list) and len(keywords) > 0:
                logger.info(f"Extracted search queries: {keywords}")
                return keywords[:5]  # Limit to 5 max
        except json.JSONDecodeError:
            logger.warning(f"Failed to parse keywords JSON: {response_text}")

        # Fallback: use the summary itself as a single query
        return [pr_summary]

    except Exception as e:
        logger.error(f"Error extracting keywords: {e}")
        # Fallback to summary
        return [pr_summary]


def analyze_similarity_with_claude(
    pr_summary: str,
    kb_context: str,
    changed_files: List[str],
    usage_tracker: UsageTracker
) -> Dict[str, Any]:
    """
    Use Claude to analyze KB results for similar functionality.

    Args:
        pr_summary: High-level summary of what the PR does
        kb_context: Knowledge base search results
        changed_files: Files being changed in this PR (to exclude from findings)
        usage_tracker: UsageTracker to record API usage

    Returns:
        dict with keys: hasSimilar (bool), findings (list of str)
    """
    prompt = f"""You are identifying code that performs the SAME BUSINESS FUNCTION across repositories.

NEW FUNCTIONALITY BEING ADDED:
{pr_summary}

FILES BEING CHANGED IN THIS PR (DO NOT REPORT THESE):
{", ".join(changed_files)}

EXISTING CODE FROM OTHER REPOS (from knowledge base):
{kb_context}

TASK:
Find existing code that performs the SAME BUSINESS FUNCTION (not just similar technical implementation).

CRITICAL - WHAT IS DUPLICATION:
- Code that solves the SAME BUSINESS PROBLEM in a different repo
- Example: Two different repos both have "list documents" functionality
- Example: Two repos both have "process webhook events" handlers

CRITICAL - WHAT IS NOT DUPLICATION:
- Using the same database (DynamoDB) is NOT duplication
- Using the same patterns (pagination, sorting) is NOT duplication
- Using the same AWS services is NOT duplication
- Similar technical implementation is NOT duplication unless solving the SAME business problem
- Test files are NEVER duplicates of production code

STRICT FILTERING:
- ONLY report if the business function is genuinely the same
- "List documents" in a knowledge base system is NOT similar to "List AI characters" in a game
- "Process GitHub PRs" is NOT similar to "Process user messages"
- The DOMAIN must match, not just the technical verb (list, get, create, etc.)

RESPONSE FORMAT (JSON only, no markdown):
{{
  "hasSimilar": true/false,
  "findings": [
    "**repo-name/path/to/file.py**: same [business function] - candidate for shared library"
  ]
}}

If no code performs the SAME BUSINESS FUNCTION, return:
{{"hasSimilar": false, "findings": []}}"""

    logger.info("Calling Claude to analyze similarity...")

    try:
        response_text, usage = call_bedrock(
            prompt=prompt,
            model_id=SONNET_MODEL_ID,
            max_tokens=1000,
            temperature=0.1
        )
        usage_tracker.add(usage)

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

    # Create usage tracker
    usage_tracker = UsageTracker()

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
            "details": [],
            "usage": usage_tracker
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

        # Step 1: Summarize what the PR does (business function, no tech details)
        pr_summary = summarize_pr_with_claude(repository, handler_files, file_diffs, usage_tracker)

        # Step 2: Extract functional keywords for multi-angle KB search
        search_queries = extract_functional_keywords(pr_summary, file_diffs, usage_tracker)
        logger.info(f"Generated {len(search_queries)} search queries: {search_queries}")

        # Step 3: Query KB with multiple keywords to improve coverage
        # Different phrasings of the same function may match different code maps
        # Filter to code-map types only - we want actual code, not docs/ADRs
        all_results = {}  # source -> result (dedupe by source)

        for query in search_queries:
            try:
                kb_result = query_knowledge_base(
                    query_kb_lambda_name,
                    query,
                    top_k=10,  # Fewer per query since we're doing multiple
                    doc_types=["code-map", "handler-group-summary"]
                )

                # Dedupe by source, keeping highest score
                for i, source in enumerate(kb_result.get("sources", [])):
                    # Extract individual result from context string
                    context_parts = kb_result["answer"].split("\n\n---\n\n")
                    if i < len(context_parts):
                        if source not in all_results:
                            all_results[source] = context_parts[i]
                        # Keep existing if already found (first match is usually best)

                logger.info(f"Query '{query[:50]}...' returned {len(kb_result.get('sources', []))} results")

            except Exception as e:
                logger.warning(f"KB query failed for '{query}': {e}")
                continue

        # Combine deduplicated results
        if all_results:
            combined_context = "\n\n---\n\n".join(all_results.values())
            combined_sources = list(all_results.keys())
        else:
            combined_context = "No relevant documents found."
            combined_sources = []

        logger.info(f"Combined KB search returned {len(combined_sources)} unique documents")

        # Step 4: Analyze KB results with Claude to determine if there are genuine similarities
        # Pass changed files so Claude can exclude them from results
        analysis = analyze_similarity_with_claude(pr_summary, combined_context, handler_files, usage_tracker)

        # Step 5: Return results
        details = []

        if analysis["hasSimilar"] and analysis["findings"]:
            details.append("**Similar implementations found across repositories:**")
            details.append("")
            for finding in analysis["findings"]:
                details.append(f"- {finding}")
            details.append("")
            details.append("**Consolidation opportunities:**")
            details.append("- Could these implementations be extracted into a shared library?")
            details.append("- Would consolidation reduce maintenance burden across teams?")
            details.append("- Are there opportunities to collaborate on a unified approach?")
            details.append("- Does your implementation offer distinct advantages that justify duplication?")
            details.append("")
            details.append(
                "This check identifies duplicate implementations to surface consolidation opportunities. "
                "You have the context to decide if consolidation makes sense for your use case."
            )

            return {
                "checkType": check_type,
                "status": "WARN",
                "message": "Similar implementations found across repos",
                "details": details,
                "usage": usage_tracker
            }
        else:
            details.append("This functionality is unique across the platform.")
            details.append("No related implementations were identified in other repositories.")

            return {
                "checkType": check_type,
                "status": "PASS",
                "message": "No related functionality identified",
                "details": details,
                "usage": usage_tracker
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
            ],
            "usage": usage_tracker
        }
