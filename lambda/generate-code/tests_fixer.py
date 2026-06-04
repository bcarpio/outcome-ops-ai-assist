"""
Tests Fixer module for PR comment commands.

Handles the "outcomeops: fix tests" command by:
1. Fetching test analysis from PR comments (explains what's wrong)
2. Fetching test failure details from PR comments (which tests failed)
3. Reading the test files that need fixing
4. Querying KB for testing patterns
5. Using Claude to generate fixed tests
6. Committing the fixes to the PR branch
"""

import base64
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import boto3
from botocore.config import Config

from github_api import (
    get_file,
    get_github_token,
    get_pr_comments,
    post_pr_comment,
    update_file,
)
from models import PRCommandMessage

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Environment variables
ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")

# Shared model client (dispatches Converse / Mantle Responses per ADR-035).
from shared.model_client import invoke as invoke_model

lambda_client = boto3.client("lambda")

CLAUDE_MODEL_ID = os.environ.get(
    "BEDROCK_ADVANCED_MODEL_ID", "us.anthropic.claude-sonnet-4-6"
)


def invoke_run_tests(message: PRCommandMessage) -> Dict[str, Any]:
    """
    Invoke the run-tests Lambda to verify fixes and post results.

    Args:
        message: PRCommandMessage with PR details

    Returns:
        Dict with run-tests result
    """
    run_tests_function_name = f"{ENV}-{APP_NAME}-run-tests"

    # Construct the PR URL
    pr_url = f"https://github.com/{message.repo_full_name}/pull/{message.pr_number}"

    # Build event payload for run-tests
    # Use skipPlanUpdate=True since we don't have a plan file
    event_payload = {
        "detail": {
            "issueNumber": message.pr_number,  # Use PR number as issue number
            "issueTitle": "Fix Tests",
            "repoFullName": message.repo_full_name,
            "branchName": message.branch_name,
            "baseBranch": message.base_branch,
            "prNumber": message.pr_number,
            "prUrl": pr_url,
            "environment": ENV,
            "appName": APP_NAME,
            "skipPlanUpdate": True
        }
    }

    try:
        response = lambda_client.invoke(
            FunctionName=run_tests_function_name,
            InvocationType="Event",  # Async invocation
            Payload=json.dumps(event_payload)
        )

        status_code = response.get("StatusCode", 0)
        if status_code == 202:  # Async invocation accepted
            logger.info(f"[tests-fix] Triggered run-tests Lambda for PR #{message.pr_number}")
            return {"status": "triggered", "function": run_tests_function_name}
        else:
            logger.warning(f"[tests-fix] run-tests invocation returned status {status_code}")
            return {"status": "warning", "status_code": status_code}

    except Exception as e:
        logger.error(f"[tests-fix] Failed to invoke run-tests: {e}")
        return {"status": "error", "error": str(e)}


def fetch_test_analysis(comments: List[Dict[str, Any]]) -> Optional[str]:
    """
    Find the test analysis comment from PR comments.

    Looks for comments with the outcomeops:test-analysis marker.

    Returns:
        Analysis content if found, None otherwise
    """
    for comment in reversed(comments):
        body = comment.get("body", "")
        if "<!-- outcomeops:test-analysis -->" in body:
            # Extract the analysis content (between header and "---" separator)
            match = re.search(
                r"## OutcomeOps: Test Failure Analysis\s*\n(.*?)\n---",
                body,
                re.DOTALL
            )
            if match:
                return match.group(1).strip()
            # Fallback: return body without marker
            return body.replace("<!-- outcomeops:test-analysis -->", "").strip()

    return None


def fetch_test_failure_output(comments: List[Dict[str, Any]]) -> Optional[str]:
    """
    Find the test failure output from PR comments.

    Looks for comments with the outcomeops:test-results marker.

    Returns:
        Test output if found, None otherwise
    """
    for comment in reversed(comments):
        body = comment.get("body", "")
        if "<!-- outcomeops:test-results -->" in body:
            # Extract test output from the comment
            match = re.search(r"### Test Output.*?```\n?(.*?)```", body, re.DOTALL)
            if match:
                return match.group(1).strip()
            return body

    return None


def extract_failed_test_files(test_output: str) -> List[str]:
    """
    Extract test file paths from pytest failure output.

    Looks for patterns like:
    - FAILED tests/test_foo.py::test_bar
    - FAILED lambda/tests/unit/test_foo.py::test_bar
    - lambda/tests/unit/test_foo.py:42: in test_bar (traceback)
    - unit/test_foo.py::test_bar (short summary)

    Returns:
        List of unique test file paths (repo-relative, no venv/tmp paths)
    """
    files = set()
    logger.info(f"[tests-fix] Extracting files from output ({len(test_output)} chars)")

    # Pattern 1: FAILED path/test_foo.py::test_name
    for match in re.finditer(r"FAILED\s+([\w/\-_.]+\.py)::", test_output):
        files.add(match.group(1))
    if files:
        logger.info(f"[tests-fix] Pattern 1 (FAILED::): {list(files)}")

    # Pattern 2: path/test_foo.py::test_name (short summary without FAILED)
    before = len(files)
    for match in re.finditer(r"(lambda/tests/[\w/\-_.]+\.py)::", test_output):
        files.add(match.group(1))
    if len(files) > before:
        logger.info(f"[tests-fix] Pattern 2 (lambda/tests::): +{len(files) - before} files")

    # Pattern 3: Traceback file paths like "lambda/tests/unit/test_foo.py:42:"
    before = len(files)
    for match in re.finditer(r"(lambda/tests/[\w/\-_.]+\.py):\d+:", test_output):
        files.add(match.group(1))
    if len(files) > before:
        logger.info(f"[tests-fix] Pattern 3 (lambda/tests:line): +{len(files) - before} files")

    # Pattern 4: unit/test_foo.py::test_name or unit/test_foo.py:42:
    before = len(files)
    for match in re.finditer(r"(unit/[\w/\-_.]+test[\w/\-_.]*\.py)(?:::|:\d+:)", test_output):
        files.add(match.group(1))
    if len(files) > before:
        logger.info(f"[tests-fix] Pattern 4 (unit/): +{len(files) - before} files")

    # Pattern 5: integration/test_foo.py patterns
    before = len(files)
    for match in re.finditer(r"(integration/[\w/\-_.]+test[\w/\-_.]*\.py)(?:::|:\d+:)", test_output):
        files.add(match.group(1))
    if len(files) > before:
        logger.info(f"[tests-fix] Pattern 5 (integration/): +{len(files) - before} files")

    # Pattern 6: General pattern - any .py file with :line: that contains "test"
    before = len(files)
    for match in re.finditer(r"([\w/\-_.]+\.py):\d+:", test_output):
        path = match.group(1)
        if "test" in path.lower():
            files.add(path)
    if len(files) > before:
        logger.info(f"[tests-fix] Pattern 6 (general :line:): +{len(files) - before} files")

    # Pattern 7: File names in backticks like `test_foo.py` (common in analysis)
    before = len(files)
    for match in re.finditer(r"`(test_[\w]+\.py)`", test_output):
        filename = match.group(1)
        if f"unit/{filename}" in test_output or "unit test" in test_output.lower():
            files.add(f"unit/{filename}")
        elif f"integration/{filename}" in test_output:
            files.add(f"integration/{filename}")
    if len(files) > before:
        logger.info(f"[tests-fix] Pattern 7 (backtick names): +{len(files) - before} files")

    # Pattern 8: Full paths in backticks like `lambda/tests/unit/test_foo.py`
    before = len(files)
    for match in re.finditer(r"`(lambda/tests/[\w/\-_.]+\.py)`", test_output):
        files.add(match.group(1))
    if len(files) > before:
        logger.info(f"[tests-fix] Pattern 8 (backtick paths): +{len(files) - before} files")

    logger.info(f"[tests-fix] Raw extracted files: {list(files)}")

    # Filter and normalize paths
    valid_files = set()
    filtered_out = []
    for path in files:
        original_path = path
        skip_reason = None

        if "venv/" in path or "site-packages" in path:
            skip_reason = "venv/site-packages"
        elif path.startswith("/"):
            skip_reason = "absolute path"
        elif "/tmp/" in path or path.startswith("tmp/"):
            skip_reason = "tmp path"
        elif ".." in path:
            skip_reason = "contains .."
        elif "test" not in path.lower():
            skip_reason = "no 'test' in name"

        if skip_reason:
            filtered_out.append(f"{original_path} ({skip_reason})")
            continue

        # Normalize relative paths from lambda/tests/
        if path.startswith("unit/") or path.startswith("integration/"):
            path = f"lambda/tests/{path}"

        valid_files.add(path)

    if filtered_out:
        logger.info(f"[tests-fix] Filtered out: {filtered_out}")

    logger.info(f"[tests-fix] Final valid files: {list(valid_files)}")
    return list(valid_files)


def query_kb_for_test_patterns(repo_name: str) -> str:
    """
    Query knowledge base for testing patterns and standards.

    Returns combined results from relevant KB queries.
    """
    query_kb_function_name = f"{ENV}-{APP_NAME}-query-kb"

    queries = [
        "test file structure and pytest patterns",
        "testing standards and best practices",
        "mock and fixture patterns for testing",
    ]

    results = []
    for query in queries:
        try:
            payload = {
                "query": query,
                "topK": 3,
                "raw": True,
                "repo": repo_name
            }

            response = lambda_client.invoke(
                FunctionName=query_kb_function_name,
                InvocationType="RequestResponse",
                Payload=json.dumps(payload)
            )

            response_payload = json.loads(response["Payload"].read())

            if response.get("FunctionError"):
                continue

            if "body" in response_payload:
                body = json.loads(response_payload["body"])
                raw_results = body.get("results", [])

                for result in raw_results:
                    if result.get("score", 0) > 0.3:
                        results.append(f"Source: {result.get('source', 'Unknown')}\n{result.get('text', '')}")

        except Exception as e:
            logger.warning(f"[tests-fix] KB query failed: {e}")

    if results:
        return "\n\n---\n\n".join(results)
    return ""


# Delimiter pattern for file extraction (same as step_executor.py)
FILE_DELIMITER_PATTERN = re.compile(r'^===FILE:\s*(.+?)\s*===$', re.MULTILINE)


def parse_delimited_response(response_text: str) -> Dict[str, str]:
    """
    Parse Claude's delimiter-based output format.

    Format:
        ===FILE: path/to/file.py===
        <raw file content>
        ===END===

    Returns:
        Dict of file path -> content
    """
    matches = list(FILE_DELIMITER_PATTERN.finditer(response_text))

    if not matches:
        logger.error(f"[tests-fix] No file delimiters found in response")
        logger.error(f"[tests-fix] Response preview: {response_text[:500]}")
        return {}

    logger.info(f"[tests-fix] Found {len(matches)} file delimiter(s)")

    files = {}
    for i, match in enumerate(matches):
        file_path = match.group(1).strip()
        start_pos = match.end()

        # Find end position (next delimiter or ===END=== or end of text)
        if i + 1 < len(matches):
            end_pos = matches[i + 1].start()
        else:
            end_marker = response_text.find('===END===', start_pos)
            if end_marker != -1:
                end_pos = end_marker
            else:
                end_pos = len(response_text)

        content = response_text[start_pos:end_pos].strip()

        # Remove any trailing ===END=== that might be included
        if content.endswith('===END==='):
            content = content[:-9].strip()

        logger.info(f"[tests-fix] Extracted file: {file_path} ({len(content)} chars, {content.count(chr(10))} lines)")
        files[file_path] = content

    return files


def generate_test_fixes(
    test_analysis: str,
    test_files: Dict[str, str],
    kb_patterns: str,
    user_guidance: Optional[str] = None
) -> Optional[Dict[str, str]]:
    """
    Use Claude to generate fixed test files.

    Args:
        test_analysis: Analysis of what's wrong with the tests
        test_files: Dict of file path -> current content
        kb_patterns: Testing patterns from knowledge base
        user_guidance: Optional user-provided guidance (takes priority)

    Returns:
        Dict of file path -> fixed content, or None on failure
    """
    # Build the prompt with all test files
    files_context = []
    for path, content in test_files.items():
        files_context.append(f"### {path}\n```python\n{content}\n```")

    files_text = "\n\n".join(files_context)

    # Build KB context section
    kb_context_text = ""
    if kb_patterns:
        kb_context_text = f"""
## Testing Standards (from Knowledge Base):
{kb_patterns[:4000]}
"""

    # Build guidance section - user guidance takes priority
    guidance_text = ""
    if user_guidance:
        guidance_text = f"""
## User Instructions (PRIORITY - follow these first):
{user_guidance}

"""

    prompt = f"""{kb_context_text}
{guidance_text}
## Test Failure Analysis:
{test_analysis}

## Test Files to Fix:
{files_text}

## Instructions:
{"Follow the user instructions above as your primary guidance. Use the test failure analysis as additional context." if user_guidance else "Based on the test failure analysis above, fix the test files."}

CRITICAL RULES:
1. Output the COMPLETE fixed file content - do NOT summarize or truncate
2. Preserve ALL existing tests, imports, and helper functions
3. Only modify the specific lines that need fixing
4. Do NOT delete or remove any tests that weren't mentioned in the analysis
5. If using mocks, ensure they match the actual function signatures

Output each fixed file using this EXACT format:

===FILE: path/to/file.py===
<complete file content here>
===END===

Only include files that need changes."""

    system_prompt = """You are fixing failing Python tests. Output fixed files using the delimiter format:

===FILE: path/to/file.py===
<complete file content>
===END===

CRITICAL: Output the COMPLETE file content. Do NOT truncate, summarize, or use placeholders like '# ... rest of file'.
Preserve ALL existing code and only modify what's needed to fix the failures."""

    try:
        resp = invoke_model(
            model_id=CLAUDE_MODEL_ID,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            system=system_prompt,
            temperature=0.2,
            max_tokens=16000,
        )

        input_tokens = resp.usage.input_tokens
        output_tokens = resp.usage.output_tokens
        logger.info(f"[tests-fix] tokens: {input_tokens} in, {output_tokens} out")

        response_text = resp.text

        if not response_text:
            logger.warning("[tests-fix] Claude returned empty response")
            return None

        # Parse delimiter-based response (no JSON = no escaping issues)
        fixed_files = parse_delimited_response(response_text)

        return fixed_files if fixed_files else None

    except Exception as e:
        logger.error(f"[tests-fix] Claude invocation failed: {e}")
        return None


def handle_fix_tests(message: PRCommandMessage) -> Dict[str, Any]:
    """
    Handle the fix_tests PR command.

    Args:
        message: PRCommandMessage with PR details

    Returns:
        Dict with status and details
    """
    logger.info(
        f"[tests-fix] Processing fix tests for PR #{message.pr_number} "
        f"in {message.repo_full_name}"
    )

    try:
        # Get GitHub token
        github_token = get_github_token()

        # Get PR comments
        comments = get_pr_comments(
            message.repo_full_name,
            message.pr_number,
            github_token
        )

        if not comments:
            post_pr_comment(
                message.repo_full_name,
                message.pr_number,
                "## OutcomeOps: Fix Tests\n\nNo comments found on this PR. Cannot find test analysis.",
                github_token
            )
            return {"status": "no_comments", "pr_number": message.pr_number}

        # Get test analysis
        test_analysis = fetch_test_analysis(comments)

        if not test_analysis:
            post_pr_comment(
                message.repo_full_name,
                message.pr_number,
                "## OutcomeOps: Fix Tests\n\nNo test analysis found. Please run `outcomeops: explain failed tests` first.",
                github_token
            )
            return {"status": "no_analysis", "pr_number": message.pr_number}

        logger.info(f"[tests-fix] Found test analysis: {len(test_analysis)} chars")

        # Get test failure output to find which files failed
        test_output = fetch_test_failure_output(comments)
        if test_output:
            logger.info(f"[tests-fix] Test output preview: {test_output[:500]}...")

        if not test_output:
            post_pr_comment(
                message.repo_full_name,
                message.pr_number,
                "## OutcomeOps: Fix Tests\n\nNo test failure output found. Cannot determine which files to fix.",
                github_token
            )
            return {"status": "no_test_output", "pr_number": message.pr_number}

        # Extract failed test files from test output
        failed_files = extract_failed_test_files(test_output)
        logger.info(f"[tests-fix] Extracted {len(failed_files)} files from test output")

        # Fallback: also try extracting from test analysis
        if not failed_files:
            logger.info("[tests-fix] No files from test output, trying test analysis")
            failed_files = extract_failed_test_files(test_analysis)
            logger.info(f"[tests-fix] Extracted {len(failed_files)} files from test analysis")

        if not failed_files:
            post_pr_comment(
                message.repo_full_name,
                message.pr_number,
                "## OutcomeOps: Fix Tests\n\nCould not extract test file paths from failure output.",
                github_token
            )
            return {"status": "no_files_found", "pr_number": message.pr_number}

        logger.info(f"[tests-fix] Found {len(failed_files)} test files to fix: {failed_files}")

        # Read test file contents
        test_files = {}
        files_not_found = []
        for file_path in failed_files[:5]:  # Limit to 5 files to avoid token limits
            logger.info(f"[tests-fix] Attempting to read: {file_path} from branch {message.branch_name}")
            file_data = get_file(
                message.repo_full_name,
                file_path,
                message.branch_name,
                github_token
            )
            if file_data:
                content = base64.b64decode(file_data.get("content", "")).decode("utf-8")
                test_files[file_path] = content
                logger.info(f"[tests-fix] Successfully read {file_path}: {len(content)} chars")
            else:
                logger.warning(f"[tests-fix] Failed to read {file_path} - file not found or error")
                files_not_found.append(file_path)

        if files_not_found:
            logger.info(f"[tests-fix] Files not found: {files_not_found}")

        if not test_files:
            post_pr_comment(
                message.repo_full_name,
                message.pr_number,
                f"## OutcomeOps: Fix Tests\n\nCould not read test files: {', '.join(failed_files)}",
                github_token
            )
            return {"status": "files_not_found", "pr_number": message.pr_number}

        # Query KB for test patterns
        repo_name = message.repo_full_name.split("/")[-1]
        kb_patterns = query_kb_for_test_patterns(repo_name)
        logger.info(f"[tests-fix] KB patterns: {len(kb_patterns)} chars")

        # Log user guidance if provided
        if message.guidance:
            logger.info(f"[tests-fix] User guidance provided: {message.guidance[:200]}...")
        else:
            logger.info("[tests-fix] No user guidance - using test analysis only")

        # Generate fixed tests (user guidance takes priority if provided)
        fixed_files = generate_test_fixes(
            test_analysis,
            test_files,
            kb_patterns,
            user_guidance=message.guidance
        )

        if not fixed_files:
            post_pr_comment(
                message.repo_full_name,
                message.pr_number,
                "## OutcomeOps: Fix Tests\n\nFailed to generate test fixes. Please fix manually.",
                github_token
            )
            return {"status": "generation_failed", "pr_number": message.pr_number}

        # Commit the fixed files
        files_fixed = []
        for file_path, content in fixed_files.items():
            # Check if content actually changed
            if file_path in test_files and content.strip() == test_files[file_path].strip():
                continue

            commit_message = f"fix(tests): fix failing tests in {file_path.split('/')[-1]}\n\nGenerated by OutcomeOps AI Assist"

            update_file(
                message.repo_full_name,
                file_path,
                content,
                message.branch_name,
                commit_message,
                github_token
            )
            files_fixed.append(file_path)
            logger.info(f"[tests-fix] Committed fix for {file_path}")

        if not files_fixed:
            post_pr_comment(
                message.repo_full_name,
                message.pr_number,
                "## OutcomeOps: Fix Tests\n\nNo changes were needed - tests appear to be correct.",
                github_token
            )
            return {"status": "no_changes_needed", "pr_number": message.pr_number}

        # Post comment about fixes and trigger test run
        files_list = "\n".join([f"- `{f}`" for f in files_fixed])
        progress_comment = f"""## OutcomeOps: Fix Tests

Test files have been updated:

{files_list}

Running tests to verify the fixes...

---
*Powered by [OutcomeOps AI Assist](https://outcomeops.ai)*
"""

        post_pr_comment(
            message.repo_full_name,
            message.pr_number,
            progress_comment,
            github_token
        )

        logger.info(f"[tests-fix] Successfully fixed {len(files_fixed)} test files for PR #{message.pr_number}")

        # Trigger run-tests to verify the fixes
        run_tests_result = invoke_run_tests(message)
        logger.info(f"[tests-fix] run-tests invocation: {run_tests_result}")

        return {
            "status": "success",
            "pr_number": message.pr_number,
            "files_fixed": files_fixed
        }

    except Exception as e:
        logger.error(f"[tests-fix] Failed: {e}", exc_info=True)

        # Try to post error comment
        try:
            github_token = get_github_token()
            post_pr_comment(
                message.repo_full_name,
                message.pr_number,
                f"## OutcomeOps: Fix Tests\n\nAn error occurred while fixing tests:\n```\n{str(e)}\n```",
                github_token
            )
        except Exception:
            pass

        return {"status": "error", "error": str(e), "pr_number": message.pr_number}
